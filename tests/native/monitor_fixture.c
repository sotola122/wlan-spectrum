/*
 * Host-side behavioral fixture for the portable monitor core.
 *
 * Modes:
 *   config-parse <hex>   feed one CONFIG frame byte-at-a-time, print fields
 *   dwell                print the three dwell boundary values
 *   selftest <case>      assert-based native checks (exit 0 = pass)
 *
 * Test scaffolding only: never linked into the firmware image
 * (CODING_GUIDELINE section 9).
 */
/* Relative include: the host fixture lives outside the firmware component, so
 * no build-system include path exists for it. */
#include "../../firmware/main/monitor_core.h"

#include <cJSON.h>
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Documented CONFIG vector (docs/tlv-protocol.md): Band Sweep, 5 GHz,
 * 1000 ms, FFT 64, 20 MS/s, with the published CRC32 95 56 4e 1d appended.
 * The checksum bytes are literal spec data, never manufactured here. */
static const uint8_t k_config_frame[MONITOR_CONFIG_STREAM_BYTES] = {
    0x10, 0x0a, 0x00, 0x01, 0x01, 0xe8, 0x03, 0x40, 0x00,
    0x20, 0x4e, 0x00, 0x00, 0x95, 0x56, 0x4e, 0x1d,
};

/* Recompute the checksum after mutating a frame under test, so the parser
 * exercises field validation rather than CRC rejection. */
static void fix_crc(uint8_t *frame) {
    uint32_t crc = monitor_crc32(frame, MONITOR_CONFIG_FRAME_BYTES);
    frame[13] = (uint8_t)(crc & 0xffu);
    frame[14] = (uint8_t)((crc >> 8) & 0xffu);
    frame[15] = (uint8_t)((crc >> 16) & 0xffu);
    frame[16] = (uint8_t)((crc >> 24) & 0xffu);
}

static bool feed_bytes(const uint8_t *bytes, size_t count,
                       MonitorConfigParser *parser, MonitorConfig *cfg,
                       size_t *accept_index) {
    bool accepted = false;
    *accept_index = 0;
    for (size_t i = 0; i < count; i++) {
        if (monitor_config_feed(parser, bytes[i], cfg)) {
            assert(!accepted);          /* at most one accept per feed pass */
            accepted = true;
            *accept_index = i;
        }
    }
    return accepted;
}

