#include "monitor_core.h"

#include <cJSON.h>
#include <string.h>

static uint16_t read_le16(const uint8_t *bytes) {
    return (uint16_t)bytes[0] | ((uint16_t)bytes[1] << 8);
}

static uint32_t read_le32(const uint8_t *bytes) {
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8) |
           ((uint32_t)bytes[2] << 16) | ((uint32_t)bytes[3] << 24);
}

static void write_le32(uint8_t *bytes, uint32_t value) {
    bytes[0] = (uint8_t)(value & 0xffu);
    bytes[1] = (uint8_t)((value >> 8) & 0xffu);
    bytes[2] = (uint8_t)((value >> 16) & 0xffu);
    bytes[3] = (uint8_t)((value >> 24) & 0xffu);
}

static void drain_one(MonitorConfigParser *parser) {
    memmove(parser->frame_bytes, parser->frame_bytes + 1,
            parser->used_bytes - 1);
    parser->used_bytes--;
    parser->discarded_bytes++;
}

static bool has_valid_values(const MonitorConfig *config) {
    bool fft_ok = config->fft_size == 64 || config->fft_size == 128 ||
                  config->fft_size == 256 || config->fft_size == 512 ||
                  config->fft_size == 1024;
    bool rate_ok = config->sample_rate_khz == 20000 ||
                   config->sample_rate_khz == 40000;
    bool dwell_ok = config->channel_dwell_ms == 0 ||
                    (config->channel_dwell_ms >=
                         MONITOR_CHANNEL_DWELL_EXPLICIT_MIN_MS &&
                     config->channel_dwell_ms <=
                         MONITOR_CHANNEL_DWELL_EXPLICIT_MAX_MS);
    bool attempts_ok = config->cca_attempts >= MONITOR_CCA_ATTEMPTS_MIN &&
                       config->cca_attempts <= MONITOR_CCA_ATTEMPTS_MAX;
    return config->mode <= 1 && config->band <= 1 &&
           config->sweep_ms >= 100 && config->sweep_ms <= 10000 &&
           fft_ok && rate_ok && dwell_ok && attempts_ok;
}

bool monitor_config_is_valid(const MonitorConfig *config) {
    if (config == NULL) {
        return false;
    }
    return has_valid_values(config);
}

bool monitor_config_equal(const MonitorConfig *a, const MonitorConfig *b) {
    if (a == NULL || b == NULL) {
        return false;
    }
    return a->mode == b->mode && a->band == b->band &&
           a->sweep_ms == b->sweep_ms && a->fft_size == b->fft_size &&
           a->sample_rate_khz == b->sample_rate_khz &&
           a->channel_dwell_ms == b->channel_dwell_ms &&
           a->cca_attempts == b->cca_attempts;
}

void monitor_config_parser_init(MonitorConfigParser *parser) {
    if (parser == NULL) {
        return;
    }
    parser->used_bytes = 0;
    parser->discarded_bytes = 0;
}

uint32_t monitor_crc32(const uint8_t *data, size_t length_bytes) {
    if (data == NULL && length_bytes != 0) {
        return 0;
    }
    uint32_t crc = 0xffffffffu;
    for (size_t i = 0; i < length_bytes; i++) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; bit++) {
            /* reflected polynomial 0xEDB88320 (CRC-32/ISO-HDLC) */
            uint32_t mask = (uint32_t)-(int32_t)(crc & 1u);
            crc = (crc >> 1) ^ (0xedb88320u & mask);
        }
    }
    return crc ^ 0xffffffffu;
}

