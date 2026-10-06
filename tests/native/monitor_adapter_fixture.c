/*
 * Host seam tests for the REAL adapter sources (monitor_link.c,
 * monitor_radio.c) compiled against tests/native/fake_sdk.
 *
 * Exercises the code paths parent reviews call out: TX frame ownership and
 * partial-write resumption, bounded per-poll RX budget, whole-frame queue
 * drops, beacon copy lengths 255/256/oversized at the actual callback seam,
 * packet gating, and queue-drop/packet-count independence. Each mode is an
 * assert-based check; exit 0 = pass. Test scaffolding only (never linked
 * into firmware).
 */
#include "monitor_core.h"
#include "monitor_link.h"
#include "monitor_radio.h"

#include "fake_sdk.h"
#include "soc/uart_pins.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

/* Bounded busy-wait (strict C11: no POSIX sleep feature macros needed).
 * The TX task runs on its own pthread, so the spin observes its progress;
 * the iteration cap is the bound. */
static void wait_for_tx_length(size_t target) {
    for (unsigned long i = 0; i < 200000000UL && fake_uart_tx_length() < target;
         i++) {
    }
}

/* ------------------------------------------------------------- helpers */

static void build_config_frame(uint8_t *out, uint8_t mode, uint8_t band,
                               uint16_t sweep_ms, uint16_t fft_size,
                               uint32_t rate_khz) {
    out[0] = 0x10;
    out[1] = MONITOR_CONFIG_PAYLOAD_BYTES;
    out[2] = 0;
    out[3] = mode;
    out[4] = band;
    out[5] = (uint8_t)(sweep_ms & 0xffu);
    out[6] = (uint8_t)(sweep_ms >> 8);
    out[7] = (uint8_t)(fft_size & 0xffu);
    out[8] = (uint8_t)(fft_size >> 8);
    out[9] = (uint8_t)(rate_khz & 0xffu);
    out[10] = (uint8_t)((rate_khz >> 8) & 0xffu);
    out[11] = (uint8_t)((rate_khz >> 16) & 0xffu);
    out[12] = (uint8_t)((rate_khz >> 24) & 0xffu);
    uint32_t crc = monitor_crc32(out, MONITOR_CONFIG_FRAME_BYTES);
    out[13] = (uint8_t)(crc & 0xffu);
    out[14] = (uint8_t)((crc >> 8) & 0xffu);
    out[15] = (uint8_t)((crc >> 16) & 0xffu);
    out[16] = (uint8_t)((crc >> 24) & 0xffu);
}

static size_t build_beacon(uint8_t *buf, const uint8_t bssid[6],
                           const uint8_t *ssid, uint8_t ssid_len,
                           uint8_t channel) {
    memset(buf, 0, 64 + ssid_len);
    buf[0] = 0x80;
    memset(buf + 4, 0xff, 6);
    memcpy(buf + 10, bssid, 6);
    memcpy(buf + 16, bssid, 6);
    buf[32] = 0x64;
    buf[34] = 0x01;
    size_t pos = 36;
    buf[pos++] = 0x00;
    buf[pos++] = ssid_len;
    memcpy(buf + pos, ssid, ssid_len);
    pos += ssid_len;
    buf[pos++] = 0x03;
    buf[pos++] = 0x01;
    buf[pos++] = channel;
    return pos;
}

/* Deliver one frame through the adapter's registered callback. dump_len is
 * the value the fake rx_ctrl reports (what the real driver would claim). */
static void fire_rx(wifi_promiscuous_pkt_type_t type, int8_t rssi,
                    uint8_t rx_state, uint8_t channel, uint16_t dump_len,
                    const uint8_t *payload, size_t payload_len) {
    union {
        wifi_promiscuous_pkt_t pkt;
        max_align_t align;
        uint8_t raw[sizeof(wifi_promiscuous_pkt_t) + 600];
    } storage;
    memset(&storage, 0, sizeof(storage));
    storage.pkt.rx_ctrl.rssi = rssi;
    storage.pkt.rx_ctrl.rx_state = rx_state;
    storage.pkt.rx_ctrl.channel = channel;
    storage.pkt.rx_ctrl.dump_len = dump_len;
    assert(payload_len <= sizeof(storage.raw) - sizeof(wifi_promiscuous_pkt_t));
    memcpy(storage.pkt.payload, payload, payload_len);
    wifi_promiscuous_cb_t cb = fake_wifi_rx_cb();
    assert(cb != NULL);
    cb(&storage.pkt, type);
}