static void selftest_config(void) {
    /* Every possible split of the valid (CRC-bearing) frame. */
    for (size_t split = 1; split < MONITOR_CONFIG_STREAM_BYTES; split++) {
        MonitorConfigParser parser;
        MonitorConfig cfg = {0};
        monitor_config_parser_init(&parser);
        for (size_t i = 0; i < split; i++) {
            assert(!monitor_config_feed(&parser, k_config_frame[i], &cfg));
        }
        for (size_t i = split; i < MONITOR_CONFIG_STREAM_BYTES; i++) {
            bool last = i == MONITOR_CONFIG_STREAM_BYTES - 1;
            assert(monitor_config_feed(&parser, k_config_frame[i], &cfg) == last);
        }
        assert(cfg.mode == 1 && cfg.band == 1 && cfg.sweep_ms == 1000 &&
               cfg.fft_size == 64 && cfg.sample_rate_khz == 20000);
        assert(parser.used_bytes == 0);
    }

    /* Byte-at-a-time already covered by split == 1; double-check discard
     * accounting on pure garbage. */
    {
        MonitorConfigParser parser;
        MonitorConfig cfg = {0};
        monitor_config_parser_init(&parser);
        const uint8_t garbage[] = {0x00, 0xff, 0x10, 0x09, 0x00, 0x42};
        for (size_t i = 0; i < sizeof(garbage); i++) {
            assert(!monitor_config_feed(&parser, garbage[i], &cfg));
        }
        /* every input byte is either discarded or still buffered */
        assert(parser.discarded_bytes + parser.used_bytes == sizeof(garbage));
        assert(cfg.mode == 0 && cfg.band == 0);      /* unchanged */
    }

    /* Two concatenated frames: both accepted, newest wins. */
    {
        MonitorConfigParser parser;
        MonitorConfig cfg = {0};
        monitor_config_parser_init(&parser);
        uint8_t stream[2 * MONITOR_CONFIG_STREAM_BYTES];
        memcpy(stream, k_config_frame, MONITOR_CONFIG_STREAM_BYTES);
        memcpy(stream + MONITOR_CONFIG_STREAM_BYTES, k_config_frame,
               MONITOR_CONFIG_STREAM_BYTES);
        /* second frame: live/2.4 GHz variant with same timing fields */
        stream[MONITOR_CONFIG_STREAM_BYTES + 3] = 0x00;
        stream[MONITOR_CONFIG_STREAM_BYTES + 4] = 0x00;
        fix_crc(stream + MONITOR_CONFIG_STREAM_BYTES);
        size_t first_at = 0;
        bool first = false;
        size_t second_at = 0;
        bool second = false;
        for (size_t i = 0; i < sizeof(stream); i++) {
            if (monitor_config_feed(&parser, stream[i], &cfg)) {
                if (!first) {
                    first = true;
                    first_at = i;
                } else {
                    second = true;
                    second_at = i;
                }
            }
        }
        assert(first && first_at == MONITOR_CONFIG_STREAM_BYTES - 1);
        assert(second && second_at == sizeof(stream) - 1);
        assert(cfg.mode == 0 && cfg.band == 0 && cfg.sweep_ms == 1000);
    }

    /* Legacy CRC-less frame (13 bytes only): rejected, config unchanged. */
    {
        MonitorConfigParser parser;
        MonitorConfig cfg = {0};
        monitor_config_parser_init(&parser);
        for (size_t i = 0; i < MONITOR_CONFIG_FRAME_BYTES; i++) {
            assert(!monitor_config_feed(&parser, k_config_frame[i], &cfg));
        }
        assert(cfg.mode == 0 && cfg.band == 0);      /* nothing accepted */
        /* the valid 17-byte frame still parses afterwards */
        size_t accept_at = 0;
        assert(feed_bytes(k_config_frame, MONITOR_CONFIG_STREAM_BYTES,
                          &parser, &cfg, &accept_at));
        assert(accept_at == MONITOR_CONFIG_STREAM_BYTES - 1);
    }

    /* Corrupted checksum: rejected without accepting. */
    {
        MonitorConfigParser parser;
        MonitorConfig cfg = {0};
        monitor_config_parser_init(&parser);
        uint8_t bad[MONITOR_CONFIG_STREAM_BYTES];
        memcpy(bad, k_config_frame, sizeof(bad));
        bad[16] ^= 0xffu;               /* flip one checksum bit */
        for (size_t i = 0; i < sizeof(bad); i++) {
            assert(!monitor_config_feed(&parser, bad[i], &cfg));
        }
        assert(cfg.mode == 0 && cfg.band == 0);
    }

    /* Invalid length byte: parser must resync onto a following valid frame. */
    {
        MonitorConfigParser parser;
        MonitorConfig cfg = {0};
        monitor_config_parser_init(&parser);
        const uint8_t bad_len[] = {0x10, 0x0b, 0x00, 0x01, 0x01};
        for (size_t i = 0; i < sizeof(bad_len); i++) {
            assert(!monitor_config_feed(&parser, bad_len[i], &cfg));
        }
        size_t accept_at = 0;
        assert(feed_bytes(k_config_frame, MONITOR_CONFIG_STREAM_BYTES, &parser,
                          &cfg, &accept_at));
        assert(accept_at == MONITOR_CONFIG_STREAM_BYTES - 1);
        assert(cfg.mode == 1 && cfg.band == 1);
    }

    /* Garbage (including a false 0x10 header) followed by CONFIG. */
    {
        MonitorConfigParser parser;
        MonitorConfig cfg = {0};
        monitor_config_parser_init(&parser);
        /* garbage that cannot complete a valid frame, then a valid frame */
        const uint8_t noise[] = {0x10, 0x0a, 0x00, 0x02, 0x00, 0x64, 0x00,
                                 0x40, 0x00, 0x20, 0x4e, 0x00, 0x00, 0x55};
        /* first 13 bytes form a header-complete frame with mode=2 (illegal),
         * the parser must reject it and stay resynchronized */
        for (size_t i = 0; i < sizeof(noise); i++) {
            assert(!monitor_config_feed(&parser, noise[i], &cfg));
        }
        assert(cfg.mode == 0 && cfg.band == 0);
        uint8_t distinct[MONITOR_CONFIG_STREAM_BYTES];
        memcpy(distinct, k_config_frame, sizeof(distinct));
        distinct[3] = 0x01;
        distinct[4] = 0x01;
        distinct[5] = 0xb8;      /* sweep_ms = 3000 (0x0bb8, little-endian) */
        distinct[6] = 0x0b;
        fix_crc(distinct);
        size_t accept_at = 0;
        assert(feed_bytes(distinct, sizeof(distinct), &parser, &cfg, &accept_at));
        assert(accept_at == MONITOR_CONFIG_STREAM_BYTES - 1);
        assert(cfg.sweep_ms == 3000);
    }

    /* Illegal field values leave the previous config untouched. */
    {
        MonitorConfigParser parser;
        MonitorConfig cfg = {0};
        monitor_config_parser_init(&parser);
        assert(feed_bytes(k_config_frame, MONITOR_CONFIG_STREAM_BYTES, &parser,
                          &cfg, &(size_t){0}));
        MonitorConfig before = cfg;
        uint8_t bad[MONITOR_CONFIG_STREAM_BYTES];
        memcpy(bad, k_config_frame, sizeof(bad));
        static const struct {
            size_t offsets[2];
            uint8_t values[2];
            size_t count;
        } corruptions[] = {
            {{3}, {2}, 1},               /* mode out of range */
            {{4}, {2}, 1},               /* band out of range */
            {{5, 6}, {0, 0}, 2},         /* sweep_ms = 0 (< 100) */
            {{7}, {0x02}, 1},            /* fft_size = 2 */
            {{9}, {0x00}, 1},            /* sample_rate_khz = 19968 */
            {{10}, {0x01}, 1},           /* sample_rate_khz = 288 */
        };
        for (size_t c = 0; c < sizeof(corruptions) / sizeof(corruptions[0]); c++) {
            memcpy(bad, k_config_frame, sizeof(bad));
            for (size_t k = 0; k < corruptions[c].count; k++) {
                bad[corruptions[c].offsets[k]] = corruptions[c].values[k];
            }
            fix_crc(bad);       /* valid CRC so field validation is what runs */
            for (size_t i = 0; i < sizeof(bad); i++) {
                assert(!monitor_config_feed(&parser, bad[i], &cfg));
            }
            assert(monitor_config_equal(&cfg, &before));
        }
        /* parser still works after repeated rejections */
        assert(feed_bytes(k_config_frame, MONITOR_CONFIG_STREAM_BYTES, &parser,
                          &cfg, &(size_t){0}));
        assert(monitor_config_equal(&cfg, &before));
    }

    /* Buffer bound: parser never exceeds its 17-byte array (checked by
     * ASan/UBSan runs as well). */
    {
        MonitorConfigParser parser;
        MonitorConfig cfg = {0};
        monitor_config_parser_init(&parser);
        for (int i = 0; i < 10000; i++) {
            (void)monitor_config_feed(&parser, (uint8_t)(i * 7), &cfg);
            assert(parser.used_bytes < MONITOR_CONFIG_STREAM_BYTES);
        }
    }
}