bool monitor_config_feed(MonitorConfigParser *parser, uint8_t byte,
                         MonitorConfig *out_config) {
    if (parser == NULL || out_config == NULL) {
        return false;
    }
    if (parser->used_bytes >= MONITOR_CONFIG_STREAM_BYTES) {
        /* Unreachable while the drain loop below runs, but never overflow. */
        parser->used_bytes = 0;
        parser->discarded_bytes++;
    }
    parser->frame_bytes[parser->used_bytes++] = byte;
    for (;;) {
        if (parser->used_bytes < 3) {
            return false;
        }
        bool header_ok = parser->frame_bytes[0] == 0x10 &&
                         parser->frame_bytes[1] == MONITOR_CONFIG_PAYLOAD_BYTES &&
                         parser->frame_bytes[2] == 0;
        if (!header_ok) {
            drain_one(parser);
            continue;
        }
        if (parser->used_bytes < MONITOR_CONFIG_STREAM_BYTES) {
            return false;               /* wait for header + payload + crc */
        }
        /* Checksum first: no field is read from an unverified frame. */
        uint32_t crc = monitor_crc32(parser->frame_bytes,
                                     MONITOR_CONFIG_FRAME_BYTES);
        uint32_t wire_crc = read_le32(parser->frame_bytes +
                                      MONITOR_CONFIG_FRAME_BYTES);
        if (crc != wire_crc) {
            drain_one(parser);          /* reject legacy/corrupt, resync */
            continue;
        }
        MonitorConfig next = {
            .mode = parser->frame_bytes[3],
            .band = parser->frame_bytes[4],
            .sweep_ms = read_le16(parser->frame_bytes + 5),
            .fft_size = read_le16(parser->frame_bytes + 7),
            .sample_rate_khz = read_le32(parser->frame_bytes + 9),
            /* appended: payload offsets 10 (u16) and 12 (u8) */
            .channel_dwell_ms = read_le16(parser->frame_bytes + 13),
            .cca_attempts = parser->frame_bytes[15],
        };
        if (has_valid_values(&next)) {
            *out_config = next;
            parser->used_bytes = 0;
            return true;
        }
        drain_one(parser);              /* checksum ok, fields illegal */
    }
}

uint32_t monitor_dwell_ms(const MonitorConfig *config, size_t channel_count) {
    if (config == NULL || channel_count == 0) {
        return 0;
    }
    uint32_t base;
    if (config->channel_dwell_ms != 0) {
        base = config->channel_dwell_ms;        /* explicit override */
    } else {
        base = ((uint32_t)config->sweep_ms + (uint32_t)channel_count - 1u) /
               (uint32_t)channel_count;        /* ceil(sweep / channels) */
    }
    uint32_t dwell = base < MONITOR_MIN_DWELL_MS ? MONITOR_MIN_DWELL_MS : base;
    uint32_t cca_budget_ms = 5u * (uint32_t)config->cca_attempts;
    return cca_budget_ms > dwell ? cca_budget_ms : dwell;
}

/* JP conservative receive allowlist. Channel 14 excluded (NO-OFDM needs a
 * separate policy); 149-177 excluded from this first version. Upper bound
 * only: IDF may reject individual channels at runtime (channel_error). */
static const uint8_t k_jp_24_channels[] = {
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13,
};

static const uint8_t k_jp_5_channels[] = {
    36, 40, 44, 48, 52, 56, 60, 64,
    100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
};

size_t monitor_band_channels(uint8_t band, uint8_t *out_channels,
                             size_t capacity) {
    const uint8_t *src;
    size_t count;
    if (band == 0) {
        src = k_jp_24_channels;
        count = sizeof(k_jp_24_channels);
    } else if (band == 1) {
        src = k_jp_5_channels;
        count = sizeof(k_jp_5_channels);
    } else {
        return 0;
    }
    if (out_channels == NULL && capacity != 0) {
        return 0;
    }
    size_t copy_count = count < capacity ? count : capacity;
    for (size_t i = 0; i < copy_count; i++) {
        out_channels[i] = src[i];
    }
    return count;
}

uint32_t monitor_channel_center_khz(uint8_t band, uint8_t channel) {
    uint8_t channels[MONITOR_MAX_CHANNELS];
    size_t count = monitor_band_channels(band, channels, sizeof(channels));
    bool member = false;
    for (size_t i = 0; i < count; i++) {
        if (channels[i] == channel) {
            member = true;
            break;
        }
    }
    if (!member) {
        return 0;
    }
    /* 2.4 GHz: ch 1 -> 2412000 kHz; 5 GHz JP list: ch 36 -> 5180000 kHz. */
    return band == 0 ? 2407000u + 5000u * channel
                     : 5000000u + 5000u * channel;
}

