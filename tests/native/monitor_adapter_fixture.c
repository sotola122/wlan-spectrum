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

/* CCA window: read-only telemetry across begin/finish, wrap-safe deltas,
 * flag capture at both ends, exact t_us window, and conservative JSON
 * presence/absence (raw object only when both ends were sampled). */
static void case_radio_util(void) {
    MonitorObservation out;

    /* 1. no begin -> no window -> truthful absence */
    memset(&out, 0, sizeof out);
    monitor_radio_finish(&out);
    assert(!out.util_valid);

    /* 2. valid ENDPOINT window: value=0x400 read at the call site, A
     * saturates at the limit, done flag flips, B stays 0. */
    fake_cca_ctrl_set(0x80000400u);
    fake_cca_set(0u, 0u, 0u);
    fake_time_autostep(10u);
    fake_cca_step(80u, 0u);
    uint32_t calls0 = fake_cca_set_cnt_calls();
    assert(monitor_radio_begin(0, 6) == ESP_OK);
    assert(fake_cca_set_cnt_calls() == calls0 + 1u);
    assert(fake_cca_set_cnt_value() == 0x400u);
    assert(fake_cca_set_cnt_arm() == 1u);
    monitor_radio_finish(&out);
    fake_time_autostep(0u);
    fake_cca_step(0u, 0u);
    assert(out.util_valid);
    assert(out.util_total == 0x400u);        /* A final == configured limit */
    assert(out.util_busy == 0u);
    assert(out.util_window_us_upper > 0u &&
           out.util_window_us_upper <= 5000u);

    /* 3. formatter: current pooled util object — no pairs/busy_frac,
     * no version key */
    MonitorChannelEvent ev = {.epoch = 1, .cycle = 2, .observation = &out};
    char json[4096];
    size_t n = monitor_format_channel_event(json, sizeof json, &ev);
    assert(n > 0);
    assert(strstr(json, "\"util\"") != NULL);
    assert(strstr(json, "\"source\":\"c5_v6.0.3_phy_cca_cnt\"") != NULL);
    assert(strstr(json, "\"confidence\":\"experimental_sampled\"") != NULL);
    assert(strstr(json, "\"window_us_upper\"") != NULL);
    assert(strstr(json, "\"busy_frac\"") == NULL);
    assert(strstr(json, "\"pairs\"") == NULL);

    /* 4. B overshoot -> busy > total -> invalid gap */
    fake_cca_ctrl_set(0x80000400u);
    fake_cca_set(0u, 0u, 0u);
    fake_time_autostep(10u);
    fake_cca_step(80u, 240u);
    assert(monitor_radio_begin(0, 6) == ESP_OK);
    monitor_radio_finish(&out);
    fake_time_autostep(0u);
    fake_cca_step(0u, 0u);
    assert(!out.util_valid);
    ev.observation = &out;
    n = monitor_format_channel_event(json, sizeof json, &ev);
    assert(n > 0 && strstr(json, "\"util\"") == NULL);

    /* 5. completion never observed -> invalid */
    fake_cca_ctrl_set(0x87ffffffu);
    fake_cca_set(0u, 0u, 0u);
    fake_time_autostep(10u);
    fake_cca_step(1u, 0u);
    assert(monitor_radio_begin(0, 6) == ESP_OK);
    monitor_radio_finish(&out);
    fake_time_autostep(0u);
    fake_cca_step(0u, 0u);
    assert(!out.util_valid);

    /* 6. completed before first read -> invalid (no reset proof) */
    fake_cca_ctrl_set(0x80000400u);
    fake_cca_set(2000u, 0u, 0u);
    fake_time_autostep(10u);
    fake_cca_step(80u, 0u);
    assert(monitor_radio_begin(0, 6) == ESP_OK);
    monitor_radio_finish(&out);
    fake_time_autostep(0u);
    fake_cca_step(0u, 0u);
    assert(!out.util_valid);

    /* 7. valid with B tracking below A */
    fake_cca_ctrl_set(0x80000400u);
    fake_cca_set(0u, 0u, 0u);
    fake_time_autostep(10u);
    fake_cca_step(80u, 40u);
    assert(monitor_radio_begin(0, 6) == ESP_OK);
    monitor_radio_finish(&out);
    fake_time_autostep(0u);
    fake_cca_step(0u, 0u);
    assert(out.util_valid);
    assert(out.util_total == 0x400u);
    assert(out.util_busy > 0u && out.util_busy <= out.util_total);
    ev.observation = &out;
    n = monitor_format_channel_event(json, sizeof json, &ev);
    assert(n > 0 && strstr(json, "\"util\"") != NULL);

    /* 8. failed begin: no arm at all */
    uint32_t c0 = fake_cca_set_cnt_calls();
    fake_wifi_set_channel_result(ESP_ERR_INVALID_STATE);
    assert(monitor_radio_begin(0, 6) == ESP_ERR_INVALID_STATE);
    fake_wifi_set_channel_result(ESP_OK);
    assert(fake_cca_set_cnt_calls() == c0);

    /* 9. frozen clock + non-completing counter: the one-shot loop must
     * terminate via the poll-iteration bound (no hang), window invalid */
    fake_cca_ctrl_set(0x87ffffffu);
    fake_cca_set(0u, 0u, 0u);
    fake_time_autostep(0u);                /* clock frozen */
    fake_cca_step(0u, 0u);                 /* counter never advances */
    assert(monitor_radio_begin(0, 6) == ESP_OK);   /* returns, no hang */
    monitor_radio_finish(&out);
    assert(!out.util_valid);

    /* 10. valid begin -> failed begin -> finish: the stale window is
     * invalidated by the failed begin's entry (case 8 alone cannot prove
     * this: it never had an armed window pending) */
    fake_cca_ctrl_set(0x80000400u);
    fake_cca_set(0u, 0u, 0u);
    fake_time_autostep(10u);
    fake_cca_step(80u, 0u);
    assert(monitor_radio_begin(0, 6) == ESP_OK);   /* window armed */
    uint32_t c1 = fake_cca_set_cnt_calls();
    fake_wifi_set_channel_result(ESP_ERR_INVALID_STATE);
    assert(monitor_radio_begin(0, 6) == ESP_ERR_INVALID_STATE);
    fake_wifi_set_channel_result(ESP_OK);
    assert(fake_cca_set_cnt_calls() == c1);        /* failed begin: no arm */
    fake_time_autostep(0u);
    fake_cca_step(0u, 0u);
    monitor_radio_finish(&out);
    assert(!out.util_valid);                       /* stale window gone */

    printf("radio-util ok\n");
}

