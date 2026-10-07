#include "monitor_spectrum.h"

#include <math.h>
#include <string.h>

/* Rate table fact from the v6.0.3 SDK capture path: the private adctrig
 * divider argument is the rate code (80/40/20/10/8/4 MS/s -> codes 0..5),
 * verified against the SDK disasm (a3>>1 recovered as the divider) and the
 * public capability table of the research sources. Facts, not copied code. */
static const struct {
    uint32_t sample_rate_khz;
    uint8_t rate_code;
} k_rate_table[] = {
    {80000, 0},
    {40000, 1},
    {20000, 2},
    {10000, 3},
    {8000, 4},
    {4000, 5},
};

static const uint16_t k_fft_sizes[] = {64, 128, 256, 512, 1024};

#define SPECTRUM_PI_F 3.14159265358979323846f
#define IQ_FULL_SCALE 512.0f          /* signed 10-bit field range -512..511 */
#define POWER_FLOOR 1.0e-30f          /* log10 guard; encodes to the floor */
#define CENTI_DB_FLOOR (-32768)

/* Fixed-size static workspace (init-only allocation, documented in the
 * header). Single owner: the application task. The Hann window value is
 * computed inline per capture (no window table — statics are kept minimal
 * because the capture-bank linker assertion bounds DRAM growth). */
static bool g_spectrum_ready;
static float g_twiddle_re[MONITOR_SPECTRUM_MAX_BINS / 2];
static float g_twiddle_im[MONITOR_SPECTRUM_MAX_BINS / 2];
static float g_work[2 * MONITOR_SPECTRUM_MAX_BINS];

static void write_le16(uint8_t *dst, uint16_t value) {
    dst[0] = (uint8_t)(value & 0xffu);
    dst[1] = (uint8_t)((value >> 8) & 0xffu);
}

static void write_le32(uint8_t *dst, uint32_t value) {
    dst[0] = (uint8_t)(value & 0xffu);
    dst[1] = (uint8_t)((value >> 8) & 0xffu);
    dst[2] = (uint8_t)((value >> 16) & 0xffu);
    dst[3] = (uint8_t)((value >> 24) & 0xffu);
}

bool monitor_spectrum_init(void) {
    if (g_spectrum_ready) {
        return true;
    }
    for (uint32_t k = 0; k < MONITOR_SPECTRUM_MAX_BINS / 2u; k++) {
        float angle = -2.0f * SPECTRUM_PI_F * (float)k /
                      (float)MONITOR_SPECTRUM_MAX_BINS;
        g_twiddle_re[k] = cosf(angle);
        g_twiddle_im[k] = sinf(angle);
    }
    g_spectrum_ready = true;
    return true;
}

uint16_t monitor_spectrum_effective_fft_size(uint16_t requested) {
    if (requested >= MONITOR_SPECTRUM_MAX_BINS) {
        return MONITOR_SPECTRUM_MAX_BINS;
    }
    uint16_t best = 64;
    for (size_t i = 0; i < sizeof(k_fft_sizes) / sizeof(k_fft_sizes[0]); i++) {
        if (k_fft_sizes[i] <= requested) {
            best = k_fft_sizes[i];
        }
    }
    return best;
}

uint8_t monitor_spectrum_rate_code(uint32_t sample_rate_khz) {
    for (size_t i = 0; i < sizeof(k_rate_table) / sizeof(k_rate_table[0]); i++) {
        if (k_rate_table[i].sample_rate_khz == sample_rate_khz) {
            return k_rate_table[i].rate_code;
        }
    }
    return MONITOR_SPECTRUM_RATE_UNKNOWN;
}

uint32_t monitor_spectrum_rate_khz(uint8_t rate_code) {
    for (size_t i = 0; i < sizeof(k_rate_table) / sizeof(k_rate_table[0]); i++) {
        if (k_rate_table[i].rate_code == rate_code) {
            return k_rate_table[i].sample_rate_khz;
        }
    }
    return 0;
}

bool monitor_spectrum_fft_size_valid(uint16_t fft_size) {
    return fft_size >= 2 && fft_size <= MONITOR_SPECTRUM_MAX_BINS &&
           (fft_size & (uint16_t)(fft_size - 1u)) == 0;
}

static int32_t sign_extend_10(uint32_t value) {
    int32_t v = (int32_t)(value & 0x3ffu);
    if ((v & 0x200) != 0) {
        v -= 0x400;
    }
    return v;
}

/* In-place radix-2 DIT FFT over g_work (interleaved re,im, fft_size
 * complex entries). Twiddles index the MAX-sized table with stride
 * MAX/size, valid for every supported power-of-two size. */