bool monitor_parse_access_point(const uint8_t *frame, size_t frame_length_bytes,
                                MonitorAccessPoint *out) {
    if (frame == NULL || out == NULL || frame_length_bytes < 36) {
        return false;
    }
    uint8_t frame_type = frame[0];
    if (frame_type != 0x80 && frame_type != 0x50) {
        return false;
    }
    MonitorAccessPoint ap;
    memset(&ap, 0, sizeof(ap));
    memcpy(ap.bssid, frame + 16, 6);
    uint8_t ds_channel = 0;
    uint8_t ht_channel = 0;
    /* Information elements start after the 24-byte MAC header and the
     * 12-byte fixed beacon/probe-response parameters. */
    for (size_t pos = 36; pos + 2 <= frame_length_bytes;) {
        uint8_t ie_id = frame[pos];
        uint8_t ie_len = frame[pos + 1];
        pos += 2;
        if ((size_t)ie_len > frame_length_bytes - pos) {
            break;                      /* truncated IE: stop, stay in bounds */
        }
        if (ie_id == 0 && !ap.has_ssid && ie_len <= MONITOR_MAX_SSID_BYTES) {
            memcpy(ap.ssid_bytes, frame + pos, ie_len);
            ap.ssid_len = ie_len;
            ap.has_ssid = true;
        } else if (ie_id == 3 && ie_len == 1) {
            ds_channel = frame[pos];
        } else if (ie_id == 61 && ie_len >= 1) {
            ht_channel = frame[pos];
        }
        pos += ie_len;
    }
    if (!ap.has_ssid) {
        return false;                   /* missing or oversized SSID IE */
    }
    if (ds_channel != 0 && ht_channel != 0 && ds_channel != ht_channel) {
        ap.advertised_channel = 0;      /* conflict: unknown, not a guess */
    } else {
        ap.advertised_channel = ds_channel != 0 ? ds_channel : ht_channel;
    }
    *out = ap;
    return true;
}

void monitor_observation_begin(MonitorObservation *observation, uint8_t band,
                               uint8_t channel) {
    if (observation == NULL) {
        return;
    }
    memset(observation, 0, sizeof(*observation));
    observation->band = band;
    observation->channel = channel;
}

void monitor_observation_record_packet(MonitorObservation *observation,
                                       int8_t rssi_dbm) {
    if (observation == NULL) {
        return;
    }
    if (observation->packets < UINT32_MAX) {
        observation->packets++;
    }
    if (!observation->has_peak_rssi || rssi_dbm > observation->peak_rssi_dbm) {
        observation->peak_rssi_dbm = rssi_dbm;
        observation->has_peak_rssi = true;
    }
}

bool monitor_observation_record_sighting(MonitorObservation *observation,
                                         const uint8_t *frame,
                                         size_t frame_length_bytes,
                                         int8_t rssi_dbm) {
    if (observation == NULL) {
        return false;
    }
    MonitorAccessPoint parsed;
    if (!monitor_parse_access_point(frame, frame_length_bytes, &parsed)) {
        return false;
    }
    for (uint8_t i = 0; i < observation->access_point_count; i++) {
        MonitorAccessPoint *stored = &observation->access_points[i];
        if (memcmp(stored->bssid, parsed.bssid, 6) == 0) {
            stored->ssid_len = parsed.ssid_len;
            stored->has_ssid = parsed.has_ssid;
            memcpy(stored->ssid_bytes, parsed.ssid_bytes, parsed.ssid_len);
            stored->advertised_channel = parsed.advertised_channel;
            stored->rssi_dbm = rssi_dbm;         /* latest sighting wins */
            return true;
        }
    }
    if (observation->access_point_count >= MONITOR_MAX_ACCESS_POINTS) {
        observation->access_points_dropped++;
        return true;                             /* parsed but not stored */
    }
    parsed.rssi_dbm = rssi_dbm;
    observation->access_points[observation->access_point_count] = parsed;
    observation->access_point_count++;
    return true;
}