/* CURRENT pooled contract (no version key): EIGHT distributed one-shot
 * windows per dwell, scheduled from ACTUAL elapsed time, at most one
 * window per tick, missed slots skipped (never burst), and a late tick
 * close to dwell end arms NOTHING (a window never straddles the dwell). */
static void case_radio_util_pooled(void) {
    MonitorObservation out;

    /* 1. constant trace, default 120 ms dwell, real 10 ms tick cadence:
     * slot0 at begin + one attempt per due tick => exactly8 arms, never
     * more than one per tick (the expect table IS the no-burst proof). */
    fake_cca_arm_reset(true);       /* device model: arm clears counters */
    fake_cca_ctrl_set(0x80000400u);
    fake_cca_set(0u, 0u, 0u);
    fake_cca_step(80u, 40u);
    fake_time_autostep(10u);
    assert(monitor_radio_begin(0, 6) == ESP_OK);
    const uint32_t expect_after_tick[12] =
        {1, 2, 3, 3, 4, 5, 5, 6, 7, 7, 8, 8};
    for (int k = 0; k < 12; k++) {
        fake_time_advance_us(10000);   /* the app's dwell-wait tick */
        monitor_radio_cca_tick(120);
        assert(fake_cca_set_cnt_calls() == expect_after_tick[k]);
    }
    fake_time_autostep(0u);
    fake_cca_step(0u, 0u);
    monitor_radio_finish(&out);
    assert(out.util_valid);
    assert(out.util_samples == 8 && out.util_attempted == 8);
    assert(out.util_total == 8u * 0x400u);    /* constant trace */
    assert(out.util_busy > 0u && out.util_busy <= out.util_total);
    assert(out.util_busy % 8u == 0u);         /* identical windows */
    assert(out.util_window_us_upper >= out.util_samples &&
           out.util_window_us_upper <= 5000u * out.util_samples);

    /* 2. formatter: pooled counts present, NO version key. */
    MonitorChannelEvent ev = {.epoch = 1, .cycle = 1, .observation = &out};
    char json[4096];
    size_t n = monitor_format_channel_event(json, sizeof json, &ev);
    assert(n > 0);
    assert(strstr(json, "\"samples\":8") != NULL);
    assert(strstr(json, "\"attempted\":8") != NULL);
    assert(strstr(json, "\"version\"") == NULL);
    assert(strstr(json, "\"source\":\"c5_v6.0.3_phy_cca_cnt\"") != NULL);
    assert(strstr(json, "\"confidence\":\"experimental_sampled\"") != NULL);
    printf("%s\n", json);            /* host-side pooled decode input */

    /* 3. alternating trace: the second window sees a different B step;
     * pooled busy is the honest sum of the two different windows. */
    fake_cca_ctrl_set(0x80000400u);
    fake_cca_set(0u, 0u, 0u);
    fake_cca_step(80u, 40u);         /* window A: busy = 13 x 40 = 520 */
    fake_time_autostep(10u);
    assert(monitor_radio_begin(0, 6) == ESP_OK);
    fake_cca_step(80u, 60u);         /* window B: busy = 13 x 60 = 780 */
    fake_time_advance_us(10000);
    fake_time_advance_us(10000);     /* ~20 ms: slot1 due */
    monitor_radio_cca_tick(120);
    fake_time_autostep(0u);
    fake_cca_step(0u, 0u);
    monitor_radio_finish(&out);
    assert(out.util_valid);
    assert(out.util_samples == 2 && out.util_attempted == 2);
    assert(out.util_total == 2u * 0x400u);
    assert(out.util_busy == 520u + 780u);

    /* 4. late-dwell tick: remaining time < window budget -> NO arm
     * (no straddle), and the lower attempt count is reported honestly. */
    fake_cca_arm_reset(true);
    fake_cca_ctrl_set(0x80000400u);
    fake_cca_set(0u, 0u, 0u);
    fake_cca_step(80u, 40u);
    fake_time_autostep(10u);
    assert(monitor_radio_begin(0, 6) == ESP_OK);   /* slot0 = attempt 1 */
    fake_time_autostep(0u);            /* freeze; control elapsed exactly */
    fake_time_advance_us(117000u);     /* elapsed 117 ms, remaining 3 ms */
    uint32_t arms = fake_cca_set_cnt_calls();
    monitor_radio_cca_tick(120);
    assert(fake_cca_set_cnt_calls() == arms);       /* nothing armed */
    monitor_radio_finish(&out);
    assert(out.util_valid);
    assert(out.util_attempted == 1 && out.util_samples == 1);

    /* 5. failed begin invalidates everything pooled before it: finish
     * after a failed begin publishes nothing (stale-window regression). */
    fake_cca_ctrl_set(0x80000400u);
    fake_cca_set(0u, 0u, 0u);
    fake_time_autostep(10u);
    fake_cca_step(80u, 40u);
    assert(monitor_radio_begin(0, 6) == ESP_OK);
    fake_wifi_set_channel_result(ESP_ERR_INVALID_STATE);
    assert(monitor_radio_begin(0, 6) == ESP_ERR_INVALID_STATE);
    fake_wifi_set_channel_result(ESP_OK);
    fake_time_autostep(0u);
    fake_cca_step(0u, 0u);
    monitor_radio_finish(&out);
    assert(!out.util_valid);

    /* 6. repeat finish republishes NOTHING and ticks after finish arm
     * nothing (pool cleared, active gate — stale-sum regression). */
    fake_cca_arm_reset(true);
    fake_cca_ctrl_set(0x80000400u);
    fake_cca_set(0u, 0u, 0u);
    fake_cca_step(80u, 40u);
    fake_time_autostep(10u);
    assert(monitor_radio_begin(0, 6) == ESP_OK);
    monitor_radio_finish(&out);
    assert(out.util_valid && out.util_attempted == 1);
    MonitorObservation again;
    memset(&again, 0, sizeof again);
    monitor_radio_finish(&again);
    assert(!again.util_valid);        /* second finish: nothing */
    arms = fake_cca_set_cnt_calls();
    fake_time_advance_us(10000);
    monitor_radio_cca_tick(120);
    assert(fake_cca_set_cnt_calls() == arms);   /* ticks after finish */

    /* 7. MIXED validity in ONE dwell: valid / invalid / valid ->
     * samples=2, attempted=3, sums count ONLY the valid windows. The
     * middle window fails because busy > total (B runs past A). */
    fake_cca_arm_reset(true);
    fake_cca_ctrl_set(0x80000400u);
    fake_cca_set(0u, 0u, 0u);
    fake_cca_step(80u, 40u);
    fake_time_autostep(10u);
    assert(monitor_radio_begin(0, 6) == ESP_OK);      /* w1: valid */
    fake_time_advance_us(10000);
    monitor_radio_cca_tick(120);      /* ~10 ms: slot0 consumed, skip */
    fake_cca_step(80u, 200u);      /* w2: busy=13x200=2600 > total=1024 */
    fake_time_advance_us(10000);
    monitor_radio_cca_tick(120);      /* ~20 ms: slot1 attempt — INVALID */
    fake_cca_step(80u, 40u);
    fake_time_advance_us(10000);
    monitor_radio_cca_tick(120);      /* ~30 ms: slot2 attempt — valid */
    fake_time_autostep(0u);
    fake_cca_step(0u, 0u);
    monitor_radio_finish(&out);
    assert(out.util_valid);
    assert(out.util_samples == 2 && out.util_attempted == 3);
    assert(out.util_total == 2u * 0x400u);   /* only the two valid ones */
    assert(out.util_busy == 520u + 520u);
    assert(out.util_window_us_upper >= out.util_samples &&
           out.util_window_us_upper <= 5000u * out.util_samples);
    ev.observation = &out;
    n = monitor_format_channel_event(json, sizeof json, &ev);
    assert(n > 0);
    printf("%s\n", json);            /* second pooled JSON: mixed counts */

    /* 8. LATENCY proof: one tick jumping SEVERAL slots attempts exactly
     * ONE window (never a catch-up burst), and further ticks inside the
     * same time slot attempt nothing. */
    fake_cca_arm_reset(true);
    fake_cca_ctrl_set(0x80000400u);
    fake_cca_set(0u, 0u, 0u);
    fake_cca_step(80u, 40u);
    fake_time_autostep(10u);
    assert(monitor_radio_begin(0, 6) == ESP_OK);      /* slot 0: 1 arm */
    arms = fake_cca_set_cnt_calls();
    fake_time_advance_us(70000);      /* jump ~4 slots at once */
    monitor_radio_cca_tick(120);
    assert(fake_cca_set_cnt_calls() == arms + 1u);     /* exactly ONE */
    monitor_radio_cca_tick(120);      /* same instant: same slot */
    assert(fake_cca_set_cnt_calls() == arms + 1u);     /* no attempt */
    fake_time_advance_us(1000);       /* still inside the same slot */
    monitor_radio_cca_tick(120);
    assert(fake_cca_set_cnt_calls() == arms + 1u);     /* no attempt */
    fake_time_autostep(0u);
    fake_cca_step(0u, 0u);
    monitor_radio_finish(&out);

    printf("radio-util-pooled ok\n");
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
    if (strcmp(argv[1], "radio-util") == 0) {
        case_radio_util();
        return 0;
    }
    if (strcmp(argv[1], "radio-util-pooled") == 0) {
        case_radio_util_pooled();
        return 0;
    }
    fprintf(stderr, "unknown case: %s\n", argv[1]);
    return 2;
}
