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
    *out = result;
}