void monitor_observation_finalize(MonitorObservation *observation,
                                  uint32_t observed_ms) {
    if (observation == NULL) {
        return;
    }
    observation->observed_ms = observed_ms;
}

/* ------------------------------------------------------------- formatting
 * JSON is produced with the official espressif/cjson component (v1.7.19,
 * locked in firmware/dependencies.lock). Memory contract — this is the
 * documented runtime-allocation exception under CODING_GUIDELINE 1.3:
 *   - One transient cJSON tree per emitted event, built in application-task
 *     context only (never in the Wi-Fi callback). The tree is bounded by the
 *     fixed event shape: <= MONITOR_MAX_CHANNELS channel numbers,
 *     <= MONITOR_MAX_ACCESS_POINTS AP records, strings <= 64 hex chars.
 *     The host fixture measures the worst case (selftest "cjson-memory").
 *   - The printed body goes into the caller's static buffer via
 *     cJSON_PrintPreallocated; the emitted string is never heap-allocated.
 *   - Every create/add/print call is checked. On any failure the event is
 *     discarded whole (return 0) and cJSON_Delete frees the partial tree:
 *     all-or-nothing emission, allocation exhaustion only drops events.
 *   - No allocation happens in the parser, observation, CRC, or TLV paths.
 */

static void hex_bytes(char *out, const uint8_t *src, size_t n) {
    static const char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {
        out[2 * i] = digits[src[i] >> 4];
        out[2 * i + 1] = digits[src[i] & 15];
    }
    out[2 * n] = '\0';
}

static bool json_add_number(cJSON *object, const char *name, double value) {
    if (object == NULL) {
        return false;
    }
    cJSON *node = cJSON_CreateNumber(value);
    if (node == NULL) {
        return false;
    }
    if (!cJSON_AddItemToObject(object, name, node)) {
        cJSON_Delete(node);
        return false;
    }
    return true;
}

static bool json_add_string(cJSON *object, const char *name,
                            const char *value) {
    if (object == NULL) {
        return false;
    }
    cJSON *node = cJSON_CreateString(value);
    if (node == NULL) {
        return false;
    }
    if (!cJSON_AddItemToObject(object, name, node)) {
        cJSON_Delete(node);
        return false;
    }
    return true;
}

static bool json_add_bool(cJSON *object, const char *name, bool value) {
    if (object == NULL) {
        return false;
    }
    cJSON *node = cJSON_CreateBool(value);
    if (node == NULL) {
        return false;
    }
    if (!cJSON_AddItemToObject(object, name, node)) {
        cJSON_Delete(node);
        return false;
    }
    return true;
}

static bool json_add_null(cJSON *object, const char *name) {
    if (object == NULL) {
        return false;
    }
    cJSON *node = cJSON_CreateNull();
    if (node == NULL) {
        return false;
    }
    if (!cJSON_AddItemToObject(object, name, node)) {
        cJSON_Delete(node);
        return false;
    }
    return true;
}

/* Attaches a freshly created child. Ownership moves to the parent on
 * success; on failure the child is freed here (no leaks on exhaustion). */
static bool json_attach(cJSON *parent, const char *name, cJSON *child) {
    if (parent == NULL || child == NULL) {
        cJSON_Delete(child);
        return false;
    }
    if (!cJSON_AddItemToObject(parent, name, child)) {
        cJSON_Delete(child);
        return false;
    }
    return true;
}

static bool json_attach_array(cJSON *array, cJSON *child) {
    if (array == NULL || child == NULL) {
        cJSON_Delete(child);
        return false;
    }
    if (!cJSON_AddItemToArray(array, child)) {
        cJSON_Delete(child);
        return false;
    }
    return true;
}

