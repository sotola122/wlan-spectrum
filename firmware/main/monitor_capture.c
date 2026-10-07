#include "monitor_capture.h"

#include <string.h>

#include "esp_timer.h"
#include "heap_memory_layout.h"

/* Fixed capture address; the SDK routine writes here unconditionally
 * (v6.0.3 disasm: lui a3,0x40830). The bank around it is reserved. */
#define IQ_CAPTURE_WORDS ((volatile uint32_t *)0x40830000u)
/* One guard word past the request; the bank owns far more. */
#define IQ_GUARD_WORDS 4u

/* Private SDK entry (see header). 9 x u32, little-endian args. */
extern void adctrig(uint32_t count_minus_one, uint32_t arg1, uint32_t arg2,
                    uint32_t divider_times_two, uint32_t arg4, uint32_t arg5,
                    uint32_t arg6, uint32_t arg7, uint32_t stack_arg8);

/* Ownership exclusion for the modem capture bank (heap exclusion; static
 * exclusion is enforced by main/bank_guard.ld at link time). */
SOC_RESERVE_MEMORY_REGION(0x40820000, 0x40840000, monitor_iq_bank);

/* Project-chosen completion sentinels (technique shared with public
 * research; values are ours so no upstream code is reproduced): the
 * request region is pre-filled with IQ_HEAD_SENTINEL and one guard word
 * per extra slot with IQ_TAIL_SENTINEL ^ index. After adctrig returns:
 *   intact head  -> the dump never wrote -> TIMEOUT;
 *   touched tail -> the dump wrote past the request -> OVERRUN. */
#define IQ_HEAD_SENTINEL 0xA53C9669u
#define IQ_TAIL_SENTINEL 0x5AC39669u

static bool g_capture_ready;

esp_err_t monitor_capture_init(void) {
    g_capture_ready = false;
    if (!monitor_spectrum_init()) {
        return ESP_ERR_INVALID_STATE;
    }
    g_capture_ready = true;
    return ESP_OK;
}

MonitorCaptureStatus monitor_capture_validate_range(
    const volatile uint32_t *words, uint32_t word_count,
    uint32_t guard_count) {
    if (words == NULL || word_count == 0) {
        return MONITOR_CAPTURE_BAD_ARG;
    }
    /* Whole-range completeness: every requested word must have been
     * rewritten. Checking only word 0 would let a partial dump publish a
     * half-stale spectrum. A written word coincidentally equal to the
     * sentinel costs 2^-32 per word and only causes a retry, never a fake
     * frame. */
    for (uint32_t i = 0; i < word_count; i++) {
        if (words[i] == IQ_HEAD_SENTINEL) {
            return MONITOR_CAPTURE_TIMEOUT;
        }
    }
    for (uint32_t i = 0; i < guard_count; i++) {
        if (words[word_count + i] != (IQ_TAIL_SENTINEL ^ i)) {
            return MONITOR_CAPTURE_OVERRUN;
        }
    }
    return MONITOR_CAPTURE_OK;
}

MonitorCaptureStatus monitor_capture_snapshot(uint16_t fft_size,
                                              uint8_t rate_code,
                                              int16_t *out_bins,
                                              uint32_t *elapsed_us) {
    int64_t start_us = esp_timer_get_time();
    if (!g_capture_ready || out_bins == NULL ||
        !monitor_spectrum_fft_size_valid(fft_size) ||
        rate_code > 5u) {
        return MONITOR_CAPTURE_BAD_ARG;
    }
    uint32_t words = fft_size;
    for (uint32_t i = 0; i < words; i++) {
        IQ_CAPTURE_WORDS[i] = IQ_HEAD_SENTINEL;
    }
    for (uint32_t i = 0; i < IQ_GUARD_WORDS; i++) {
        IQ_CAPTURE_WORDS[words + i] = IQ_TAIL_SENTINEL ^ i;
    }
    adctrig(words - 1u, 0u, 0u, (uint32_t)rate_code * 2u, 0u, 0u, 0u, 0u, 0u);
    MonitorCaptureStatus status = monitor_capture_validate_range(
        IQ_CAPTURE_WORDS, words, IQ_GUARD_WORDS);
    if (status == MONITOR_CAPTURE_OK) {
        /* Read the bank directly (no copy buffer: DRAM is bounded by the
         * bank_guard assertion and the C5 does not data-cache this SRAM —
         * evidenced by sentinel-valid AMBIENT captures on hardware. The
         * tone correctness checks are the NATIVE DSP synthetic-tone suite
         * against a numpy oracle (host build); no controlled RF tone
         * generator was ever run on air.) */
        if (!monitor_spectrum_power_dbfs((const uint32_t *)IQ_CAPTURE_WORDS,
                                         fft_size, out_bins)) {
            status = MONITOR_CAPTURE_BAD_ARG;
        }
    }
    if (elapsed_us != NULL) {
        int64_t elapsed = esp_timer_get_time() - start_us;
        *elapsed_us = elapsed > 0 ? (uint32_t)elapsed : 0u;
    }
    return status;
}
