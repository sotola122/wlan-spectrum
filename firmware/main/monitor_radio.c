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
 * called from here beyond the explicit arm call below, and no register is
 * written from this firmware. Counter semantics
 * are being established empirically, raw values only. */
extern uint32_t phy_get_cca_cnt(uint32_t out[2]);

/* Exact SDK setter (contract proposal): called once per armed window with
 * the control-word value read at the call site (ctrl & 0x07ffffff) and
 * arm=1; upper bits preserved by the SDK keep-mask. No other register
 * writes exist in this firmware. */
extern void phy_set_cca_cnt(uint32_t counter_limit, uint32_t arm);

#define CCA_COUNTER_MASK 0x07ffffffu

/* Per-dwell pooled CCA state (CURRENT contract, no version key): EIGHT
 * distributed one-shot attempts at j*dwell/8, each strictly validated. */
#define CCA_SLOTS 8u
typedef struct {
    bool dwell_open;            /* dwell open: ticks may schedule */
    uint8_t next_slot;          /* lowest slot index still pendable */
    uint8_t attempted_windows;  /* windows actually attempted, <=8 */
    uint8_t valid_windows;      /* windows that passed validation */
    uint32_t busy_sum_ticks;    /* sum of valid busy_ticks: <= 8 x mask */
    uint32_t total_sum_ticks;   /* sum of valid totals: <= 8 x mask */
    uint32_t window_sum_us;     /* sum of valid brackets: <= 40000 */
} CcaState;   /* APP-TASK-ONLY: begin / finish / tick. */

static void cca_attempt_once(void);   /* defined below monitor_radio_begin */

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

/* Single statically allocated owner for ALL radio state — storage class
 * and C ABI unchanged. Concurrency contract: observation, capture_active,
 * active_channel and generation change only under radio_state.lock
 * (critical section). sighting_queue is created once before the callback
 * is installed; its FreeRTOS queue operations are thread-safe. start_us
 * and the CCA pool are app-task-only (begin / finish / tick). */
typedef struct {
    portMUX_TYPE lock;
    MonitorObservation observation;
    bool capture_active;
    uint32_t generation;
    uint8_t active_channel;
    int64_t start_us;              /* app-task-only */
    CcaState cca;                  /* app-task-only */
    QueueHandle_t sighting_queue;
} RadioState;

static RadioState radio_state = {
    .lock = portMUX_INITIALIZER_UNLOCKED,
};

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

    portENTER_CRITICAL(&radio_state.lock);
    if (radio_state.capture_active && rx_channel == radio_state.active_channel) {
        monitor_observation_record_packet(&radio_state.observation, rssi_dbm);
        generation = radio_state.generation;
        accepted = true;
    }
    portEXIT_CRITICAL(&radio_state.lock);
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
    if (radio_state.sighting_queue != NULL &&
        xQueueSend(radio_state.sighting_queue, &record, 0) != pdTRUE) {
        portENTER_CRITICAL(&radio_state.lock);
        if (radio_state.capture_active && generation == radio_state.generation) {
            radio_state.observation.access_points_dropped++;
        }
        portEXIT_CRITICAL(&radio_state.lock);
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
    radio_state.sighting_queue = xQueueCreate(SIGHTING_QUEUE_DEPTH,
                                    sizeof(MonitorSightingRecord));
    if (radio_state.sighting_queue == NULL) {
        return ESP_ERR_NO_MEM;
    }
    return esp_wifi_set_promiscuous_rx_cb(promiscuous_callback);
}

esp_err_t monitor_radio_begin(uint8_t band, uint8_t channel) {
    /* Invalidate any previous CCA window at ENTRY: a failed or repeated
     * begin can never leave a stale window for a later finish to reuse.
     * Only the CCA pool resets — lock and queue stay untouched. */
    radio_state.cca = (CcaState){0};
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
    while (radio_state.sighting_queue != NULL &&
           xQueueReceive(radio_state.sighting_queue, &stale, 0) == pdTRUE) {
    }
    portENTER_CRITICAL(&radio_state.lock);
    monitor_observation_begin(&radio_state.observation, band, channel);
    radio_state.active_channel = channel;
    radio_state.generation++;
    radio_state.capture_active = true;
    portEXIT_CRITICAL(&radio_state.lock);

    err = esp_wifi_set_promiscuous(true);
    if (err != ESP_OK) {
        portENTER_CRITICAL(&radio_state.lock);
        radio_state.capture_active = false;
        portEXIT_CRITICAL(&radio_state.lock);
        return err;
    }
    radio_state.start_us = esp_timer_get_time();  /* reception enabled before timing */
    /* Pooled CCA: activate scheduling and attempt slot 0 at the dwell
     * start. Ticks 1..7 are scheduled from ACTUAL elapsed time by
     * monitor_radio_cca_tick. */
    radio_state.cca.dwell_open = true;
    cca_attempt_once();
    radio_state.cca.next_slot = 1;   /* slot 0 consumed */
    return ESP_OK;
}

/* One bounded armed window, strictly validated, folded into the pooled
 * sums. Bounds: wall bracket <= 5000 us AND <= 4M poll
 * iterations (frozen clock exits via the iteration cap). */
