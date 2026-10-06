/* Fake esp_netif: succeeds unconditionally. */
#ifndef FAKE_SDK_ESP_NETIF_H
#define FAKE_SDK_ESP_NETIF_H

#include "esp_err.h"

esp_err_t esp_netif_init(void);

#endif /* FAKE_SDK_ESP_NETIF_H */