/* --------------------------------------------------------------- cases */

static void case_tx_partial(void) {
    assert(monitor_link_init() == ESP_OK);
    fake_uart_tx_set_max_per_call(2);    /* writer accepts 2 bytes per call */
    fake_uart_tx_fail_next(1);           /* then one zero-byte timeout */

    const char *body = "{\"a\":1}";
    size_t body_len = strlen(body);
    assert(monitor_link_submit_json(body, body_len));

    fake_tasks_start();
    /* bounded wait for the TX task to flush the whole frame */
    MonitorTlvFrame expected_frame;
    assert(monitor_tlv_wrap_status(&expected_frame, body, body_len));
    wait_for_tx_length(expected_frame.length_bytes);
    assert(fake_uart_tx_length() == expected_frame.length_bytes);
    assert(memcmp(fake_uart_tx_data(), expected_frame.bytes,
                  expected_frame.length_bytes) == 0);
    /* frame arrives exactly once: no restart, no truncation */
    size_t settled = fake_uart_tx_length();
    wait_for_tx_length(settled + 1);        /* spins the full bound */
    assert(fake_uart_tx_length() == settled);
    printf("tx-partial ok\n");
}

static void case_tx_queue_full(void) {
    assert(monitor_link_init() == ESP_OK);
    /* TX task recorded but NOT started: queue fills deterministically. */
    char body[64];
    size_t bodies = 0;
    size_t total_expected = 0;
    for (int i = 0; i < 4; i++) {
        snprintf(body, sizeof(body), "{\"n\":%d}", i);
        assert(monitor_link_submit_json(body, strlen(body)));
        bodies++;
        MonitorTlvFrame frame;
        assert(monitor_tlv_wrap_status(&frame, body, strlen(body)));
        total_expected += frame.length_bytes;
    }
    snprintf(body, sizeof(body), "{\"n\":99}");
    assert(!monitor_link_submit_json(body, strlen(body)));   /* whole drop */
    assert(monitor_link_tx_dropped() == 1);

    fake_tasks_start();
    wait_for_tx_length(total_expected);
    assert(fake_uart_tx_length() == total_expected);           /* 4 frames */
    assert(bodies == 4);
    printf("tx-queue-full ok\n");
}

static void case_uart_config(void) {
    /* Transport contract: UART0 @921600 8N1 no-flow on the SoC-default
     * download pins (matches GUI/smoke 921600 default). */
    assert(monitor_link_init() == ESP_OK);
    assert(fake_uart_baud() == 921600);
    assert(fake_uart_tx_pin() == U0TXD_GPIO_NUM);
    assert(fake_uart_rx_pin() == U0RXD_GPIO_NUM);
    assert(U0TXD_GPIO_NUM == 11 && U0RXD_GPIO_NUM == 12);
    printf("uart-config ok\n");
}

static void case_tx_starvation(void) {
    /* Firmware backpressure: the FIFO never accepts -> the TX task must
     * hit its 30 s deadline, count ONE whole-frame drop, wire stays clean,
     * and the task must remain alive for later frames. */
    assert(monitor_link_init() == ESP_OK);
    fake_uart_tx_fail_next(0x7fffffff);
    const char *body = "{\"starved\":1}";
    assert(monitor_link_submit_json(body, strlen(body)));
    fake_tasks_start();
    for (unsigned long i = 0; i < 200000000UL &&
         monitor_link_tx_dropped() == 0; i++) {
    }
    assert(monitor_link_tx_dropped() == 1);        /* deadline fired */
    assert(fake_uart_tx_length() == 0);            /* nothing hit the wire */
    /* starvation cleared: the same task must still drain a new frame */
    fake_uart_tx_fail_next(0);
    assert(monitor_link_submit_json(body, strlen(body)));
    for (unsigned long i = 0; i < 200000000UL &&
         fake_uart_tx_length() == 0; i++) {
    }
    MonitorTlvFrame expected;
    assert(monitor_tlv_wrap_status(&expected, body, strlen(body)));
    assert(fake_uart_tx_length() == expected.length_bytes);
    assert(memcmp(fake_uart_tx_data(), expected.bytes,
                  expected.length_bytes) == 0);
    printf("tx-starvation ok\n");
}