static bool json_add_array(cJSON *object, const char *name,
                           cJSON **out_array) {
    cJSON *node = cJSON_CreateArray();
    if (!json_attach(object, name, node)) {
        return false;
    }
    *out_array = node;
    return true;
}

static bool json_array_add_number(cJSON *array, double value) {
    if (array == NULL) {
        return false;
    }
    cJSON *node = cJSON_CreateNumber(value);
    if (node == NULL) {
        return false;
    }
    if (!cJSON_AddItemToArray(array, node)) {
        cJSON_Delete(node);
        return false;
    }
    return true;
}

static bool json_add_object(cJSON *object, const char *name, cJSON **out) {
    cJSON *node = cJSON_CreateObject();
    if (!json_attach(object, name, node)) {
        return false;
    }
    *out = node;
    return true;
}

/* One rate_caps entry: {"code": k, "span_khz": v} (docs/tlv-protocol.md;
 * hosts match rate_code against `code`, never array position). */
static bool json_array_add_rate_code(cJSON *array, double code,
                                     double span_khz) {
    if (array == NULL) {
        return false;
    }
    cJSON *node = cJSON_CreateObject();
    if (node == NULL) {
        return false;
    }
    bool ok = json_add_number(node, "code", code) &&
              json_add_number(node, "span_khz", span_khz);
    if (!ok) {
        cJSON_Delete(node);
        return false;
    }
    if (!json_attach_array(array, node)) {
        return false;                       /* attach frees on failure */
    }
    return true;
}

/* Print the tree into dst (all-or-nothing) and free it. Returns the body
 * length or 0 on any failure; dst is unspecified when 0 is returned. */
static size_t json_print_and_free(cJSON *root, bool ok, char *dst,
                                  size_t capacity_bytes) {
    if (root == NULL) {
        return 0;
    }
    size_t bound = capacity_bytes;
    if (bound > MONITOR_JSON_CAPACITY_BYTES + 1u) {
        bound = MONITOR_JSON_CAPACITY_BYTES + 1u;   /* body cap incl. NUL */
    }
    size_t length_bytes = 0;
    if (ok && bound > 1u &&
        cJSON_PrintPreallocated(root, dst, (int)bound, false)) {
        length_bytes = strlen(dst);
    }
    cJSON_Delete(root);
    return length_bytes;
}

