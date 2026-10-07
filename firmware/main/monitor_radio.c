#include "monitor_radio.h"

#include <string.h>

#include "esp_event.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

/* Bound: one copied beacon record; dump_len is clamped to this prefix. */
#define SIGHTING_COPY_BYTES 256
/* Bound: sighting queue depth; overflow counts toward access_points_dropped. */
#define SIGHTING_QUEUE_DEPTH 32
/* Armed one-shot window bound (contract FINAL): ~10 us read spacing from
 * t0 (BEFORE the setter) up to first-done; hard caps 5 ms AND 4M poll
 * iterations (the iteration cap also bounds a frozen clock). No tail. */
#define CCA_UTIL_WINDOW_BUDGET_US 5000

/* Private v6.0.3 libphy entry (phy_feature.o): reads the two 27-bit CCA
 * counters at 0x600a7c5c/0x600a7c60 and returns the bit27 status flag;
 * a0 = out[2] (signature from disasm, evidence/cca-source-evidence.txt).
 * READ-ONLY diagnostic telemetry: the companion phy_set_cca_cnt is never
 * called, and no register is written from this firmware. Counter semantics
 * are being established empirically, raw values only. */
extern uint32_t phy_get_cca_cnt(uint32_t out[2]);

/* Exact SDK setter (contract proposal): called once per VALID dwell with
 * the control-word value read at the call site (ctrl & 0x07ffffff) and
 * arm=1; upper bits preserved by the SDK keep-mask. No other register
 * writes exist in this firmware. */
extern void phy_set_cca_cnt(uint32_t value, uint32_t arm);

#define CCA_COUNTER_MASK 0x07ffffffu

/* Per-dwell armed one-shot, ENDPOINT form (contract FINAL v1): minimal
 * state only. window_us_upper = t_after - t0 with t0 BEFORE the setter and
 * t_after AFTER the getter read that observed done (flag==1 && A==param). */
static bool g_util_armed;              /* window sampled for current dwell */
static uint32_t g_util_value;          /* parameter read at the call site */
static uint8_t g_util_reset_ok;        /* first read: flag==0 && A < value */
static bool g_util_done;               /* completion observed (strict) */
static uint32_t g_util_upper;          /* t_after - t0, bounded (0=invalid) */
static uint32_t g_util_a_final;        /* raw A at completion (== value) */
static uint32_t g_util_b_final;        /* raw B at completion (busy) */

#ifdef MONITOR_CCA_CTRL_TEST
/* Native seam: fake_sdk supplies the control word (adapter test build). */
uint32_t fake_cca_ctrl_read(void);
static uint32_t cca_ctrl_read(void) {
    return fake_cca_ctrl_read();
}
#else
static uint32_t cca_ctrl_read(void) {
    /* Direct read of control word 0x600a7c58 — the exact address
     * phy_set_cca_cnt loads in its own RMW (phy_feature.o, lw at +0x4;
     * evidence/cca-set-cca-full-disasm.txt). Read-only; no SDK getter
     * exists, but the address is proven by that source-backed load. */
    return *(volatile uint32_t *)0x600a7c58u;
}
#endif

typedef struct {
    uint32_t generation;            /* stale-generation records are dropped */
    int8_t rssi_dbm;
    uint16_t length_bytes;          /* up to SIGHTING_COPY_BYTES; uint8_t
                                     * would collapse 256 to 0 */
    uint8_t frame_bytes[SIGHTING_COPY_BYTES];
} MonitorSightingRecord;

static portMUX_TYPE g_observation_lock = portMUX_INITIALIZER_UNLOCKED;
static MonitorObservation g_observation;
static bool g_capture_active;
static uint32_t g_generation;
static uint8_t g_active_channel;
static int64_t g_start_us;
static QueueHandle_t g_sighting_queue;