static void case_poll_budget(void) {
    assert(monitor_link_init() == ESP_OK);

    /* Flood: 4096 bytes of non-frame garbage. */
    static uint8_t garbage[4096];
    memset(garbage, 0x55, sizeof(garbage));
    fake_uart_rx_push(garbage, sizeof(garbage));
    size_t before = fake_uart_rx_pending();
    monitor_link_poll();
    size_t consumed = before - fake_uart_rx_pending();
    assert(consumed > 0);
    /* boundedness regression: one poll may not monopolize the dwell tick */
    assert(consumed <= 256);

    int polls = 1;
    while (fake_uart_rx_pending() > 0 && polls < 1000) {
        monitor_link_poll();
        polls++;
    }
    assert(fake_uart_rx_pending() == 0);      /* nothing lost, just paced */

    /* two configs queued back-to-back: newest wins, second take empty */
    uint8_t frame[MONITOR_CONFIG_STREAM_BYTES];
    build_config_frame(frame, 0, 0, 1000, 64, 20000);
    fake_uart_rx_push(frame, sizeof(frame));
    build_config_frame(frame, 1, 1, 3000, 128, 40000);
    fake_uart_rx_push(frame, sizeof(frame));
    monitor_link_poll();
    MonitorConfig got;
    assert(monitor_link_take_config(&got));
    assert(got.mode == 1 && got.band == 1 && got.sweep_ms == 3000 &&
           got.fft_size == 128 && got.sample_rate_khz == 40000);
    assert(!monitor_link_take_config(&got));
    printf("poll-budget ok polls=%d consumed=%zu\n", polls, consumed);
}

static void case_invalid_rate(void) {
    assert(monitor_link_init() == ESP_OK);
    uint8_t junk[3] = {0x55, 0x55, 0x55};
    fake_uart_rx_push(junk, sizeof(junk));
    monitor_link_poll();
    assert(monitor_link_take_invalid_config());
    assert(!monitor_link_take_invalid_config());      /* one per window */
    fake_uart_rx_push(junk, sizeof(junk));
    monitor_link_poll();
    assert(!monitor_link_take_invalid_config());      /* within 1 s */
    fake_time_advance_us(1000001);
    fake_uart_rx_push(junk, sizeof(junk));
    monitor_link_poll();
    assert(monitor_link_take_invalid_config());       /* after period */
    printf("invalid-rate ok\n");
}

