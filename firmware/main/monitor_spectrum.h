/*
 * Portable spectrum core: I/Q unpacking, windowed radix-2 FFT, contract
 * normalization, fftshift ordering, and the 0x04 SPECTRUM_RF wire frame.
 * No SDK, RTOS, or MCU headers (CODING_GUIDELINE section 6); the IDF capture
 * adapter lives in monitor_capture.h.
 *
 * Wire contract: docs/tlv-protocol.md (frozen layout, frozen
 * units). Bins are centi-dBFS (int16, 0.01 dB) relative to a full-scale
 * complex tone; this is not dBm and must never be converted to RSSI.
 *
 * Concurrency: task-context only, single caller (the application task).
 * monitor_spectrum_init must succeed before any other call; it computes the
 * fixed-size twiddle table exactly once. No allocation after init: all
 * buffers are file-scope statics, documented and bounded (MAX bins = 1024).
 *
 * Normalization (frozen): per-capture DC removal FIRST — subtract the
 * capture's own mean I and mean Q from the raw samples (explicit
 * limitation: a true signal exactly at the tuned centre is removed with
 * the offset; subtraction only, no notch/interpolation/absolute offset) —
 * then periodic Hann w[n]=0.5*(1-cos(2*pi*n/N)), W=sum(w), X=FFT(x*w),
 * P=|X|^2 / W^2 (complex two-sided convention — NO factor 4),
 * dBFS=10*log10(P), encoded as round(dBFS*100) clamped to int16,
 * floored at -32768. A full-scale complex tone on a bin center reads
 * 0 dBFS; half amplitude reads about -602 (centi-dB).
 *
 * Bin order (frozen, fftshift): bin k of N sits at
 * center_khz - span_khz/2 + k*span_khz/N; DC (even N) at k = N/2.
 */
#ifndef MONITOR_SPECTRUM_H
#define MONITOR_SPECTRUM_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "monitor_core.h"

#define MONITOR_SPECTRUM_MAX_BINS 1024
#define MONITOR_SPECTRUM_WIRE_TYPE 0x04
#define MONITOR_SPECTRUM_SOURCE_C5_SNAPSHOT 0
#define MONITOR_SPECTRUM_META_BYTES 24

/* One device-to-host spectrum observation. bins borrows fft_size entries
 * for the wrap call only. epoch/cycle/band/channel/mode match the STATUS
 * events; center_khz/span_khz are this frame's effective values (per-frame
 * authoritative; there is no global center). */
typedef struct {
    uint32_t epoch;
    uint32_t cycle;
    uint8_t band;
    uint8_t channel;
    uint8_t mode;
    uint8_t rate_code;
    uint16_t fft_size;
    uint16_t source;
    uint32_t center_khz;
    uint32_t span_khz;
    const int16_t *bins;                /* borrowed, fft_size entries */
} MonitorSpectrumEvent;

/* Compute the fixed-size twiddle table. Idempotent, bounded, no allocation.
 * Returns false on internal table-range failure (treated as fatal by the
 * caller). Every other function requires a prior successful call. */
bool monitor_spectrum_init(void);

/* Effective FFT size for a requested size: the largest supported power of
 * two <= requested from {64,128,256,512,1024}; below 64 returns 64.
 * Requests above MONITOR_SPECTRUM_MAX_BINS return MONITOR_SPECTRUM_MAX_BINS. */
uint16_t monitor_spectrum_effective_fft_size(uint16_t requested);

/* Rate code for a requested sample rate in kHz: exact match against the
 * rate table (80000->0, 40000->1, 20000->2, 10000->3, 8000->4, 4000->5).
 * Unknown input returns MONITOR_SPECTRUM_RATE_UNKNOWN: callers must treat
 * that as unsupported instead of echoing a fabricated rate. */
#define MONITOR_SPECTRUM_RATE_UNKNOWN 0xffu
uint8_t monitor_spectrum_rate_code(uint32_t sample_rate_khz);

/* Inverse of monitor_spectrum_rate_code; unknown code returns 0. */
uint32_t monitor_spectrum_rate_khz(uint8_t rate_code);

/* True for a power-of-two bin count the frame format accepts (even, 2..MAX).
 * fft_size == 0 or non-power-of-two returns false. */
bool monitor_spectrum_fft_size_valid(uint16_t fft_size);

/* Unpack n complex samples from the capture buffer words (each word holds
 * two signed 10-bit fields: I in bits 0..9, Q in bits 10..19 — v6.0.3 SDK
 * capture format), apply the periodic Hann window, run the radix-2 FFT,
 * normalize per the frozen convention, and write fftshifted centi-dBFS into
 * out_bins[fft_size]. Returns false on invalid fft_size, NULL arguments, or
 * a non-finite intermediate; on false nothing valid was produced and the
 * caller must NOT transmit out_bins (no fabricated bins — partial contents
 * are unspecified). Task context,
 * single caller; uses only the static work buffer. */
bool monitor_spectrum_power_dbfs(const uint32_t *iq_words, uint16_t fft_size,
                                 int16_t *out_bins);

/* Wrap a complete SPECTRUM_RF frame (type 0x04) into frame: header, payload
 * of MONITOR_SPECTRUM_META_BYTES + 2*fft_size, CRC-32/ISO-HDLC. Returns false
 * (frame untouched) on NULL, invalid fft_size/source, NULL bins, or a frame
 * exceeding MONITOR_TLV_FRAME_CAPACITY_BYTES. */
bool monitor_tlv_wrap_spectrum(MonitorTlvFrame *frame,
                               const MonitorSpectrumEvent *event);

#endif /* MONITOR_SPECTRUM_H */