static void promiscuous_callback(void *buffer, wifi_promiscuous_pkt_type_t type) {
    if (type == WIFI_PKT_MISC || buffer == NULL) {
        return;                         /* not a mgmt/data/control reception */
    }
    const wifi_promiscuous_pkt_t *packet = (const wifi_promiscuous_pkt_t *)buffer;
    const wifi_pkt_rx_ctrl_t *rx = &packet->rx_ctrl;
    if (rx->rx_state != 0) {
        return;                         /* failed reception: not a packet */
    }
    int8_t rssi_dbm = (int8_t)rx->rssi;
    uint8_t rx_channel = (uint8_t)rx->channel;
    uint32_t generation = 0;
    bool accepted = false;
    bool is_management = (type == WIFI_PKT_MGMT);

    portENTER_CRITICAL(&g_observation_lock);
    if (g_capture_active && rx_channel == g_active_channel) {
        monitor_observation_record_packet(&g_observation, rssi_dbm);
        generation = g_generation;
        accepted = true;
    }
    portEXIT_CRITICAL(&g_observation_lock);
    if (!accepted) {
        return;
    }
    if (!is_management) {
        return;                         /* counted; payloads never copied */
    }
    MonitorSightingRecord record;
    size_t copy_bytes = rx->dump_len;   /* excludes FCS on C5: do not subtract */
    if (copy_bytes > SIGHTING_COPY_BYTES) {
        copy_bytes = SIGHTING_COPY_BYTES;
    }
    record.generation = generation;
    record.rssi_dbm = rssi_dbm;
    record.length_bytes = (uint16_t)copy_bytes;
    memcpy(record.frame_bytes, packet->payload, copy_bytes);
    if (g_sighting_queue != NULL &&
        xQueueSend(g_sighting_queue, &record, 0) != pdTRUE) {
        portENTER_CRITICAL(&g_observation_lock);
        if (g_capture_active && generation == g_generation) {
            g_observation.access_points_dropped++;
        }
        portEXIT_CRITICAL(&g_observation_lock);
    }
}

esp_err_t monitor_radio_init(void) {
    esp_err_t err = esp_netif_init();
    if (err != ESP_OK) {
        return err;
    }
    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    err = esp_wifi_init(&init_config);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_wifi_set_storage(WIFI_STORAGE_RAM);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_wifi_set_mode(WIFI_MODE_NULL);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_wifi_set_country_code("JP", false);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_wifi_start();
    if (err != ESP_OK) {
        return err;
    }
    err = esp_wifi_set_ps(WIFI_PS_NONE);
    if (err != ESP_OK) {
        return err;
    }
    wifi_promiscuous_filter_t filter = {
        .filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT |
                       WIFI_PROMIS_FILTER_MASK_DATA |
                       WIFI_PROMIS_FILTER_MASK_CTRL,
    };
    err = esp_wifi_set_promiscuous_filter(&filter);
    if (err != ESP_OK) {
        return err;
    }
    wifi_promiscuous_filter_t control_subtypes = {.filter_mask = UINT32_MAX};
    err = esp_wifi_set_promiscuous_ctrl_filter(&control_subtypes);
    if (err != ESP_OK) {
        return err;
    }
    g_sighting_queue = xQueueCreate(SIGHTING_QUEUE_DEPTH,
                                    sizeof(MonitorSightingRecord));
    if (g_sighting_queue == NULL) {
        return ESP_ERR_NO_MEM;
    }
    return esp_wifi_set_promiscuous_rx_cb(promiscuous_callback);
}

