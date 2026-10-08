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

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

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

/* bench: DSP measurement for the settings-performance scope (timing only,
 * host-dependent, no pass/fail thresholds). Prints ns/frame for the
 * production path (fft64/fft1024) and an in-process A/B of the Hann step
 * (per-sample cosf vs twiddle-table lookup) on identical inputs. */
static int mode_bench(void) {
    static uint32_t iq64[64];
    static uint32_t iq1024[1024];
    static int16_t out_bins[1024];
    for (uint16_t i = 0; i < 1024; i++) {
        int32_t v = (int32_t)((uint32_t)(i * 37u) % 900u) - 450;
        int32_t u = (int32_t)((uint32_t)(i * 91u) % 900u) - 450;
        iq1024[i] = (uint32_t)(v & 0x3ff) | ((uint32_t)(u & 0x3ff) << 10);
        if (i < 64) {
            iq64[i] = iq1024[i];
        }
    }
    if (!monitor_spectrum_init()) {
        return 1;
    }

    const int reps64 = 50000;
    const int reps1024 = 5000;
    clock_t c0 = clock();
    for (int r = 0; r < reps64; r++) {
        if (!monitor_spectrum_power_dbfs(iq64, 64, out_bins)) {
            return 1;
        }
    }
    double ns64 = (double)(clock() - c0) * 1e9 / CLOCKS_PER_SEC / reps64;
    c0 = clock();
    for (int r = 0; r < reps1024; r++) {
        if (!monitor_spectrum_power_dbfs(iq1024, 1024, out_bins)) {
            return 1;
        }
    }
    double ns1024 = (double)(clock() - c0) * 1e9 / CLOCKS_PER_SEC / reps1024;
    printf("bench power_dbfs fft=64 frames=%d ns_per_frame=%.1f\n",
           reps64, ns64);
    printf("bench power_dbfs fft=1024 frames=%d ns_per_frame=%.1f\n",
           reps1024, ns1024);

    /* Hann step A/B, identical inputs: per-sample cosf (the previous
     * product path) vs precomputed-table lookup (the current path). */
    static float twiddle[512];
    for (uint16_t j = 0; j < 512; j++) {
        twiddle[j] = cosf(-2.0f * 3.14159265358979323846f * (float)j /
                          1024.0f);
    }
    const int reps_win = 50000;
    volatile float sink = 0.0f;
    c0 = clock();
    for (int r = 0; r < reps_win; r++) {
        float acc = 0.0f;
        for (uint16_t n = 0; n < 1024; n++) {
            acc += 0.5f * (1.0f - cosf(2.0f * 3.14159265358979323846f *
                                       (float)n / 1024.0f));
        }
        sink += acc;
    }
    double cosf_ns = (double)(clock() - c0) * 1e9 / CLOCKS_PER_SEC /
                     (double)reps_win;
    c0 = clock();
    for (int r = 0; r < reps_win; r++) {
        float acc = 0.0f;
        for (uint16_t n = 0; n < 1024; n++) {
            uint16_t j = n;                 /* stride MAX/N == 1 at N=1024 */
            float cosv = j < 512 ? twiddle[j] : -twiddle[j - 512];
            acc += 0.5f * (1.0f - cosv);
        }
        sink += acc;
    }
    double twiddle_ns = (double)(clock() - c0) * 1e9 / CLOCKS_PER_SEC /
                        (double)reps_win;
    printf("bench hann cosf_ns=%.2f twiddle_ns=%.2f sink=%.3f\n",
           cosf_ns, twiddle_ns, (double)sink);
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
    if (strcmp(argv[1], "bench") == 0) {
        return mode_bench();
    }
    return 64;
}