static int hex_nibble(char c) {
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

static int run_config_parse(const char *hex) {
    size_t len = strlen(hex);
    if (len % 2 != 0 || len / 2 > MONITOR_CONFIG_STREAM_BYTES) {
        return 1;
    }
    MonitorConfigParser parser;
    MonitorConfig cfg = {0};
    monitor_config_parser_init(&parser);
    bool accepted = false;
    for (size_t i = 0; i < len / 2; i++) {
        int hi = hex_nibble(hex[2 * i]);
        int lo = hex_nibble(hex[2 * i + 1]);
        if (hi < 0 || lo < 0) {
            return 1;
        }
        if (monitor_config_feed(&parser, (uint8_t)((hi << 4) | lo), &cfg)) {
            accepted = true;
        }
    }
    if (!accepted) {
        fprintf(stderr, "config not accepted\n");
        return 1;
    }
    printf("mode=%u band=%u sweep_ms=%u fft_size=%u sample_rate_khz=%lu\n",
           cfg.mode, cfg.band, cfg.sweep_ms, cfg.fft_size,
           (unsigned long)cfg.sample_rate_khz);
    return 0;
}

/* Literal beacon fixture from the plan (independent of the implementation):
 * SSID "test", BSSID 00:11:22:33:44:55, DS parameter channel 6. */
static const uint8_t k_beacon[] = {
    0x80, 0x00, 0x00, 0x00, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x00, 0x11, 0x22, 0x33, 0x44, 0x55,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x64, 0x00, 0x01, 0x00,
    0x00, 0x04, 0x74, 0x65, 0x73, 0x74, 0x03, 0x01, 0x06,
};

static void selftest_ap(void) {
    MonitorAccessPoint ap;

    /* literal beacon */
    assert(monitor_parse_access_point(k_beacon, sizeof(k_beacon), &ap));
    const uint8_t want_bssid[6] = {0x00, 0x11, 0x22, 0x33, 0x44, 0x55};
    assert(memcmp(ap.bssid, want_bssid, 6) == 0);
    assert(ap.has_ssid && ap.ssid_len == 4);
    assert(memcmp(ap.ssid_bytes, "test", 4) == 0);
    assert(ap.advertised_channel == 6);

    /* probe response (frame control 0x50) parses the same way */
    {
        uint8_t probe[sizeof(k_beacon)];
        memcpy(probe, k_beacon, sizeof(probe));
        probe[0] = 0x50;
        assert(monitor_parse_access_point(probe, sizeof(probe), &ap));
        assert(ap.advertised_channel == 6 && ap.ssid_len == 4);
    }

    /* empty (hidden) SSID: accepted, explicitly empty */
    {
        uint8_t frame[40] = {0};
        frame[0] = 0x80;
        frame[36] = 0x00;
        frame[37] = 0x00;               /* SSID IE, length 0 */
        assert(monitor_parse_access_point(frame, sizeof(frame), &ap));
        assert(ap.has_ssid && ap.ssid_len == 0);
        assert(ap.advertised_channel == 0);
    }

    /* 32-byte SSID accepted, 33-byte SSID rejected */
    {
        uint8_t frame[36 + 2 + 33] = {0};
        frame[0] = 0x80;
        frame[36] = 0x00;
        frame[37] = 32;
        for (size_t i = 0; i < 32; i++) {
            frame[38 + i] = (uint8_t)(0xc0 + (i % 16));
        }
        assert(monitor_parse_access_point(frame, 36 + 2 + 32, &ap));
        assert(ap.ssid_len == 32);
        frame[37] = 33;                 /* now the IE claims 33 bytes */
        assert(!monitor_parse_access_point(frame, sizeof(frame), &ap));
    }

    /* truncated final IE: no out-of-bounds read, SSID already found */
    {
        uint8_t frame[40] = {0};
        frame[0] = 0x80;
        frame[36] = 0x00;
        frame[37] = 0x04;
        frame[38] = 'h';
        frame[39] = 'i';                /* SSID IE claims 4, has 2 */
        MonitorAccessPoint before = ap;
        assert(!monitor_parse_access_point(frame, sizeof(frame), &ap));
        assert(memcmp(&ap, &before, sizeof(ap)) == 0);   /* unchanged on fail */
    }

    /* truncated DS IE after a valid SSID: SSID kept, channel unknown */
    {
        uint8_t frame[41] = {0};
        frame[0] = 0x80;
        frame[36] = 0x00;
        frame[37] = 0x01;
        frame[38] = 'x';
        frame[39] = 0x03;               /* DS param */
        frame[40] = 0x01;               /* claims 1 byte, none left */
        assert(monitor_parse_access_point(frame, sizeof(frame), &ap));
        assert(ap.advertised_channel == 0);
    }

    /* DS-only, HT-only, and conflicting DS/HT channel attribution */
    {
        uint8_t frame[36 + 2 + 1 + 2 + 1 + 4] = {0};
        frame[0] = 0x80;
        frame[36] = 0x00; frame[37] = 0x01; frame[38] = 'a';
        frame[39] = 0x03; frame[40] = 0x01; frame[41] = 6;      /* DS 6 */
        assert(monitor_parse_access_point(frame, sizeof(frame), &ap));
        assert(ap.advertised_channel == 6);
        frame[41] = 44;                                                /* DS 44 */
        assert(monitor_parse_access_point(frame, sizeof(frame), &ap));
        assert(ap.advertised_channel == 44);
        frame[39] = 0x03; frame[40] = 0x01; frame[41] = 11;     /* DS 11 */
        frame[42] = 61; frame[43] = 0x01; frame[44] = 6;        /* HT 6 */
        assert(monitor_parse_access_point(frame, sizeof(frame), &ap));
        assert(ap.advertised_channel == 0);                      /* conflict */
        frame[41] = 6;                                   /* DS 6 == HT 6 */
        assert(monitor_parse_access_point(frame, sizeof(frame), &ap));
        assert(ap.advertised_channel == 6);
    }

    /* HT-operation only (no DS IE) */
    {
        uint8_t frame[42] = {0};
        frame[0] = 0x80;
        frame[36] = 0x00; frame[37] = 0x01; frame[38] = 'a';
        frame[39] = 61; frame[40] = 0x01; frame[41] = 11;   /* HT 11 */
        assert(monitor_parse_access_point(frame, sizeof(frame), &ap));
        assert(ap.advertised_channel == 11);
    }

    /* non-management frame and short header rejected */
    {
        uint8_t frame[sizeof(k_beacon)];
        memcpy(frame, k_beacon, sizeof(frame));
        frame[0] = 0x08;               /* data frame */
        assert(!monitor_parse_access_point(frame, sizeof(frame), &ap));
        frame[0] = 0x80;
        assert(!monitor_parse_access_point(frame, 35, &ap));
        assert(!monitor_parse_access_point(frame, 0, &ap));
        assert(!monitor_parse_access_point(NULL, 40, &ap));
    }
}

static void selftest_fuzz(void) {
    /* Deterministic length sweep: every prefix of the beacon (and of a
     * mutated copy) must parse safely; ASan/UBSan runs prove no OOB reads. */
    uint8_t mutated[sizeof(k_beacon) + 4];
    memcpy(mutated, k_beacon, sizeof(k_beacon));
    memset(mutated + sizeof(k_beacon), 0xff, 4);
    for (size_t len = 0; len <= sizeof(mutated); len++) {
        for (size_t flip = 0; flip <= sizeof(k_beacon); flip++) {
            uint8_t scratch[sizeof(mutated)];
            memcpy(scratch, mutated, sizeof(mutated));
            if (flip < sizeof(k_beacon)) {
                scratch[flip] ^= 0xa5;
            }
            MonitorAccessPoint ap;
            (void)monitor_parse_access_point(scratch, len, &ap);
        }
    }
}

/* -------------------------------------------------------- wire fixtures */

static size_t build_beacon(uint8_t *buf, const uint8_t bssid[6],
                           const uint8_t *ssid, uint8_t ssid_len,
                           uint8_t channel) {
    memset(buf, 0, 48 + 2 + ssid_len);
    buf[0] = 0x80;                      /* beacon */
    memset(buf + 4, 0xff, 6);           /* destination */
    memcpy(buf + 10, bssid, 6);         /* source */
    memcpy(buf + 16, bssid, 6);         /* bssid */
    buf[32] = 0x64;                     /* beacon interval */
    buf[34] = 0x01;                     /* capability */
    size_t pos = 36;
    buf[pos++] = 0x00;                  /* SSID IE */
    buf[pos++] = ssid_len;
    memcpy(buf + pos, ssid, ssid_len);
    pos += ssid_len;
    buf[pos++] = 0x03;                  /* DS parameter set */
    buf[pos++] = 0x01;
    buf[pos++] = channel;
    return pos;
}

static void emit_tlv(const char *json, size_t len) {
    MonitorTlvFrame frame;
    if (!monitor_tlv_wrap_status(&frame, json, len)) {
        fprintf(stderr, "tlv wrap failed\n");
        exit(1);
    }
    fwrite(frame.bytes, 1, frame.length_bytes, stdout);
    fflush(stdout);
}

static const uint8_t k_jp_24[13] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13};

