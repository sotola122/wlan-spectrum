/*
 * Portable monitor core: CONFIG framing, AP sighting parsing, observation
 * aggregation, and bounded JSON/TLV serialization for the wifi-monitor/1
 * protocol. No SDK, RTOS, or MCU headers are permitted here (CODING_GUIDELINE
 * section 6); IDF adapters live in monitor_radio.h / monitor_link.h.
 *
 * Concurrency: every function is task-context only unless a comment says
 * otherwise. Functions whose names take a `MonitorObservation *` may be called
 * from the radio callback only where monitor_radio.c documents it; the core
 * itself performs no locking.
 */
#ifndef MONITOR_CORE_H
#define MONITOR_CORE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Pure-C consumers only (firmware sources and the host fixture are C). */

/* Wire-size constants, all bytes unless suffixed otherwise.
 *
 * Frame layout (both directions, every message):
 *   type u8 | payload_length u16 LE | payload[payload_length] | crc32 u32 LE
 * The length excludes header and checksum. The CRC-32/ISO-HDLC covers the
 * exact header + payload bytes; legacy CRC-less frames are rejected. */
#define MONITOR_TLV_HEADER_BYTES 3
#define MONITOR_TLV_CRC_BYTES 4
#define MONITOR_CONFIG_PAYLOAD_BYTES 10
#define MONITOR_CONFIG_FRAME_BYTES 13      /* header + payload (CRC input) */
#define MONITOR_CONFIG_STREAM_BYTES 17     /* frame + 4-byte checksum */
#define MONITOR_MAX_ACCESS_POINTS 8
#define MONITOR_MAX_SSID_BYTES 32
#define MONITOR_JSON_CAPACITY_BYTES 4096
#define MONITOR_TLV_FRAME_CAPACITY_BYTES 4103  /* 3 + 4096 body + 4 crc */
#define MONITOR_MAX_CHANNELS 20            /* 5 GHz JP allowlist size */
#define MONITOR_MIN_DWELL_MS 120

/* ------------------------------------------------------------------ config */

typedef struct {
    uint8_t mode;               /* 0 live, 1 sweep */
    uint8_t band;               /* 0 = 2.4 GHz, 1 = 5 GHz */
    uint16_t sweep_ms;          /* requested cycle time, milliseconds */
    uint16_t fft_size;          /* echoed, not supported by this firmware */
    uint32_t sample_rate_khz;   /* echoed, not supported by this firmware */
} MonitorConfig;

typedef struct {
    uint8_t frame_bytes[MONITOR_CONFIG_STREAM_BYTES];
    size_t used_bytes;
    uint32_t discarded_bytes;   /* bytes dropped while resynchronizing, not frames */
} MonitorConfigParser;

/* CRC-32/ISO-HDLC (a.k.a. CRC-32, zlib-compatible): reflected polynomial
 * 0xEDB88320, init/xorout 0xFFFFFFFF, refin/refout true. Portable, no ROM
 * dependency. Known-answer check: monitor_crc32("123456789", 9) == 0xCBF43926. */
uint32_t monitor_crc32(const uint8_t *data, size_t length_bytes);

/* Reset parser state. Task-context only. */
void monitor_config_parser_init(MonitorConfigParser *parser);

/* Feed one stream byte. Returns true only when a complete CONFIG frame with
 * a valid CRC and valid field values was accepted; then *out_config is
 * replaced. On false, *out_config is unchanged. The checksum is validated
 * before any field is read. Frames failing CRC or field validation are
 * dropped one byte at a time and counted in parser->discarded_bytes.
 * parser and out_config must be non-NULL. */
bool monitor_config_feed(MonitorConfigParser *parser, uint8_t byte,
                         MonitorConfig *out_config);

/* Field validation for a parsed config (also usable for incoming values). */
bool monitor_config_is_valid(const MonitorConfig *config);

/* True when all five fields are equal. Never reads padding (plain struct). */
bool monitor_config_equal(const MonitorConfig *a, const MonitorConfig *b);

