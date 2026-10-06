/*
 * Passive Wi-Fi observation adapter (ESP-IDF v6.0.3, ESP32-C5).
 *
 * Owns the promiscuous RX callback, the per-dwell observation, and the
 * bounded beacon-sighting queue. No association, scanning, SoftAP, injection,
 * or payload capture: frames are parsed in task context only and no raw
 * payload bytes leave this module.
 *
 * Concurrency: monitor_radio_begin/monitor_radio_finish are task-only (the
 * application task). The promiscuous callback runs in the Wi-Fi task; it may
 * run concurrently with begin/finish and is serialized against them by
 * g_observation_lock. The callback performs bounded work only: no heap, no
 * logging, no USB, zero-timeout queue sends.
 *
 * Memory: initialization-only allocation. The sighting queue
 * (32 x ~264 B, ~8.5 KB) is created once in monitor_radio_init; nothing is
 * allocated per packet. Vendor exception: esp_wifi_init allocates the
 * Wi-Fi driver's internal buffers at init (documented SDK behavior).
 */
#ifndef MONITOR_RADIO_H
#define MONITOR_RADIO_H

#include <stdint.h>

#include "esp_err.h"

#include "monitor_core.h"

/* Initialize netif/event/Wi-Fi in RAM-storage NULL mode, country JP,
 * promiscuous filters (mgmt/data/control + all control subtypes), and the
 * sighting queue. Returns the first SDK error; on failure the caller must
 * not observe. No allocation after this call. */
esp_err_t monitor_radio_init(void);

/* Tune band/channel, enable passive reception, and start observation
 * timing. Timing starts only after tuning and reception are enabled.
 * On failure nothing is observed and the returned SDK error should be
 * reported as channel_error. Band 0 = 2.4 GHz, band 1 = 5 GHz. */
esp_err_t monitor_radio_begin(uint8_t band, uint8_t channel);

/* Stop timing and reception, then copy out the complete observation
 * (counters, bounded AP sightings, rounded observed_ms). Only call after a
 * successful monitor_radio_begin; out must be non-NULL. Retune/USB time is
 * excluded from observed_ms. */
void monitor_radio_finish(MonitorObservation *out);

#endif /* MONITOR_RADIO_H */
