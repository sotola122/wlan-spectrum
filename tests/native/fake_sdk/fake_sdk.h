/* Fixture control surface for the fake SDK (host seam tests). */
#ifndef FAKE_SDK_FAKE_SDK_H
#define FAKE_SDK_FAKE_SDK_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_wifi.h"

/* time */
void fake_time_advance_us(int64_t delta_us);

/* uart */
void fake_uart_rx_push(const uint8_t *bytes, size_t length_bytes);
size_t fake_uart_rx_pending(void);
size_t fake_uart_tx_length(void);
const uint8_t *fake_uart_tx_data(void);
void fake_uart_tx_set_max_per_call(size_t max_bytes);
void fake_uart_tx_fail_next(int count);
int fake_uart_tx_pin(void);
int fake_uart_rx_pin(void);
int fake_uart_baud(void);

/* wifi */
wifi_promiscuous_cb_t fake_wifi_rx_cb(void);
bool fake_wifi_promiscuous_enabled(void);
uint8_t fake_wifi_last_channel(void);
void fake_wifi_set_channel_result(esp_err_t result);

/* tasks */
void fake_tasks_start(void);

#endif /* FAKE_SDK_FAKE_SDK_H */