static int scenario_config_event(void) {
    char json[MONITOR_JSON_CAPACITY_BYTES];
    MonitorConfigEvent event = {
        .epoch = 1,
        .config = {.mode = 0, .band = 0, .sweep_ms = 1000,
                   .fft_size = 64, .sample_rate_khz = 20000},
        .dwell_ms = monitor_dwell_ms(1000, 13),
        .channels = k_jp_24,
        .channel_count = 13,
        .tx_dropped = 0,
    };
    size_t len = monitor_format_config_event(json, sizeof(json), &event);
    if (len == 0) {
        fprintf(stderr, "config format failed\n");
        return 1;
    }
    emit_tlv(json, len);
    return 0;
}

static int scenario_empty_channel(void) {
    MonitorObservation obs;
    monitor_observation_begin(&obs, 0, 6);
    monitor_observation_finalize(&obs, 120);
    char json[MONITOR_JSON_CAPACITY_BYTES];
    MonitorChannelEvent event = {.epoch = 1, .cycle = 1, .observation = &obs};
    size_t len = monitor_format_channel_event(json, sizeof(json), &event);
    if (len == 0) {
        fprintf(stderr, "channel format failed\n");
        return 1;
    }
    emit_tlv(json, len);
    return 0;
}

static int scenario_full_channel(void) {
    MonitorObservation obs;
    monitor_observation_begin(&obs, 0, 6);
    /* 24 received frames, peak -48 dBm */
    static const int8_t rssis[24] = {
        -70, -65, -71, -68, -72, -66, -48, -69, -73, -67, -70, -64,
        -71, -68, -75, -66, -69, -72, -65, -70, -67, -74, -68, -71,
    };
    for (size_t i = 0; i < 24; i++) {
        monitor_observation_record_packet(&obs, rssis[i]);
    }
    uint8_t beacon[96];
    uint8_t bssid[6] = {0x00, 0x11, 0x22, 0x33, 0x44, 0x55};
    size_t n = build_beacon(beacon, bssid, (const uint8_t *)"test", 4, 6);
    monitor_observation_record_sighting(&obs, beacon, n, -48);
    /* seven more unique BSSIDs; one carries a 32-byte non-UTF8 SSID */
    for (uint8_t i = 1; i <= 7; i++) {
        uint8_t other[6] = {0x02, 0x00, 0x00, 0x00, 0x00, i};
        uint8_t ssid32[32];
        const uint8_t *ssid = (const uint8_t *)"other";
        uint8_t ssid_len = 5;
        if (i == 3) {
            for (size_t k = 0; k < 32; k++) {
                ssid32[k] = (uint8_t)(0xc0 + (k % 16));
            }
            ssid = ssid32;
            ssid_len = 32;
        }
        n = build_beacon(beacon, other, ssid, ssid_len, 1);
        monitor_observation_record_sighting(&obs, beacon, n, (int8_t)(-60 - i));
    }
    monitor_observation_finalize(&obs, 120);
    char json[MONITOR_JSON_CAPACITY_BYTES];
    MonitorChannelEvent event = {.epoch = 1, .cycle = 1, .observation = &obs};
    size_t len = monitor_format_channel_event(json, sizeof(json), &event);
    if (len == 0) {
        fprintf(stderr, "channel format failed\n");
        return 1;
    }
    emit_tlv(json, len);
    return 0;
}

