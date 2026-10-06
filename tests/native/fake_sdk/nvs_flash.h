/* Fake NVS header for host seam tests (app_main requires nvs_flash_init). */
#ifndef FAKE_SDK_NVS_FLASH_H
#define FAKE_SDK_NVS_FLASH_H

#include "esp_err.h"

#define ESP_ERR_NVS_NO_FREE_PAGES 0x1101
#define ESP_ERR_NVS_NEW_VERSION_FOUND 0x1102

esp_err_t nvs_flash_init(void);

#endif /* FAKE_SDK_NVS_FLASH_H */