static void case_radio_sightings(void) {
    assert(monitor_radio_init() == ESP_OK);
    wifi_promiscuous_cb_t cb = fake_wifi_rx_cb();
    assert(cb != NULL);
    assert(monitor_radio_begin(0, 6) == ESP_OK);
    assert(fake_wifi_promiscuous_enabled());
    assert(fake_wifi_last_channel() == 6);

    static const uint16_t dump_lens[] = {255, 256, 500};
    for (size_t i = 0; i < 3; i++) {
        uint8_t beacon[128];
        uint8_t bssid[6] = {0x02, 0x00, 0x00, 0x00, 0x00, (uint8_t)i};
        size_t n = build_beacon(beacon, bssid, (const uint8_t *)"test", 4, 6);
        /* payload buffer padded with IE-safe zeros to reach dump_len */
        uint8_t payload[600] = {0};
        memcpy(payload, beacon, n);
        fire_rx(WIFI_PKT_MGMT, -50, 0, 6, dump_lens[i], payload,
                sizeof(payload));
        fake_time_advance_us(40000);        /* 40 ms per sighting */
    }

    MonitorObservation obs;
    monitor_radio_finish(&obs);
    assert(obs.packets == 3);
    assert(obs.has_peak_rssi && obs.peak_rssi_dbm == -50);
    assert(obs.channel == 6 && obs.band == 0);
    assert(obs.observed_ms == 120);
    /* THE regression: dump_len 256 must not collapse to length 0, and an
     * oversized dump must be clamped, not truncated to a bogus length. */
    assert(obs.access_point_count == 3);
    for (uint8_t i = 0; i < obs.access_point_count; i++) {
        assert(obs.access_points[i].ssid_len == 4);
        assert(memcmp(obs.access_points[i].ssid_bytes, "test", 4) == 0);
    }
    assert(!fake_wifi_promiscuous_enabled());  /* finish disables capture */
    printf("radio-sightings ok\n");
}

static void case_radio_gating(void) {
    assert(monitor_radio_init() == ESP_OK);
    assert(monitor_radio_begin(0, 6) == ESP_OK);

    uint8_t beacon[128];
    uint8_t bssid[6] = {0x02, 0x11, 0x22, 0x33, 0x44, 0x55};
    size_t n = build_beacon(beacon, bssid, (const uint8_t *)"gate", 4, 6);

    /* failed reception: not a packet */
    fire_rx(WIFI_PKT_MGMT, -40, 1, 6, (uint16_t)n, beacon, n);
    /* wrong receive channel: not accepted */
    fire_rx(WIFI_PKT_MGMT, -40, 0, 11, (uint16_t)n, beacon, n);
    /* MISC: ignored before payload use */
    fire_rx(WIFI_PKT_MISC, -40, 0, 6, (uint16_t)n, beacon, n);
    /* control + data: counted, never parsed into sightings */
    fire_rx(WIFI_PKT_CTRL, -40, 0, 6, 10, beacon, 10);
    fire_rx(WIFI_PKT_DATA, -60, 0, 6, 10, beacon, 10);
    /* valid management: counted + sighting */
    fire_rx(WIFI_PKT_MGMT, -55, 0, 6, (uint16_t)n, beacon, n);

    MonitorObservation obs;
    monitor_radio_finish(&obs);
    assert(obs.packets == 3);                 /* ctrl + data + mgmt */
    assert(obs.access_point_count == 1);      /* only the mgmt sighting */

    /* sighting-queue overflow increments ap_dropped, never reduces packets */
    assert(monitor_radio_begin(0, 6) == ESP_OK);
    for (int i = 0; i < 40; i++) {
        fire_rx(WIFI_PKT_MGMT, -50, 0, 6, (uint16_t)n, beacon, n);
    }
    monitor_radio_finish(&obs);
    assert(obs.packets == 40);
    assert(obs.access_points_dropped >= 8);   /* queue depth 32 */
    printf("radio-gating ok\n");
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "tx-partial") == 0) {
        case_tx_partial();
        return 0;
    }
    if (strcmp(argv[1], "tx-queue-full") == 0) {
        case_tx_queue_full();
        return 0;
    }
    if (strcmp(argv[1], "uart-config") == 0) {
        case_uart_config();
        return 0;
    }
    if (strcmp(argv[1], "tx-starvation") == 0) {
        case_tx_starvation();
        return 0;
    }
    if (strcmp(argv[1], "poll-budget") == 0) {
        case_poll_budget();
        return 0;
    }
    if (strcmp(argv[1], "invalid-rate") == 0) {
        case_invalid_rate();
        return 0;
    }
    if (strcmp(argv[1], "radio-sightings") == 0) {
        case_radio_sightings();
        return 0;
    }
    if (strcmp(argv[1], "radio-gating") == 0) {
        case_radio_gating();
        return 0;
    }
    fprintf(stderr, "unknown case: %s\n", argv[1]);
    return 2;
}