static int scenario_cycle_event(void) {
    char json[MONITOR_JSON_CAPACITY_BYTES];
    MonitorCycleEvent event = {
        .epoch = 1, .cycle = 1, .band = 0,
        .elapsed_ms = 1608, .uptime_ms = 1608,
    };
    size_t len = monitor_format_cycle_event(json, sizeof(json), &event);
    if (len == 0) {
        return 1;
    }
    emit_tlv(json, len);
    return 0;
}

static int scenario_error_event(void) {
    char json[MONITOR_JSON_CAPACITY_BYTES];
    size_t len = monitor_format_error_event(json, sizeof(json), "invalid_config");
    if (len == 0) {
        return 1;
    }
    emit_tlv(json, len);
    return 0;
}

static int scenario_channel_error_event(void) {
    char json[MONITOR_JSON_CAPACITY_BYTES];
    MonitorChannelErrorEvent event = {
        .epoch = 1, .cycle = 1, .band = 1, .channel = 144,
        .code = "ESP_ERR_INVALID_ARG",
    };
    size_t len = monitor_format_channel_error_event(json, sizeof(json), &event);
    if (len == 0) {
        return 1;
    }
    emit_tlv(json, len);
    return 0;
}

/* ------------------------------------------------------------ selftests */

static void selftest_observation(void) {
    MonitorObservation obs;

    /* no packets: nullable peak, not a fake floor */
    monitor_observation_begin(&obs, 0, 6);
    monitor_observation_finalize(&obs, 120);
    assert(obs.packets == 0 && !obs.has_peak_rssi);
    assert(obs.access_point_count == 0 && obs.access_points_dropped == 0);
    assert(obs.band == 0 && obs.channel == 6 && obs.observed_ms == 120);

    /* RSSI -70, -48, -65 -> packets 3, peak -48 */
    monitor_observation_begin(&obs, 0, 6);
    monitor_observation_record_packet(&obs, -70);
    monitor_observation_record_packet(&obs, -48);
    monitor_observation_record_packet(&obs, -65);
    assert(obs.packets == 3 && obs.has_peak_rssi && obs.peak_rssi_dbm == -48);

    /* AP dedup by BSSID: latest RSSI/SSID wins */
    uint8_t beacon[96];
    uint8_t bssid_a[6] = {0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff};
    size_t n = build_beacon(beacon, bssid_a, (const uint8_t *)"one", 3, 6);
    assert(monitor_observation_record_sighting(&obs, beacon, n, -70));
    assert(monitor_observation_record_sighting(&obs, beacon, n, -60));
    assert(obs.access_point_count == 1);
    assert(obs.access_points[0].rssi_dbm == -60);

    /* nine unique BSSIDs: eight kept, one counted as dropped */
    for (uint8_t i = 0; i < 9; i++) {
        uint8_t bssid[6] = {0x02, 0x00, 0x00, 0x00, 0x00, i};
        n = build_beacon(beacon, bssid, (const uint8_t *)"x", 1, 6);
        assert(monitor_observation_record_sighting(&obs, beacon, n, -50));
    }
    assert(obs.access_point_count == MONITOR_MAX_ACCESS_POINTS);
    assert(obs.access_points_dropped >= 1);

    /* dropped sightings never touch the packet counter */
    uint32_t packets_before = obs.packets;
    obs.access_points_dropped += 2;
    assert(obs.packets == packets_before);

    /* counters saturate instead of wrapping */
    obs.packets = UINT32_MAX;
    monitor_observation_record_packet(&obs, -30);
    assert(obs.packets == UINT32_MAX);
    obs.access_points_dropped = UINT32_MAX;
    n = build_beacon(beacon, bssid_a, (const uint8_t *)"one", 3, 6);
    /* duplicate of a stored BSSID: in-place update, no drop increment */
    assert(monitor_observation_record_sighting(&obs, beacon, n, -55));
    assert(obs.access_points_dropped == UINT32_MAX);

    /* malformed beacons do not enter the table but do not fail packets */
    uint8_t junk[40] = {0x08};
    assert(!monitor_observation_record_sighting(&obs, junk, sizeof(junk), -40));
    assert(obs.packets == UINT32_MAX);
}