size_t monitor_format_config_event(char *dst, size_t capacity_bytes,
                                   const MonitorConfigEvent *event) {
    if (dst == NULL || event == NULL || event->channels == NULL ||
        event->channel_count == 0 ||
        event->channel_count > MONITOR_MAX_CHANNELS) {
        return 0;
    }
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return 0;
    }
    bool ok = json_add_string(root, "schema", "wifi-monitor/1");
    ok = ok && json_add_string(root, "event", "config");
    ok = ok && json_add_string(root, "fw", "wifi-monitor-0.1");
    ok = ok && json_add_string(root, "idf", "v6.0.3");
    ok = ok && json_add_string(root, "chip", "ESP32-C5");
    ok = ok && json_add_string(root, "country", "JP");
    ok = ok && json_add_number(root, "epoch", event->epoch);
    ok = ok && json_add_number(root, "mode", event->config.mode);
    ok = ok && json_add_number(root, "band", event->config.band);
    ok = ok && json_add_number(root, "sweep_ms", event->config.sweep_ms);
    ok = ok && json_add_number(root, "fft_size", event->config.fft_size);
    ok = ok && json_add_number(root, "sample_rate_khz",
                               event->config.sample_rate_khz);
    /* ONE v1 CONFIG: echo BOTH requested new fields, then the effective
     * dwell (ack/retry comparison uses all seven requested fields). */
    ok = ok && json_add_number(root, "channel_dwell_ms",
                               event->config.channel_dwell_ms);
    ok = ok && json_add_number(root, "cca_attempts",
                               event->config.cca_attempts);
    ok = ok && json_add_number(root, "dwell_ms", event->dwell_ms);
    cJSON *channels = NULL;
    ok = ok && json_add_array(root, "channels", &channels);
    for (size_t i = 0; ok && i < event->channel_count; i++) {
        ok = json_array_add_number(channels, event->channels[i]);
    }
    /* Spectrum capability block (docs/tlv-protocol.md):
     * booleans are truth, the object fields are the effective values the
     * firmware will actually produce — never a fabricated echo. */
    ok = ok && json_add_bool(root, "spectrum", event->spectrum_available);
    ok = ok && json_add_bool(root, "cca", event->utilization_available);
    ok = ok && json_add_bool(root, "fft_supported", event->spectrum_available);
    if (ok && event->spectrum_available) {
        cJSON *caps = NULL;
        ok = json_add_object(root, "spectrum_caps", &caps);
        ok = ok && json_add_string(caps, "source", "c5_snapshot_iq_fft");
        cJSON *sizes = NULL;
        ok = ok && json_add_array(caps, "fft_sizes", &sizes);
        static const uint16_t k_fft_sizes[] = {64, 128, 256, 512, 1024};
        for (size_t i = 0; ok && i < sizeof(k_fft_sizes) / sizeof(k_fft_sizes[0]);
             i++) {
            ok = json_array_add_number(sizes, k_fft_sizes[i]);
        }
        cJSON *rates = NULL;
        ok = ok && json_add_array(caps, "rate_codes", &rates);
        /* Proven-subset order is irrelevant; hosts match on code. */
        ok = ok && json_array_add_rate_code(rates, 1, 40000);
        ok = ok && json_array_add_rate_code(rates, 2, 20000);
        ok = ok && json_add_string(caps, "bin_unit", "centi_dbfs");
        cJSON *effective = NULL;
        ok = ok && json_add_object(root, "spectrum_effective", &effective);
        ok = ok && json_add_number(effective, "fft_size",
                                   event->effective_fft_size);
        ok = ok && json_add_number(effective, "rate_code",
                                   event->effective_rate_code);
        ok = ok && json_add_number(effective, "span_khz",
                                   event->effective_span_khz);
    }
    /* Utilization capability from boot (contract FINAL): available=true
     * declares SUPPORT; per-dwell validity is the presence of the util
     * object in the channel event, never this config latch. A packet rate
     * or FFT energy must never become a percentage. */
    if (ok) {
        cJSON *util = NULL;
        ok = json_add_object(root, "utilization", &util);
        ok = ok && json_add_bool(util, "available",
                                 event->utilization_available);
        if (event->utilization_available) {
            ok = ok && json_add_string(util, "source",
                                       "c5_v6.0.3_phy_cca_cnt");
            ok = ok && json_add_string(util, "confidence",
                                       "experimental_sampled");
            ok = ok && json_add_string(util, "label",
                                       "sampled PHY CCA (experimental); "
                                       "NAV equivalence not established; "
                                       "pooled sum of up to cca_attempts "
                                       "(1..32, default 16) sampled short "
                                       "windows, not the dwell span");
        } else {
            ok = ok && json_add_string(util, "blocker",
                                       "cca_semantics_unproven");
        }
    }
    ok = ok && json_add_number(root, "tx_dropped", event->tx_dropped);
    return json_print_and_free(root, ok, dst, capacity_bytes);
}

