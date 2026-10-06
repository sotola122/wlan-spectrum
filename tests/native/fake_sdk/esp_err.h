/* Minimal stand-in for esp_err.h — host seam tests for adapter code. */
#ifndef FAKE_SDK_ESP_ERR_H
#define FAKE_SDK_ESP_ERR_H

typedef int esp_err_t;

#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NO_MEM 0x101
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_INVALID_STATE 0x103

const char *esp_err_to_name(esp_err_t code);

#endif /* FAKE_SDK_ESP_ERR_H */