static void cca_attempt_once(void) {
    uint32_t counter_limit = cca_ctrl_read() & CCA_COUNTER_MASK;
    int64_t t0 = esp_timer_get_time();     /* BEFORE the setter */
    phy_set_cca_cnt(counter_limit, 1u);
    uint32_t polls = 0;
    uint16_t poll_reads = 0;       /* read count, NOT contract samples */
    bool reset_ok = false;
    bool done = false;
    uint32_t window_us = 0;
    uint32_t total = 0;            /* endpoint A at completion */
    uint32_t busy_ticks = 0;       /* endpoint B at completion */
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
        uint32_t counts[2];
        uint8_t flag = (uint8_t)(phy_get_cca_cnt(counts) & 1u);
        poll_reads++;
        if (poll_reads == 1) {
            /* reset proof: first read sees the counter NOT completed */
            reset_ok = (flag == 0 && counts[0] < counter_limit);
            if (flag == 1 || counts[0] >= counter_limit) {
                break;            /* completed before first read -> gap */
            }
        }
        if (flag == 1 && counts[0] == counter_limit) {
            int64_t t_after = esp_timer_get_time(); /* AFTER the read
                                                     * that observed done */
            int64_t meas_us = t_after - t0;
            done = true;
            window_us = (meas_us > 0 &&
                         meas_us <= (int64_t)CCA_UTIL_WINDOW_BUDGET_US)
                            ? (uint32_t)meas_us : 0u;
            total = counts[0];
            busy_ticks = counts[1];
            break;                /* stop immediately at first done */
        }
    }
    radio_state.cca.attempted_windows++;
    bool valid = reset_ok && done &&
                 window_us > 0 &&
                 window_us <= CCA_UTIL_WINDOW_BUDGET_US &&
                 total == counter_limit &&
                 total > 0 && total <= CCA_COUNTER_MASK &&
                 busy_ticks <= total;
    if (valid) {
        radio_state.cca.valid_windows++;
        radio_state.cca.busy_sum_ticks += busy_ticks;   /* <= mask; total <= 8xmask */
        radio_state.cca.total_sum_ticks += total;
        radio_state.cca.window_sum_us += window_us;  /* <=5000; total <=40000 */
    }
}

void monitor_radio_cca_tick(uint32_t dwell_ms) {
    if (!radio_state.cca.dwell_open || radio_state.cca.next_slot >= CCA_SLOTS || dwell_ms == 0) {
        return;                        /* no dwell, done, or nothing due */
    }
    int64_t elapsed = esp_timer_get_time() - radio_state.start_us;
    if (elapsed < 0) {
        return;
    }
    uint64_t elapsed_us = (uint64_t)elapsed;
    uint64_t dwell_us = (uint64_t)dwell_ms * 1000u;
    if (elapsed_us >= dwell_us) {
        return;                        /* dwell over */
    }
    /* Never START a window that cannot finish inside the dwell: a window
     * may run up to CCA_UTIL_WINDOW_BUDGET_US (5000 us). A tick with less
     * remaining dwell skips its slot here (no wait, no catch-up) and the
     * lower attempt count is reported honestly by attempted. Bound: the
     * multiplication below is safe because elapsed_us < dwell_us <=
     * UINT32_MAX * 1000 before it (guard above, not an assumption). */
    if (dwell_us - elapsed_us < (uint64_t)CCA_UTIL_WINDOW_BUDGET_US) {
        return;
    }
    /* slot = floor(elapsed_us * 8 / dwell_us); elapsed_us < dwell_us makes
     * the result 0..7 without further range claims. */
    uint64_t slot = elapsed_us * CCA_SLOTS / dwell_us;
    if (slot < radio_state.cca.next_slot) {
        return;                        /* not due / already sampled: no new
                                        * attempt, never a burst */
    }
    radio_state.cca.next_slot = (uint8_t)(slot + 1u);
    cca_attempt_once();                /* at most ONE window per tick */
}

void monitor_radio_finish(MonitorObservation *out) {
    if (out == NULL) {
        return;
    }
    int64_t stop_us = esp_timer_get_time();
    uint32_t generation;
    portENTER_CRITICAL(&radio_state.lock);
    radio_state.capture_active = false;
    generation = radio_state.generation;
    portEXIT_CRITICAL(&radio_state.lock);
    (void)esp_wifi_set_promiscuous(false);

    MonitorObservation result;
    portENTER_CRITICAL(&radio_state.lock);
    result = radio_state.observation;
    portEXIT_CRITICAL(&radio_state.lock);

    MonitorSightingRecord record;
    while (radio_state.sighting_queue != NULL &&
           xQueueReceive(radio_state.sighting_queue, &record, 0) == pdTRUE) {
        if (record.generation != generation) {
            continue;
        }
        monitor_observation_record_sighting(&result, record.frame_bytes,
                                            record.length_bytes,
                                            record.rssi_dbm);
    }
    int64_t observed_us = stop_us - radio_state.start_us;
    if (observed_us < 0) {
        observed_us = 0;
    }
    uint32_t observed_ms = (uint32_t)((observed_us + 500) / 1000);
    monitor_observation_finalize(&result, observed_ms);
    /* Pool the dwell's valid windows: >=1 valid window publishes the SUMS
     * and honest counts; zero valid windows leave util unset (gap).
     * Gated on radio_state.cca.dwell_open so a REPEAT finish (or a finish after a
     * failed begin) republishes nothing — and the pool is cleared so no
     * stale sums survive into a later observation. */
    if (radio_state.cca.dwell_open && radio_state.cca.valid_windows > 0) {
        result.util_valid = true;
        result.util_window_us_upper = radio_state.cca.window_sum_us;
        result.util_busy = radio_state.cca.busy_sum_ticks;
        result.util_total = radio_state.cca.total_sum_ticks;
        result.util_samples = radio_state.cca.valid_windows;
        result.util_attempted = radio_state.cca.attempted_windows;
    }
    radio_state.cca = (CcaState){0};      /* dwell closed: ticks stop */
    *out = result;
}