size_t monitor_format_channel_event(char *dst, size_t capacity_bytes,
                                    const MonitorChannelEvent *event) {
    if (dst == NULL || event == NULL || event->observation == NULL) {
        return 0;
    }
    const MonitorObservation *obs = event->observation;
    if (obs->access_point_count > MONITOR_MAX_ACCESS_POINTS) {
        return 0;
    }
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return 0;
    }
    bool ok = json_add_string(root, "schema", "wifi-monitor/1");
    ok = ok && json_add_string(root, "event", "channel");
    ok = ok && json_add_number(root, "epoch", event->epoch);
    ok = ok && json_add_number(root, "cycle", event->cycle);
    ok = ok && json_add_number(root, "band", obs->band);
    ok = ok && json_add_number(root, "ch", obs->channel);
    ok = ok && json_add_number(root, "observed_ms", obs->observed_ms);
    ok = ok && json_add_number(root, "packets", obs->packets);
    if (obs->has_peak_rssi) {
        ok = ok && json_add_number(root, "peak_rssi_dbm",
                                   obs->peak_rssi_dbm);
    } else {
        ok = ok && json_add_null(root, "peak_rssi_dbm");
    }
    cJSON *aps = NULL;
    ok = ok && json_add_array(root, "aps", &aps);
    for (uint8_t i = 0; ok && i < obs->access_point_count; i++) {
        const MonitorAccessPoint *ap = &obs->access_points[i];
        if (ap->ssid_len > MONITOR_MAX_SSID_BYTES) {
            ok = false;
            break;
        }
        char bssid_hex[13];
        char ssid_hex[2 * MONITOR_MAX_SSID_BYTES + 1];
        hex_bytes(bssid_hex, ap->bssid, 6);
        hex_bytes(ssid_hex, ap->ssid_bytes, ap->ssid_len);
        cJSON *item = cJSON_CreateObject();
        if (item == NULL || !json_attach_array(aps, item)) {
            ok = false;
            break;
        }
        ok = json_add_string(item, "bssid", bssid_hex);
        ok = ok && json_add_string(item, "ssid_hex", ssid_hex);
        if (ap->advertised_channel != 0) {
            ok = ok && json_add_number(item, "primary_ch",
                                       ap->advertised_channel);
        } else {
            ok = ok && json_add_null(item, "primary_ch");
        }
        ok = ok && json_add_number(item, "rssi_dbm", ap->rssi_dbm);
    }
    ok = ok && json_add_number(root, "ap_dropped",
                               obs->access_points_dropped);
    /* CCA utilization (CURRENT pooled contract, no version key): sum of
     * up to cca_attempts (1..32, default 16) distributed one-shot windows
     * per valid dwell, each window strictly validated by the firmware.
     * Absence of the object is the truthful gap (zero valid windows
     * publish nothing). Host computes 100*busy/total over the pooled sums;
     * window_us_upper is the SUM of the sampled short windows, never the
     * dwell span. samples = valid windows (1..32), attempted = windows
     * actually tried (samples..32); 32 x 0x07ffffff fits uint32. */
    if (ok && obs->util_valid &&
        obs->util_samples >= 1 && obs->util_samples <= 32 &&
        obs->util_attempted >= obs->util_samples &&
        obs->util_attempted <= 32 &&
        obs->util_total >= obs->util_samples &&
        obs->util_total <= obs->util_samples * 0x07ffffffu &&
        obs->util_busy <= obs->util_total &&
        obs->util_window_us_upper >= obs->util_samples &&
        obs->util_window_us_upper <= 5000u * obs->util_samples) {
        cJSON *u = NULL;
        ok = json_add_object(root, "util", &u);
        ok = ok && json_add_string(u, "source", "c5_v6.0.3_phy_cca_cnt");
        ok = ok && json_add_string(u, "confidence", "experimental_sampled");
        ok = ok && json_add_number(u, "busy", obs->util_busy);
        ok = ok && json_add_number(u, "total", obs->util_total);
        ok = ok && json_add_number(u, "samples", obs->util_samples);
        ok = ok && json_add_number(u, "attempted", obs->util_attempted);
        ok = ok && json_add_number(u, "window_us_upper",
                                   obs->util_window_us_upper);
    }
    /* Not inside the if: a truthfully missing util still yields channel. */
    return json_print_and_free(root, ok, dst, capacity_bytes);
}

