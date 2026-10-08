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
 * radio_state.lock. The callback performs bounded work only: no heap, no
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
 * reported as channel_error. Band 0 = 2.4 GHz, band 1 = 5 GHz.
 * cca_attempts (validated 1..32 by config; clamped here defensively:
 * 0 -> default 16, >32 -> 32) selects how many sampled-CCA windows this
 * dwell schedules; the effective dwell must budget 5 ms per attempt
 * (monitor_dwell_ms does). */
esp_err_t monitor_radio_begin(uint8_t band, uint8_t channel,
                              uint8_t cca_attempts);

/* Stop timing and reception, then copy out the complete observation
 * (counters, bounded AP sightings, rounded observed_ms). Only call after a
 * successful monitor_radio_begin; out must be non-NULL. Retune/USB time is
 * excluded from observed_ms. */
void monitor_radio_finish(MonitorObservation *out);

/* Receive-start timestamp of the current dwell: set after tuning and
 * reception are enabled, BEFORE the slot-0 CCA window (so the window cost
 * sits inside the dwell, retune cost outside). Valid only after a
 * successful monitor_radio_begin; the application uses it as THE deadline
 * basis so the dwell loop, the CCA scheduler, and observed_ms share one
 * receive-start clock. Task-only. */
int64_t monitor_radio_receive_start_us(void);

/* Sampled PHY CCA (CURRENT pooled contract, no version key): up to
 * cca_attempts (1..32, default 16) distributed one-shot windows per
 * dwell. monitor_radio_begin attempts slot 0 (dwell start); the
 * application calls this once per dwell-wait tick (1 ms cadence: 32
 * attempts fit a 160 ms dwell).
 * Scheduling uses ACTUAL elapsed time since begin:
 * slots are distributed over [0, dwell - 5000us) —
 * slot = floor(elapsed_us * attempts / (dwell_us - 5000us)) — so every
 * due slot still has the full window budget left (end-of-dwell tail).
 * A slot below the next pending index returns immediately (missed slots
 * are skipped, never burst), and at most ONE window is attempted per
 * call. Ticks in the tail (< one window budget remaining) skip entirely
 * so a window never straddles the dwell. Each window keeps the strict
 * per-window bounds (reset proof, done+endpoint, <=5000 us, <=4M polls —
 * frozen-clock safe). No-op when no dwell is active. */
void monitor_radio_cca_tick(uint32_t dwell_ms);

#endif /* MONITOR_RADIO_H */
