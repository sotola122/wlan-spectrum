/*
 * ESP-IDF capture adapter for the ESP32-C5 snapshot I/Q path (v6.0.3).
 *
 * Mechanism (source-backed, independently implemented): the private
 * `adctrig` entry in components/esp_phy/lib/esp32c5/librftest.a
 * (mac_common.o, symbol size 0x57e) arms the modem dump engine at
 * 0x600a9004/0x600a9008 and stores complex samples as 20-bit words into
 * the fixed SRAM address 0x40830000 (the buffer base is a literal inside
 * the SDK routine). This module links that archive by enabling
 * CONFIG_ESP_PHY_ENABLE_CERT_TEST (SDK feature gate, no new code paths in
 * this project) and declares the call from the v6.0.3 disassembly:
 *     adctrig(n_words - 1, 0, 0, rate_code * 2, 0, 0, 0, 0, 0)
 * where rate_code 0..5 = 80/40/20/10/8/4 MS/s (divider = a3 >> 1 inside
 * the SDK routine).
 *
 * Boundedness (from the v6.0.3 disassembly, evidence
 * research-adctrig-disasm.txt): the routine polls done status bit 0x40000
 * of 0x600a9004 and additionally exits its wait loop once the delta of
 * 0x600ad800 exceeds 1,000,000 ticks. It returns through a common cleanup
 * that clears the bits it set in 0x600a9004 and 0x60095004. NO wall-clock
 * ceiling is proven: the tick rate of 0x600ad800 is unproven, and the fast
 * capture cycles seen in acceptance runs are observations, not a
 * worst-case bound. Configured backstop (actual sdkconfig):
 * CONFIG_ESP_TASK_WDT_EN=y with CONFIG_ESP_TASK_WDT_TIMEOUT_S=5 but
 * CONFIG_ESP_TASK_WDT_PANIC is NOT set — a hypothetical both-exits failure
 * would starve the idle task and stall the stream WITHOUT resetting the
 * board (log output is suppressed as well), so recovery would need an
 * external reset. The watchdog is therefore observability only, not a
 * bound. A task-level timeout around this call is deliberately NOT the
 * only bound — the bound must come from inside the SDK routine.
 *
 * UART hygiene: the routine's only print call is `phy_printf`, which IDF's
 * Apache-2.0 components/esp_phy/src/lib_printf.c routes into ESP_LOGI, and
 * this firmware builds with CONFIG_LOG_DEFAULT_LEVEL_NONE +
 * ESP_CONSOLE_NONE, so no diagnostic text can interleave with the TLV
 * stream (source-level expectation only — REQUIRED to be re-verified by
 * the hardware probe: parser discarded_bytes must stay 0 for the whole
 * stream, evidence log pending).
 *
 * SRAM ownership: the capture bank 0x40820000-0x40840000 (128 KiB, the
 * granularity of the modem's ownership bit) is excluded from heap by
 * SOC_RESERVE_MEMORY_REGION and from static placement by the linker
 * assertion in main/bank_guard.ld. No heap is used here (no generic heap
 * in the measurement path); everything else is caller-provided.
 *
 * Concurrency: application task only, called between monitor_radio_finish
 * and the next monitor_radio_begin (promiscuous reception is off during the
 * capture window; packet observations and spectrum captures never overlap).
 */
#ifndef MONITOR_CAPTURE_H
#define MONITOR_CAPTURE_H

#include <stdint.h>

#include "esp_err.h"

#include "monitor_spectrum.h"

typedef enum {
    MONITOR_CAPTURE_OK = 0,
    MONITOR_CAPTURE_BAD_ARG = 1,       /* invalid size/rate/init state */
    MONITOR_CAPTURE_TIMEOUT = 2,       /* requested range not fully written */
    MONITOR_CAPTURE_OVERRUN = 3,       /* dump wrote past the request */
} MonitorCaptureStatus;

/* Sentinel validation for one completed capture, exposed for host tests.
 * words[0..word_count-1] must ALL differ from the head sentinel (a partial
 * dump that writes only the first word still counts as TIMEOUT) and the
 * guard_count words after the range must still equal their tail sentinels
 * (any change = OVERRUN). Takes the buffer explicitly so the native test
 * fixture can drive partial/overrun cases without MCU hardware. */
MonitorCaptureStatus monitor_capture_validate_range(
    const volatile uint32_t *words, uint32_t word_count,
    uint32_t guard_count);

/* Prepare the spectrum engine (fixed tables). Call once after
 * monitor_radio_init. Returns ESP_ERR_INVALID_STATE when the tables cannot
 * be prepared; the caller then keeps spectrum disabled (STATUS reports
 * spectrum:false) instead of emitting fabricated frames. */
esp_err_t monitor_capture_init(void);

/* Take one snapshot at the parked channel: fill sentinels, trigger adctrig,
 * validate completion markers, then compute fftshifted centi-dBFS bins.
 * fft_size must be a valid power of two; rate_code 0..5; out_bins holds
 * fft_size entries. *elapsed_us (nullable) receives the measured wall time
 * of the whole call for evidence. Bounds: adctrig self-bounds as documented
 * above; FFT work uses static buffers; no allocation, no logging. */
MonitorCaptureStatus monitor_capture_snapshot(uint16_t fft_size,
                                              uint8_t rate_code,
                                              int16_t *out_bins,
                                              uint32_t *elapsed_us);

#endif /* MONITOR_CAPTURE_H */
