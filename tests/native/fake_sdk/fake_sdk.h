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
/* Gated auto-steps (test scaffolding for the util one-shot loop): default
 * off; fixture enables only around a util case, restores after. */
void fake_time_autostep(uint32_t step_us);
void fake_cca_step(uint32_t a_step, uint32_t b_step);

/* CCA counter telemetry fake (read-only phy_get_cca_cnt seam): returns the
 * values planted by fake_cca_set plus the status flag. */
uint32_t phy_get_cca_cnt(uint32_t out[2]);
void fake_cca_set(uint32_t a, uint32_t b, uint32_t flag);

/* Control word 0x600a7c58 seam for MONITOR_CCA_CTRL_TEST builds. */
uint32_t fake_cca_ctrl_read(void);
void fake_cca_ctrl_set(uint32_t value);

/* Recorder for the authorized one-shot arm call (native seam). */
void phy_set_cca_cnt(uint32_t value, uint32_t arm);
uint32_t fake_cca_set_cnt_calls(void);
uint32_t fake_cca_set_cnt_value(void);
uint32_t fake_cca_set_cnt_arm(void);

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