esp_err_t monitor_radio_begin(uint8_t band, uint8_t channel) {
    /* Invalidate any previous CCA window at ENTRY: a failed or repeated
     * begin can never leave a stale window for a later finish to reuse. */
    g_util_armed = false;               /* stale window can never survive */
    esp_err_t err = esp_wifi_set_promiscuous(false);
    if (err != ESP_OK) {
        return err;
    }
    wifi_band_mode_t band_mode =
        band == 0 ? WIFI_BAND_MODE_2G_ONLY : WIFI_BAND_MODE_5G_ONLY;
    err = esp_wifi_set_band_mode(band_mode);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
    if (err != ESP_OK) {
        return err;
    }
    /* Drain stale records from earlier generations before arming. */
    MonitorSightingRecord stale;
    while (g_sighting_queue != NULL &&
           xQueueReceive(g_sighting_queue, &stale, 0) == pdTRUE) {
    }
    portENTER_CRITICAL(&g_observation_lock);
    monitor_observation_begin(&g_observation, band, channel);
    g_active_channel = channel;
    g_generation++;
    g_capture_active = true;
    portEXIT_CRITICAL(&g_observation_lock);

    err = esp_wifi_set_promiscuous(true);
    if (err != ESP_OK) {
        portENTER_CRITICAL(&g_observation_lock);
        g_capture_active = false;
        portEXIT_CRITICAL(&g_observation_lock);
        return err;
    }
    g_start_us = esp_timer_get_time();  /* reception enabled before timing */
    /* Per-dwell armed one-shot (contract proposal): exact SDK call, value
     * read from the control word at THIS call site, arm=1, short finite
     * window, minimal state. Bounds: <=5000 us AND <=4M poll iterations
     * (every loop, frozen-clock safe) with ~10 us read spacing; STOP at the
     * first done observation (flag==1 && A==param) — no tail sampling, no
     * per-interval sums. Validity decided at finish. */
    g_util_armed = false;
    {
        uint32_t value = cca_ctrl_read() & CCA_COUNTER_MASK;
        int64_t t0 = esp_timer_get_time();     /* BEFORE the setter */
        phy_set_cca_cnt(value, 1u);
        uint32_t polls = 0;
        uint16_t samples = 0;
        bool reset_ok = false;
        bool done = false;
        uint32_t upper = 0;
        uint32_t a_fin = 0;
        uint32_t b_fin = 0;
        uint32_t next_us = 0;
        while (true) {
            if (++polls > 4000000u) {
                break;               /* finite even if the clock never runs */
            }
            int64_t dt = esp_timer_get_time() - t0;
            if (dt > (int64_t)CCA_UTIL_WINDOW_BUDGET_US) {
                break;
            }
            if (dt < 0) {
                dt = 0;              /* no negative cast anywhere */
            }
            if ((uint32_t)dt < next_us) {
                continue;            /* ~10 us spacing between reads */
            }
            next_us = (uint32_t)dt + 10u;
            uint32_t pair[2];
            uint8_t flag = (uint8_t)(phy_get_cca_cnt(pair) & 1u);
            samples++;
            if (samples == 1) {
                /* reset proof: first read sees the counter NOT completed */
                reset_ok = (flag == 0 && pair[0] < value);
                if (flag == 1 || pair[0] >= value) {
                    break;            /* completed before first read -> gap */
                }
            }
            if (flag == 1 && pair[0] == value) {
                int64_t t_after = esp_timer_get_time(); /* AFTER the read
                                                         * that observed done */
                int64_t upper64 = t_after - t0;
                done = true;
                upper = (upper64 > 0 &&
                         upper64 <= (int64_t)CCA_UTIL_WINDOW_BUDGET_US)
                            ? (uint32_t)upper64 : 0u;
                a_fin = pair[0];
                b_fin = pair[1];
                break;                /* stop immediately at first done */
            }
        }
        g_util_value = value;
        g_util_reset_ok = reset_ok;
        g_util_done = done;
        g_util_upper = upper;
        g_util_a_final = a_fin;
        g_util_b_final = b_fin;
        g_util_armed = samples > 0;
    }
    return ESP_OK;
}

void monitor_radio_finish(MonitorObservation *out) {
    if (out == NULL) {
        return;
    }
    int64_t stop_us = esp_timer_get_time();
    uint32_t generation;
    portENTER_CRITICAL(&g_observation_lock);
    g_capture_active = false;
    generation = g_generation;
    portEXIT_CRITICAL(&g_observation_lock);
    (void)esp_wifi_set_promiscuous(false);

    MonitorObservation result;
    portENTER_CRITICAL(&g_observation_lock);
    result = g_observation;
    portEXIT_CRITICAL(&g_observation_lock);

    MonitorSightingRecord record;
    while (g_sighting_queue != NULL &&
           xQueueReceive(g_sighting_queue, &record, 0) == pdTRUE) {
        if (record.generation != generation) {
            continue;
        }
        monitor_observation_record_sighting(&result, record.frame_bytes,
                                            record.length_bytes,
                                            record.rssi_dbm);
    }
    int64_t observed_us = stop_us - g_start_us;
    if (observed_us < 0) {
        observed_us = 0;
    }
    uint32_t observed_ms = (uint32_t)((observed_us + 500) / 1000);
    monitor_observation_finalize(&result, observed_ms);
    if (g_util_armed) {
        bool valid = g_util_reset_ok && g_util_done &&
                     g_util_upper > 0 &&
                     g_util_upper <= CCA_UTIL_WINDOW_BUDGET_US &&
                     g_util_a_final == g_util_value &&
                     g_util_a_final > 0 && g_util_a_final <= CCA_COUNTER_MASK &&
                     g_util_b_final <= g_util_a_final;
        if (valid) {
            result.util_valid = true;
            result.util_window_us_upper = g_util_upper;
            result.util_busy = g_util_b_final;
            result.util_total = g_util_a_final;
        }
    }
    g_util_armed = false;            /* one-shot: never resampled */
    *out = result;
}
