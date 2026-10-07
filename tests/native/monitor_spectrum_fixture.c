/*
 * Host fixture for the portable spectrum core (monitor_spectrum.c): the
 * same source the firmware links, compiled with the host compiler. Modes:
 *
 *   golden            wrap the handoff v2.1 golden event, print frame hex
 *   bins <fft_size>   read fft_size u32 IQ words (LE) on stdin, print the
 *                     fftshifted centi-dBFS bins as decimal text
 *   map <fft> <rate>  print "<effective_fft> <rate_code> <span_khz>"
 *   invalid           exercise the rejection paths, print "ok" on pass
 *
 * Test scaffolding only (never linked into firmware).
 */
#include "monitor_spectrum.h"
#include "monitor_capture.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Fake of the private SDK entry (librftest.a) so the adapter links on the
 * host. The native fixture never runs monitor_capture_snapshot's MCU path;
 * sentinel validation is driven directly through
 * monitor_capture_validate_range in "validate" mode. */
void adctrig(uint32_t count_minus_one, uint32_t arg1, uint32_t arg2,
             uint32_t divider_times_two, uint32_t arg4, uint32_t arg5,
             uint32_t arg6, uint32_t arg7, uint32_t stack_arg8) {
    (void)count_minus_one; (void)arg1; (void)arg2; (void)divider_times_two;
    (void)arg4; (void)arg5; (void)arg6; (void)arg7; (void)stack_arg8;
}

/* Sentinel constants mirrored from monitor_capture.c (same-source check is
 * done by compiling monitor_capture.c itself; these are only for the
 * standalone "validate" scenarios). */
#define V_HEAD 0xA53C9669u
#define V_TAIL 0x5AC39669u

static int mode_validate(void) {
    static uint32_t buf[72];
    const uint32_t n = 64;
    const uint32_t guards = 4;
    MonitorCaptureStatus results[5];

    for (uint32_t i = 0; i < n + guards; i++) {
        buf[i] = 0x11110000u + i;               /* rewritten, not HEAD */
    }
    for (uint32_t i = 0; i < guards; i++) {
        buf[n + i] = V_TAIL ^ i;
    }
    results[0] = monitor_capture_validate_range(buf, n, guards);

    /* Partial dump: only the middle of the range untouched. */
    buf[5] = V_HEAD;
    results[1] = monitor_capture_validate_range(buf, n, guards);
    buf[5] = 0x22220005u;

    /* Parent review case: first word written, the rest never written. */
    for (uint32_t i = 1; i < n; i++) {
        buf[i] = V_HEAD;
    }
    results[2] = monitor_capture_validate_range(buf, n, guards);
    for (uint32_t i = 1; i < n; i++) {
        buf[i] = 0x33330000u + i;
    }

    /* Guard overrun: dump wrote one word past the request. */
    buf[n + 1] ^= 1u;
    results[3] = monitor_capture_validate_range(buf, n, guards);
    buf[n + 1] = V_TAIL ^ 1u;

    /* NULL argument. */
    results[4] = monitor_capture_validate_range(NULL, n, guards);

    printf("%d %d %d %d %d\n", (int)results[0], (int)results[1],
           (int)results[2], (int)results[3], (int)results[4]);
    return 0;
}

static int mode_golden(void) {
    static const int16_t bins[8] = {0, -100, -200, -300, -400, -500, -600,
                                    -700};
    MonitorSpectrumEvent event = {
        .epoch = 1,
        .cycle = 0,
        .band = 0,
        .channel = 1,
        .mode = 0,
        .rate_code = 0,
        .fft_size = 8,
        .source = MONITOR_SPECTRUM_SOURCE_C5_SNAPSHOT,
        .center_khz = 2412000,
        .span_khz = 20000,
        .bins = bins,
    };
    MonitorTlvFrame frame;
    if (!monitor_tlv_wrap_spectrum(&frame, &event)) {
        return 1;
    }
    for (size_t i = 0; i < frame.length_bytes; i++) {
        printf("%02x", frame.bytes[i]);
    }
    printf("\n");
    return 0;
}

static int mode_bins(uint16_t fft_size) {
    static uint32_t words[MONITOR_SPECTRUM_MAX_BINS];
    static int16_t out[MONITOR_SPECTRUM_MAX_BINS];
    if (fft_size > MONITOR_SPECTRUM_MAX_BINS ||
        fread(words, sizeof(uint32_t), fft_size, stdin) != fft_size) {
        return 2;
    }
    if (!monitor_spectrum_init()) {
        return 3;
    }
    if (!monitor_spectrum_power_dbfs(words, fft_size, out)) {
        return 4;
    }
    for (uint16_t i = 0; i < fft_size; i++) {
        printf("%d%s", (int)out[i], i + 1 == fft_size ? "\n" : " ");
    }
    return 0;
}

static int mode_map(int fft, int rate) {
    if (!monitor_spectrum_init()) {
        return 3;
    }
    uint8_t code = monitor_spectrum_rate_code((uint32_t)rate);
    printf("%u %u %u\n",
           (unsigned)monitor_spectrum_effective_fft_size((uint16_t)fft),
           (unsigned)code, (unsigned)monitor_spectrum_rate_khz(code));
    return 0;
}

static int mode_invalid(void) {
    MonitorTlvFrame frame;
    MonitorSpectrumEvent event = {0};
    static int16_t bins[8] = {0};
    static uint32_t words[8] = {0};

    if (!monitor_spectrum_init()) {
        return 1;
    }
    /* non-power-of-two frame rejected */
    event.fft_size = 100;
    event.source = MONITOR_SPECTRUM_SOURCE_C5_SNAPSHOT;
    event.bins = bins;
    if (monitor_tlv_wrap_spectrum(&frame, &event)) {
        return 1;
    }
    /* unknown source rejected */
    event.fft_size = 8;
    event.source = 7;
    if (monitor_tlv_wrap_spectrum(&frame, &event)) {
        return 1;
    }
    /* NULL rejection */
    if (monitor_tlv_wrap_spectrum(NULL, &event) ||
        monitor_tlv_wrap_spectrum(&frame, NULL)) {
        return 1;
    }
    if (monitor_spectrum_power_dbfs(NULL, 8, bins) ||
        monitor_spectrum_power_dbfs(words, 3, bins) ||
        monitor_spectrum_power_dbfs(words, 8, NULL)) {
        return 1;
    }
    /* rate mapping honesty: unknown input stays unknown */
    if (monitor_spectrum_rate_code(12345) != MONITOR_SPECTRUM_RATE_UNKNOWN ||
        monitor_spectrum_rate_khz(9) != 0) {
        return 1;
    }
    printf("ok\n");
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        return 64;
    }
    if (strcmp(argv[1], "golden") == 0) {
        return mode_golden();
    }
    if (strcmp(argv[1], "bins") == 0 && argc == 3) {
        return mode_bins((uint16_t)atoi(argv[2]));
    }
    if (strcmp(argv[1], "map") == 0 && argc == 4) {
        return mode_map(atoi(argv[2]), atoi(argv[3]));
    }
    if (strcmp(argv[1], "invalid") == 0) {
        return mode_invalid();
    }
    if (strcmp(argv[1], "validate") == 0) {
        return mode_validate();
    }
    return 64;
}
