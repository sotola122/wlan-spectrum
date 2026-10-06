/* Fake esp_event: succeeds unconditionally. */
#ifndef FAKE_SDK_ESP_EVENT_H
#define FAKE_SDK_ESP_EVENT_H

#include "esp_err.h"

esp_err_t esp_event_loop_create_default(void);

#endif /* FAKE_SDK_ESP_EVENT_H */