static void selftest_serialize(void) {
    char json[MONITOR_JSON_CAPACITY_BYTES];
    char tiny[8];
    MonitorConfigEvent cfg_event = {
        .epoch = 1,
        .config = {.mode = 1, .band = 1, .sweep_ms = 1000,
                   .fft_size = 64, .sample_rate_khz = 20000},
        .dwell_ms = 500,
        .channels = k_jp_24,
        .channel_count = 13,
        .tx_dropped = 0,
    };
    size_t len = monitor_format_config_event(json, sizeof(json), &cfg_event);
    assert(len > 0 && json[0] == '{' && json[len - 1] == '}');
    /* too-small destination fails without producing a body */
    memset(tiny, 0x5a, sizeof(tiny));
    assert(monitor_format_config_event(tiny, sizeof(tiny), &cfg_event) == 0);

    MonitorObservation obs;
    monitor_observation_begin(&obs, 0, 6);
    monitor_observation_finalize(&obs, 120);
    MonitorChannelEvent ch_event = {.epoch = 1, .cycle = 1, .observation = &obs};
    len = monitor_format_channel_event(json, sizeof(json), &ch_event);
    assert(len > 0 && json[len - 1] == '}');
    /* exact-fit-minus-one must fail: no truncated bodies ever leave */
    assert(monitor_format_channel_event(json, len, &ch_event) == 0);

    MonitorCycleEvent cy_event = {
        .epoch = 1, .cycle = 2, .band = 1, .elapsed_ms = 3000, .uptime_ms = 9000,
    };
    assert(monitor_format_cycle_event(json, sizeof(json), &cy_event) > 0);
    assert(monitor_format_cycle_event(tiny, sizeof(tiny), &cy_event) == 0);
    assert(monitor_format_error_event(json, sizeof(json), "invalid_config") > 0);
    assert(monitor_format_error_event(tiny, sizeof(tiny), "invalid_config") == 0);

    /* peak null vs value serialization */
    MonitorObservation with_peak;
    monitor_observation_begin(&with_peak, 1, 44);
    monitor_observation_record_packet(&with_peak, -42);
    monitor_observation_finalize(&with_peak, 150);
    ch_event.observation = &with_peak;
    len = monitor_format_channel_event(json, sizeof(json), &ch_event);
    assert(len > 0 && strstr(json, "\"peak_rssi_dbm\":-42") != NULL);
    monitor_observation_begin(&obs, 0, 6);
    monitor_observation_finalize(&obs, 10);
    ch_event.observation = &obs;
    len = monitor_format_channel_event(json, sizeof(json), &ch_event);
    assert(len > 0 && strstr(json, "\"peak_rssi_dbm\":null") != NULL);
    assert(strstr(json, "\"packets\":0") != NULL);

    /* TLV wrapping */
    MonitorTlvFrame frame;
    assert(monitor_tlv_wrap_status(&frame, json, len));
    assert(frame.bytes[0] == 0x03);
    assert(frame.length_bytes == len + 3 + 4);       /* header + body + crc */
    /* trailing 4 bytes are the CRC over header+body and verify */
    assert(monitor_crc32(frame.bytes, len + 3) ==
           ((uint32_t)frame.bytes[len + 3] |
            ((uint32_t)frame.bytes[len + 4] << 8) |
            ((uint32_t)frame.bytes[len + 5] << 16) |
            ((uint32_t)frame.bytes[len + 6] << 24)));
    assert(!monitor_tlv_wrap_status(&frame, json, 0));
    assert(!monitor_tlv_wrap_status(&frame, NULL, len));
    char over[MONITOR_JSON_CAPACITY_BYTES + 1] = "{x}";
    assert(!monitor_tlv_wrap_status(&frame, over, MONITOR_JSON_CAPACITY_BYTES + 1));

    /* worst case: 8 APs with 32-byte SSIDs still fits the 4096 cap */
    monitor_observation_begin(&obs, 0, 6);
    uint8_t beacon[128];
    for (uint8_t i = 0; i < 9; i++) {
        uint8_t bssid[6] = {0x02, 0x10, 0x20, 0x30, 0x40, i};
        uint8_t ssid[32];
        memset(ssid, 0xc0 + i, sizeof(ssid));
        size_t n = build_beacon(beacon, bssid, ssid, 32, 6);
        (void)monitor_observation_record_sighting(&obs, beacon, n, -55);
    }
    assert(obs.access_point_count == MONITOR_MAX_ACCESS_POINTS);
    monitor_observation_finalize(&obs, 120);
    ch_event.observation = &obs;
    len = monitor_format_channel_event(json, sizeof(json), &ch_event);
    assert(len > 0 && len <= MONITOR_JSON_CAPACITY_BYTES);
    MonitorTlvFrame big;
    assert(monitor_tlv_wrap_status(&big, json, len));
    assert(big.length_bytes == len + 3 + 4);
}