/* Dwell per channel: max(MONITOR_MIN_DWELL_MS, ceil(sweep_ms / channels)).
 * channels == 0 returns 0. Result fits uint32 (sweep_ms <= 65535). */
uint32_t monitor_dwell_ms(uint16_t sweep_ms, size_t channel_count);

/* JP receive-channel allowlist for band (0 or 1). Writes up to capacity
 * channel numbers into out_channels and returns the count for the band
 * (13 or 20); out_channels may be NULL only when capacity is 0. Unknown
 * band returns 0. */
size_t monitor_band_channels(uint8_t band, uint8_t *out_channels,
                             size_t capacity);

/* -------------------------------------------------------------- ap sight */

typedef struct {
    uint8_t bssid[6];
    uint8_t ssid_bytes[MONITOR_MAX_SSID_BYTES];
    uint8_t ssid_len;
    uint8_t advertised_channel;     /* 0 = no valid DS/HT IE found */
    int8_t rssi_dbm;                /* latest sighting RSSI in this dwell */
    bool has_ssid;                  /* false: no (or oversized) SSID IE */
} MonitorAccessPoint;

/* Parse one complete management frame (beacon 0x80 or probe response 0x50)
 * into an AP sighting. `frame` must reference `frame_length_bytes` readable
 * bytes; NULL, short, non-management, or SSID-less frames return false and
 * leave *out unchanged. All IE reads are bounded by frame_length_bytes; a
 * truncated IE stops parsing without out-of-bounds access. Conflicting DS and
 * HT primary channels yield advertised_channel == 0 (unknown). Task-context,
 * no allocation, no locking. */
bool monitor_parse_access_point(const uint8_t *frame, size_t frame_length_bytes,
                                MonitorAccessPoint *out);

/* ----------------------------------------------------------- observation */

typedef struct {
    uint8_t band;
    uint8_t channel;                   /* hardware receive channel observed */
    uint32_t packets;                  /* successfully received frames */
    int8_t peak_rssi_dbm;              /* valid only when has_peak_rssi */
    bool has_peak_rssi;                /* false: no valid packet this dwell */
    uint32_t observed_ms;              /* actual reception time this dwell */
    MonitorAccessPoint access_points[MONITOR_MAX_ACCESS_POINTS];
    uint8_t access_point_count;
    uint32_t access_points_dropped;    /* unique-BSSID overflow + queue drops */
} MonitorObservation;

/* Reset counters for a new dwell. Task context only (radio capture disabled). */
void monitor_observation_begin(MonitorObservation *observation, uint8_t band,
                               uint8_t channel);

/* Record one successfully received frame. ISR/callback-safe: bounded work,
 * no allocation, no locking; the adapter serializes access (radio capture is
 * disabled across begin/finish). Counters saturate instead of wrapping. */
void monitor_observation_record_packet(MonitorObservation *observation,
                                       int8_t rssi_dbm);

/* Fold one copied beacon/probe-response sighting into the per-dwell AP table
 * (task context). Duplicate BSSIDs update in place with the latest RSSI/SSID;
 * a ninth unique BSSID increments access_points_dropped. Returns true when
 * the frame parsed; false leaves the table unchanged. */
bool monitor_observation_record_sighting(MonitorObservation *observation,
                                         const uint8_t *frame,
                                         size_t frame_length_bytes,
                                         int8_t rssi_dbm);

/* Seal the dwell: store the measured reception time. observed_ms is the
 * adapter-rounded elapsed milliseconds; no retune/USB time is included. */
void monitor_observation_finalize(MonitorObservation *observation,
                                  uint32_t observed_ms);

/* ------------------------------------------------------- json / tlv wire */

typedef struct {
    uint32_t epoch;
    MonitorConfig config;
    uint32_t dwell_ms;
    const uint8_t *channels;            /* borrowed for the call only */
    size_t channel_count;
    uint32_t tx_dropped;
} MonitorConfigEvent;