size_t monitor_format_cycle_event(char *dst, size_t capacity_bytes,
                                  const MonitorCycleEvent *event) {
    if (dst == NULL || event == NULL) {
        return 0;
    }
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return 0;
    }
    bool ok = json_add_string(root, "schema", "wifi-monitor/1");
    ok = ok && json_add_string(root, "event", "cycle");
    ok = ok && json_add_number(root, "epoch", event->epoch);
    ok = ok && json_add_number(root, "cycle", event->cycle);
    ok = ok && json_add_number(root, "band", event->band);
    ok = ok && json_add_number(root, "elapsed_ms", event->elapsed_ms);
    ok = ok && json_add_number(root, "uptime_ms", event->uptime_ms);
    return json_print_and_free(root, ok, dst, capacity_bytes);
}

size_t monitor_format_error_event(char *dst, size_t capacity_bytes,
                                  const char *code) {
    if (dst == NULL || code == NULL) {
        return 0;
    }
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return 0;
    }
    bool ok = json_add_string(root, "schema", "wifi-monitor/1");
    ok = ok && json_add_string(root, "event", "error");
    ok = ok && json_add_string(root, "code", code);
    return json_print_and_free(root, ok, dst, capacity_bytes);
}

size_t monitor_format_channel_error_event(char *dst, size_t capacity_bytes,
                                          const MonitorChannelErrorEvent *event) {
    if (dst == NULL || event == NULL || event->code == NULL) {
        return 0;
    }
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return 0;
    }
    bool ok = json_add_string(root, "schema", "wifi-monitor/1");
    ok = ok && json_add_string(root, "event", "channel_error");
    ok = ok && json_add_number(root, "epoch", event->epoch);
    ok = ok && json_add_number(root, "cycle", event->cycle);
    ok = ok && json_add_number(root, "band", event->band);
    ok = ok && json_add_number(root, "ch", event->channel);
    ok = ok && json_add_string(root, "code", event->code);
    return json_print_and_free(root, ok, dst, capacity_bytes);
}

bool monitor_tlv_wrap_status(MonitorTlvFrame *frame, const char *json_body,
                             size_t json_length_bytes) {
    if (frame == NULL || json_body == NULL || json_length_bytes == 0 ||
        json_length_bytes > MONITOR_JSON_CAPACITY_BYTES) {
        return false;
    }
    size_t total = MONITOR_TLV_HEADER_BYTES + json_length_bytes +
                   MONITOR_TLV_CRC_BYTES;
    if (total > MONITOR_TLV_FRAME_CAPACITY_BYTES) {
        return false;
    }
    frame->bytes[0] = 0x03;                 /* STATUS */
    frame->bytes[1] = (uint8_t)(json_length_bytes & 0xffu);
    frame->bytes[2] = (uint8_t)((json_length_bytes >> 8) & 0xffu);
    memcpy(frame->bytes + MONITOR_TLV_HEADER_BYTES, json_body, json_length_bytes);
    uint32_t crc = monitor_crc32(frame->bytes,
                                 MONITOR_TLV_HEADER_BYTES + json_length_bytes);
    write_le32(frame->bytes + MONITOR_TLV_HEADER_BYTES + json_length_bytes, crc);
    frame->length_bytes = total;
    return true;
}

void monitor_run_init(MonitorRun *run, const MonitorConfig *defaults) {
    if (run == NULL || defaults == NULL) {
        return;
    }
    run->active_config = *defaults;
    run->epoch = 1;
    run->cycle = 0;
}

MonitorBoundary monitor_run_boundary(MonitorRun *run,
                                     const MonitorConfig *pending,
                                     bool pending_valid) {
    if (run == NULL) {
        return MONITOR_BOUNDARY_IDLE;
    }
    if (!pending_valid || pending == NULL) {
        return MONITOR_BOUNDARY_IDLE;
    }
    if (monitor_config_equal(pending, &run->active_config)) {
        return MONITOR_BOUNDARY_ACK;    /* idempotent: no epoch change */
    }
    run->active_config = *pending;
    run->epoch++;
    return MONITOR_BOUNDARY_APPLY;
}

uint32_t monitor_run_begin_cycle(MonitorRun *run) {
    if (run == NULL) {
        return 0;
    }
    if (run->cycle < UINT32_MAX) {
        run->cycle++;
    }
    return run->cycle;
}