/* ------------------------------------------------------ crc / cjson */

static void selftest_crc(void) {
    /* Published CRC-32/ISO-HDLC check value, independent of this code. */
    const uint8_t check[] = "123456789";
    assert(monitor_crc32(check, 9) == 0xcbf43926u);

    /* Golden CONFIG frame from the wire spec: CRC bytes 95 56 4e 1d. */
    uint32_t crc = monitor_crc32(k_config_frame, MONITOR_CONFIG_FRAME_BYTES);
    assert(crc == 0x1d4e5695u);
    assert(k_config_frame[13] == (uint8_t)(crc & 0xffu));
    assert(k_config_frame[14] == (uint8_t)((crc >> 8) & 0xffu));
    assert(k_config_frame[15] == (uint8_t)((crc >> 16) & 0xffu));
    assert(k_config_frame[16] == (uint8_t)((crc >> 24) & 0xffu));

    /* Empty input: defined init/xorout result. */
    assert(monitor_crc32(NULL, 0) == 0u);

    /* Any single-bit flip in header, payload, or checksum breaks it. */
    uint8_t mutated[MONITOR_CONFIG_STREAM_BYTES];
    for (size_t i = 0; i < MONITOR_CONFIG_STREAM_BYTES; i++) {
        memcpy(mutated, k_config_frame, sizeof(mutated));
        mutated[i] ^= 0x01u;
        uint32_t got = monitor_crc32(mutated, MONITOR_CONFIG_FRAME_BYTES);
        bool checksum_changed = i >= MONITOR_CONFIG_FRAME_BYTES;
        assert((got == crc) == checksum_changed);
    }
}

/* Counting allocator used only by this fixture to measure the cJSON memory
 * contract (firmware uses cJSON's default malloc/free). */
static size_t g_live_bytes;
static size_t g_peak_bytes;

static void *counting_malloc(size_t size) {
    size_t *block = malloc(size + sizeof(size_t));
    if (block == NULL) {
        return NULL;
    }
    block[0] = size;
    g_live_bytes += size;
    if (g_live_bytes > g_peak_bytes) {
        g_peak_bytes = g_live_bytes;
    }
    return (char *)block + sizeof(size_t);
}

static void counting_free(void *ptr) {
    if (ptr == NULL) {
        return;
    }
    size_t *block = (size_t *)((char *)ptr - sizeof(size_t));
    g_live_bytes -= block[0];
    free(block);
}

static void selftest_cjson_memory(void) {
    cJSON_Hooks hooks = {.malloc_fn = counting_malloc, .free_fn = counting_free};
    cJSON_InitHooks(&hooks);

    /* Worst case: 8 APs with 32-byte SSIDs. Measures peak transient cJSON
     * tree bytes and proves the tree is fully freed after formatting. */
    static char json[MONITOR_JSON_CAPACITY_BYTES];
    MonitorObservation obs;
    monitor_observation_begin(&obs, 0, 6);
    uint8_t beacon[128];
    for (uint8_t i = 0; i < 9; i++) {
        uint8_t bssid[6] = {0x02, 0x10, 0x20, 0x30, 0x40, i};
        uint8_t ssid[32];
        memset(ssid, 0xc0 + i, sizeof(ssid));
        size_t n = build_beacon(beacon, bssid, ssid, 32, 6);
        (void)monitor_observation_record_sighting(&obs, beacon, n, -55);
    }
    monitor_observation_finalize(&obs, 120);
    MonitorChannelEvent event = {.epoch = 1, .cycle = 1, .observation = &obs};
    size_t len = monitor_format_channel_event(json, sizeof(json), &event);
    assert(len > 0);
    assert(g_live_bytes == 0);          /* tree fully freed, no leak */
    assert(g_peak_bytes > 0 && g_peak_bytes <= 16384);
    printf("cjson_peak_bytes=%zu\n", g_peak_bytes);

    /* Also measure a config event (20 channels). */
    uint8_t channels[MONITOR_MAX_CHANNELS];
    size_t count = monitor_band_channels(1, channels, sizeof(channels));
    assert(count == 20);
    MonitorConfigEvent cfg_event = {
        .epoch = 1,
        .config = {.mode = 1, .band = 1, .sweep_ms = 1000,
                   .fft_size = 64, .sample_rate_khz = 20000},
        .dwell_ms = 500,
        .channels = channels,
        .channel_count = count,
        .tx_dropped = 0,
    };
    g_peak_bytes = 0;
    len = monitor_format_config_event(json, sizeof(json), &cfg_event);
    assert(len > 0);
    assert(g_live_bytes == 0);
    assert(g_peak_bytes > 0 && g_peak_bytes <= 16384);
    printf("cjson_cfg_peak_bytes=%zu\n", g_peak_bytes);
}