static void run_fft(uint16_t fft_size) {
    uint16_t bits = 0;
    for (uint16_t v = fft_size; v > 1u; v >>= 1) {
        bits++;
    }
    /* bit-reversal permutation */
    for (uint16_t i = 0; i < fft_size; i++) {
        uint16_t rev = 0;
        for (uint16_t b = 0; b < bits; b++) {
            rev = (uint16_t)((rev << 1) | ((i >> b) & 1u));
        }
        if (rev > i) {
            float tr = g_work[2u * i];
            float ti = g_work[2u * i + 1u];
            g_work[2u * i] = g_work[2u * rev];
            g_work[2u * i + 1u] = g_work[2u * rev + 1u];
            g_work[2u * rev] = tr;
            g_work[2u * rev + 1u] = ti;
        }
    }
    uint16_t twiddle_table = MONITOR_SPECTRUM_MAX_BINS;
    for (uint16_t len = 2; len <= fft_size; len = (uint16_t)(len << 1)) {
        uint16_t half = (uint16_t)(len >> 1);
        /* Stage len needs e^(-i*2*pi*j/len): index the MAX-sized table with
         * stride MAX/len, PER STAGE (not once for the whole transform). */
        uint16_t tw_stride = (uint16_t)(twiddle_table / len);
        for (uint16_t base = 0; base < fft_size; base = (uint16_t)(base + len)) {
            for (uint16_t j = 0; j < half; j++) {
                uint16_t tw = (uint16_t)(j * tw_stride);
                float wr = g_twiddle_re[tw];
                float wi = g_twiddle_im[tw];
                uint16_t a = (uint16_t)(base + j);
                uint16_t b = (uint16_t)(a + half);
                float ar = g_work[2u * a];
                float ai = g_work[2u * a + 1u];
                float br = g_work[2u * b];
                float bi = g_work[2u * b + 1u];
                float tr = wr * br - wi * bi;
                float ti = wr * bi + wi * br;
                g_work[2u * a] = ar + tr;
                g_work[2u * a + 1u] = ai + ti;
                g_work[2u * b] = ar - tr;
                g_work[2u * b + 1u] = ai - ti;
            }
        }
    }
}

bool monitor_spectrum_power_dbfs(const uint32_t *iq_words, uint16_t fft_size,
                                 int16_t *out_bins) {
    if (!g_spectrum_ready || iq_words == NULL || out_bins == NULL ||
        !monitor_spectrum_fft_size_valid(fft_size)) {
        return false;
    }
    /* Periodic Hann; for N >= 2 its sum is exactly N/2 (the cosine terms
     * cancel over a full period), so W is computed analytically. */
    float window_sum = (float)fft_size / 2.0f;
    float inv_w = 1.0f / window_sum;
    for (uint16_t n = 0; n < fft_size; n++) {
        float w = 0.5f * (1.0f - cosf(2.0f * SPECTRUM_PI_F * (float)n /
                                       (float)fft_size));
        int32_t i_raw = sign_extend_10(iq_words[n]);
        int32_t q_raw = sign_extend_10(iq_words[n] >> 10);
        g_work[2u * n] = ((float)i_raw / IQ_FULL_SCALE) * w;
        g_work[2u * n + 1u] = ((float)q_raw / IQ_FULL_SCALE) * w;
    }
    run_fft(fft_size);
    uint16_t shift = (uint16_t)(fft_size >> 1);
    for (uint16_t k = 0; k < fft_size; k++) {
        uint16_t src = (uint16_t)((k + shift) & (uint16_t)(fft_size - 1u));
        float re = g_work[2u * src];
        float im = g_work[2u * src + 1u];
        float power = (re * re + im * im) * inv_w * inv_w;
        float dbfs = 10.0f * log10f(power > POWER_FLOOR ? power
                                                        : POWER_FLOOR);
        if (!isfinite(dbfs)) {
            /* Broken input or math: fail the whole frame instead of
             * publishing fabricated bins (handoff missing semantics). */
            return false;
        }
        long centi = lroundf(dbfs * 100.0f);
        if (centi > INT16_MAX) {
            centi = INT16_MAX;
        } else if (centi < CENTI_DB_FLOOR) {
            centi = CENTI_DB_FLOOR;
        }
        out_bins[k] = (int16_t)centi;
    }
    return true;
}

bool monitor_tlv_wrap_spectrum(MonitorTlvFrame *frame,
                               const MonitorSpectrumEvent *event) {
    if (frame == NULL || event == NULL || event->bins == NULL ||
        !monitor_spectrum_fft_size_valid(event->fft_size)) {
        return false;
    }
    if (event->source != MONITOR_SPECTRUM_SOURCE_C5_SNAPSHOT) {
        return false;                   /* reserved sources are not emitted */
    }
    size_t payload = MONITOR_SPECTRUM_META_BYTES +
                     (size_t)event->fft_size * 2u;
    size_t total = MONITOR_TLV_HEADER_BYTES + payload +
                   MONITOR_TLV_CRC_BYTES;
    if (total > MONITOR_TLV_FRAME_CAPACITY_BYTES) {
        return false;
    }
    uint8_t *p = frame->bytes;
    p[0] = MONITOR_SPECTRUM_WIRE_TYPE;
    write_le16(p + 1, (uint16_t)payload);
    uint8_t *body = p + MONITOR_TLV_HEADER_BYTES;
    write_le32(body + 0, event->epoch);
    write_le32(body + 4, event->cycle);
    body[8] = event->band;
    body[9] = event->channel;
    body[10] = event->mode;
    body[11] = event->rate_code;
    write_le16(body + 12, event->fft_size);
    write_le16(body + 14, event->source);
    write_le32(body + 16, event->center_khz);
    write_le32(body + 20, event->span_khz);
    for (uint16_t i = 0; i < event->fft_size; i++) {
        write_le16(body + MONITOR_SPECTRUM_META_BYTES + (size_t)i * 2u,
                   (uint16_t)event->bins[i]);
    }
    uint32_t crc = monitor_crc32(p, MONITOR_TLV_HEADER_BYTES + payload);
    write_le32(p + MONITOR_TLV_HEADER_BYTES + payload, crc);
    frame->length_bytes = total;
    return true;
}
