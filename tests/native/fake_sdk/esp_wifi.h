/* Fake esp_wifi for host seam tests of monitor_radio.c.
 *
 * wifi_pkt_rx_ctrl_t exposes the *fields the adapter uses* (rssi/rx_state/
 * channel/dump_len) as plain fixed-width members (CODING_GUIDELINE: no
 * bit-field layout as a portable packet definition; the real SDK header is
 * vendor code and keeps its bit-fields). Adapter and header share this
 * layout, so the seam tests exercise adapter logic (clamping, channel
 * acceptance, type gating), not SDK struct packing. */
#ifndef FAKE_SDK_ESP_WIFI_H
#define FAKE_SDK_ESP_WIFI_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

typedef struct {
    int unused;
} wifi_init_config_t;

#define WIFI_INIT_CONFIG_DEFAULT() ((wifi_init_config_t){0})

typedef enum {
    WIFI_STORAGE_RAM = 1,
} wifi_storage_t;

typedef enum {
    WIFI_MODE_NULL = 0,
} wifi_mode_t;

typedef enum {
    WIFI_PS_NONE = 0,
} wifi_ps_type_t;

typedef enum {
    WIFI_BAND_MODE_2G_ONLY = 1,
    WIFI_BAND_MODE_5G_ONLY = 2,
    WIFI_BAND_MODE_AUTO = 3,
} wifi_band_mode_t;

typedef enum {
    WIFI_SECOND_CHAN_NONE = 0,
} wifi_second_chan_t;

typedef enum {
    WIFI_PKT_MGMT,
    WIFI_PKT_CTRL,
    WIFI_PKT_DATA,
    WIFI_PKT_MISC,
} wifi_promiscuous_pkt_type_t;

#define WIFI_PROMIS_FILTER_MASK_MGMT (1)
#define WIFI_PROMIS_FILTER_MASK_CTRL (1 << 1)
#define WIFI_PROMIS_FILTER_MASK_DATA (1 << 2)

typedef struct {
    uint32_t filter_mask;
} wifi_promiscuous_filter_t;

typedef struct {
    int8_t rssi;
    uint8_t rx_state;
    uint8_t channel;
    uint16_t dump_len;
} wifi_pkt_rx_ctrl_t;

typedef struct {
    wifi_pkt_rx_ctrl_t rx_ctrl;
    uint8_t payload[0];
} wifi_promiscuous_pkt_t;

typedef void (*wifi_promiscuous_cb_t)(void *buf,
                                      wifi_promiscuous_pkt_type_t type);

esp_err_t esp_wifi_init(const wifi_init_config_t *config);
esp_err_t esp_wifi_set_storage(wifi_storage_t storage);
esp_err_t esp_wifi_set_mode(wifi_mode_t mode);
esp_err_t esp_wifi_set_country_code(const char *country, bool ieee80211d_enabled);
esp_err_t esp_wifi_start(void);
esp_err_t esp_wifi_set_ps(wifi_ps_type_t type);
esp_err_t esp_wifi_set_promiscuous_filter(const wifi_promiscuous_filter_t *filter);
esp_err_t esp_wifi_set_promiscuous_ctrl_filter(const wifi_promiscuous_filter_t *filter);
esp_err_t esp_wifi_set_promiscuous_rx_cb(wifi_promiscuous_cb_t cb);
esp_err_t esp_wifi_set_promiscuous(bool enable);
esp_err_t esp_wifi_set_band_mode(wifi_band_mode_t band_mode);
esp_err_t esp_wifi_set_channel(uint8_t primary, wifi_second_chan_t second);

#endif /* FAKE_SDK_ESP_WIFI_H */