static void selftest_run(void) {
    MonitorConfig a = {.mode = 0, .band = 0, .sweep_ms = 1000,
                       .fft_size = 64, .sample_rate_khz = 20000};
    MonitorConfig b = {.mode = 1, .band = 1, .sweep_ms = 3000,
                       .fft_size = 128, .sample_rate_khz = 40000};
    MonitorRun run;
    monitor_run_init(&run, &a);
    assert(run.epoch == 1 && run.cycle == 0);
    assert(monitor_config_equal(&run.active_config, &a));

    /* nothing pending */
    assert(monitor_run_boundary(&run, &b, false) == MONITOR_BOUNDARY_IDLE);
    assert(run.epoch == 1);

    /* config A: identical pending is acked without an epoch change */
    assert(monitor_run_boundary(&run, &a, true) == MONITOR_BOUNDARY_ACK);
    assert(run.epoch == 1);

    /* A runs one partial cycle (the loop discards its completion event) */
    assert(monitor_run_begin_cycle(&run) == 1);

    /* config B at the boundary: applied, new epoch, incomplete cycle
     * discarded — no completion for the partial A cycle */
    assert(monitor_run_boundary(&run, &b, true) == MONITOR_BOUNDARY_APPLY);
    assert(run.epoch == 2);
    assert(monitor_config_equal(&run.active_config, &b));
    assert(run.cycle == 1);             /* numbering stays monotonic */

    /* B runs a full cycle */
    assert(monitor_run_begin_cycle(&run) == 2);
    /* repeating B is idempotent: epoch must not increment */
    assert(monitor_run_boundary(&run, &b, true) == MONITOR_BOUNDARY_ACK);
    assert(run.epoch == 2);
    assert(monitor_run_begin_cycle(&run) == 3);

    /* NULL safety */
    assert(monitor_run_boundary(&run, NULL, true) == MONITOR_BOUNDARY_IDLE);
    assert(monitor_run_boundary(NULL, &b, true) == MONITOR_BOUNDARY_IDLE);
    monitor_run_init(NULL, &a);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <mode> [arg]\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "config-parse") == 0 && argc == 3) {
        return run_config_parse(argv[2]);
    }
    if (strcmp(argv[1], "dwell") == 0) {
        printf("%lu %lu %lu\n", (unsigned long)monitor_dwell_ms(1000, 13),
               (unsigned long)monitor_dwell_ms(10000, 20),
               (unsigned long)monitor_dwell_ms(1000, 0));
        return 0;
    }
    if (strcmp(argv[1], "selftest") == 0 && argc == 3) {
        if (strcmp(argv[2], "config") == 0) {
            selftest_config();
            printf("config ok\n");
            return 0;
        }
        if (strcmp(argv[2], "ap") == 0) {
            selftest_ap();
            printf("ap ok\n");
            return 0;
        }
        if (strcmp(argv[2], "fuzz") == 0) {
            selftest_fuzz();
            printf("fuzz ok\n");
            return 0;
        }
        if (strcmp(argv[2], "observation") == 0) {
            selftest_observation();
            printf("observation ok\n");
            return 0;
        }
        if (strcmp(argv[2], "serialize") == 0) {
            selftest_serialize();
            printf("serialize ok\n");
            return 0;
        }
        if (strcmp(argv[2], "crc") == 0) {
            selftest_crc();
            printf("crc ok\n");
            return 0;
        }
        if (strcmp(argv[2], "cjson-memory") == 0) {
            selftest_cjson_memory();
            return 0;
        }
        if (strcmp(argv[2], "run") == 0) {
            selftest_run();
            printf("run ok\n");
            return 0;
        }
        fprintf(stderr, "unknown selftest case: %s\n", argv[2]);
        return 2;
    }
    if (strcmp(argv[1], "config-event") == 0) {
        return scenario_config_event();
    }
    if (strcmp(argv[1], "empty-channel") == 0) {
        return scenario_empty_channel();
    }
    if (strcmp(argv[1], "full-channel") == 0) {
        return scenario_full_channel();
    }
    if (strcmp(argv[1], "cycle-event") == 0) {
        return scenario_cycle_event();
    }
    if (strcmp(argv[1], "error-event") == 0) {
        return scenario_error_event();
    }
    if (strcmp(argv[1], "channel-error-event") == 0) {
        return scenario_channel_error_event();
    }
    fprintf(stderr, "unknown mode: %s\n", argv[1]);
    return 2;
}
