/* Fake esp_timer: fixture-controlled monotonic microseconds. */
#ifndef FAKE_SDK_ESP_TIMER_H
#define FAKE_SDK_ESP_TIMER_H

#include <stdint.h>

int64_t esp_timer_get_time(void);

#endif /* FAKE_SDK_ESP_TIMER_H */