typedef struct {
    uint32_t epoch;
    uint32_t cycle;
    const MonitorObservation *observation;   /* borrowed for the call only */
} MonitorChannelEvent;

typedef struct {
    uint32_t epoch;
    uint32_t cycle;
    uint8_t band;
    uint32_t elapsed_ms;
    uint32_t uptime_ms;
} MonitorCycleEvent;

typedef struct {
    uint32_t epoch;
    uint32_t cycle;
    uint8_t band;
    uint8_t channel;
    const char *code;                    /* vetted literal / esp_err_to_name */
} MonitorChannelErrorEvent;

typedef struct {
    uint8_t bytes[MONITOR_TLV_FRAME_CAPACITY_BYTES];
    size_t length_bytes;
} MonitorTlvFrame;

/* Each formatter writes a complete JSON body plus NUL into dst (capacity
 * capacity_bytes) and returns the body length in bytes, or 0 on any failure
 * (too small, NULL, allocation failure, formatting error). On 0 the caller
 * must not transmit any part of the event; dst contents are unspecified.
 * Bodies are limited to MONITOR_JSON_CAPACITY_BYTES regardless of the
 * 8192-byte TLV payload cap.
 *
 * JSON is produced with the official espressif/cjson component (v1.7.19,
 * locked in firmware/dependencies.lock): one transient, bounded cJSON tree
 * per event, built in task context (never in the Wi-Fi callback), every
 * create/add/print call checked, freed with cJSON_Delete before return. See
 * monitor_core.c for the full memory contract. */
size_t monitor_format_config_event(char *dst, size_t capacity_bytes,
                                   const MonitorConfigEvent *event);
size_t monitor_format_channel_event(char *dst, size_t capacity_bytes,
                                    const MonitorChannelEvent *event);
size_t monitor_format_cycle_event(char *dst, size_t capacity_bytes,
                                  const MonitorCycleEvent *event);
size_t monitor_format_error_event(char *dst, size_t capacity_bytes,
                                  const char *code);
size_t monitor_format_channel_error_event(char *dst, size_t capacity_bytes,
                                          const MonitorChannelErrorEvent *event);

/* Wrap a JSON body in the device-to-PC STATUS frame (type 0x03,
 * little-endian length) and append the CRC-32/ISO-HDLC checksum over the
 * header + payload bytes. Returns false (frame untouched) when
 * json_length_bytes is 0 or exceeds MONITOR_JSON_CAPACITY_BYTES. On success
 * frame->length_bytes is 3 + json_length_bytes + 4. */
bool monitor_tlv_wrap_status(MonitorTlvFrame *frame, const char *json_body,
                             size_t json_length_bytes);

/* ------------------------------------------------------- run accounting */

typedef struct {
    MonitorConfig active_config;
    uint32_t epoch;                 /* increments only on a changed config */
    uint32_t cycle;                 /* monotonic; gaps allowed on discard */
} MonitorRun;

typedef enum {
    MONITOR_BOUNDARY_IDLE = 0,      /* no pending config */
    MONITOR_BOUNDARY_ACK,           /* identical: re-ack, epoch unchanged */
    MONITOR_BOUNDARY_APPLY          /* changed: adopt, epoch++ (caller must
                                     * discard the incomplete cycle and skip
                                     * its cycle event) */
} MonitorBoundary;

/* Initialize with defaults; epoch starts at 1, cycle at 0. Task-context. */
void monitor_run_init(MonitorRun *run, const MonitorConfig *defaults);

/* Channel-boundary decision: pending_valid means one complete valid CONFIG
 * is waiting. On MONITOR_BOUNDARY_APPLY the pending config becomes active
 * and epoch increments; otherwise run state is unchanged. */
MonitorBoundary monitor_run_boundary(MonitorRun *run,
                                     const MonitorConfig *pending,
                                     bool pending_valid);

/* Start the next cycle; returns the new cycle number (monotonic). */
uint32_t monitor_run_begin_cycle(MonitorRun *run);

#endif /* MONITOR_CORE_H */
