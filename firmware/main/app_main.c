/*
 * Wi-Fi monitor application: owns the active CONFIG, epoch, and cycle via
 * the MonitorRun state machine (monitor_core.c, host-tested).
 *
 * Two tasks: this application task (scheduling, JSON, mailbox) and the link
 * TX task (USB writes). Driver callbacks never format JSON or write USB.
 *
 * Memory: initialization-only allocation in this application (static JSON
 * buffer, static run state). Runtime allocation is limited to the vendor
 * Wi-Fi driver's init-time buffers (esp_wifi_init), the FreeRTOS queues
 * created in monitor_link_init / monitor_radio_init, and the transient
 * per-event cJSON tree documented in monitor_core.c. No per-packet heap use.
 */
#include "monitor_core.h"
#include "monitor_link.h"
#include "monitor_radio.h"

#include "esp_err.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#define HEARTBEAT_PERIOD_US 1000000
#define DWELL_POLL_STEP_MS 20
#define CHANNEL_ERROR_BACKOFF_LIMIT_MS 1000

/* CONFIG defaults: live, 2.4 GHz, 1000 ms, FFT 64, 20000 kHz. */
static const MonitorConfig DEFAULT_CONFIG = {
    .mode = 0,
    .band = 0,
    .sweep_ms = 1000,
    .fft_size = 64,
    .sample_rate_khz = 20000,
};

static MonitorRun g_run;
static uint8_t g_channel_list[MONITOR_MAX_CHANNELS];
static char g_json_buffer[MONITOR_JSON_CAPACITY_BYTES];
static int64_t g_next_heartbeat_us;

static uint32_t uptime_ms(void) {
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void send_json(size_t length_bytes) {
    if (length_bytes > 0) {
        (void)monitor_link_submit_json(g_json_buffer, length_bytes);
    }
}

static void send_config_event(void) {
    size_t channel_count = monitor_band_channels(
        g_run.active_config.band, g_channel_list, sizeof(g_channel_list));
    MonitorConfigEvent event = {
        .epoch = g_run.epoch,
        .config = g_run.active_config,
        .dwell_ms = monitor_dwell_ms(g_run.active_config.sweep_ms,
                                     channel_count),
        .channels = g_channel_list,
        .channel_count = channel_count,
        .tx_dropped = monitor_link_tx_dropped(),
    };
    send_json(monitor_format_config_event(g_json_buffer, sizeof(g_json_buffer),
                                          &event));
}

static void send_error(const char *code) {
    send_json(monitor_format_error_event(g_json_buffer, sizeof(g_json_buffer),
                                         code));
}

static void send_channel_event(const MonitorObservation *observation) {
    MonitorChannelEvent event = {
        .epoch = g_run.epoch,
        .cycle = g_run.cycle,
        .observation = observation,
    };
    send_json(monitor_format_channel_event(g_json_buffer, sizeof(g_json_buffer),
                                           &event));
}

static void send_cycle_event(uint32_t elapsed_ms) {
    MonitorCycleEvent event = {
        .epoch = g_run.epoch,
        .cycle = g_run.cycle,
        .band = g_run.active_config.band,
        .elapsed_ms = elapsed_ms,
        .uptime_ms = uptime_ms(),
    };
    send_json(monitor_format_cycle_event(g_json_buffer, sizeof(g_json_buffer),
                                         &event));
}

static void send_channel_error(uint8_t channel, const char *code) {
    MonitorChannelErrorEvent event = {
        .epoch = g_run.epoch,
        .cycle = g_run.cycle,
        .band = g_run.active_config.band,
        .channel = channel,
        .code = code,
    };
    send_json(monitor_format_channel_error_event(g_json_buffer,
                                                 sizeof(g_json_buffer),
                                                 &event));
}

static void heartbeat_if_due(void) {
    int64_t now_us = esp_timer_get_time();
    if (now_us >= g_next_heartbeat_us) {
        g_next_heartbeat_us = now_us + HEARTBEAT_PERIOD_US;
        send_config_event();
    }
}

/* Poll the link during waits: feed the CONFIG parser, surface rate-limited
 * invalid_config errors, and heartbeat. The pending-config mailbox is NOT
 * consumed here — a changed CONFIG must survive until a channel boundary. */
static void poll_link(void) {
    monitor_link_poll();
    if (monitor_link_take_invalid_config()) {
        send_error("invalid_config");
    }
    heartbeat_if_due();
}

/* Channel-boundary check: consumes the mailbox and applies the run-state
 * decision (ack identical, apply+epoch++ changed, idle otherwise). */
static MonitorBoundary check_boundary(void) {
    MonitorConfig incoming;
    bool have_pending = monitor_link_take_config(&incoming);
    return monitor_run_boundary(&g_run, &incoming, have_pending);
}

static void fatal_error_loop(const char *code) {
    for (;;) {
        send_error(code);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

void app_main(void) {
    monitor_run_init(&g_run, &DEFAULT_CONFIG);

    esp_err_t err = monitor_link_init();
    if (err != ESP_OK) {
        return;                     /* no transport: nothing else can run */
    }
    err = nvs_flash_init();
    if (err != ESP_OK) {
        /* No erase fallback: an exhausted/version-mismatched NVS must be
         * reported to the operator, never wiped automatically. */
        fatal_error_loop(esp_err_to_name(err));
    }
    err = monitor_radio_init();
    if (err != ESP_OK) {
        fatal_error_loop(esp_err_to_name(err));
    }

    send_config_event();
    g_next_heartbeat_us = esp_timer_get_time() + HEARTBEAT_PERIOD_US;

    for (;;) {
        size_t channel_count = monitor_band_channels(
            g_run.active_config.band, g_channel_list, sizeof(g_channel_list));
        if (channel_count == 0) {
            send_error("no_channels");
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        uint32_t dwell_ms =
            monitor_dwell_ms(g_run.active_config.sweep_ms, channel_count);
        (void)monitor_run_begin_cycle(&g_run);
        int64_t cycle_start_us = esp_timer_get_time();
        bool cycle_discarded = false;

        for (size_t i = 0; i < channel_count; i++) {
            /* Channel boundary: a changed CONFIG applies here and discards
             * this incomplete cycle; an identical one is re-acknowledged.
             * On APPLY the new epoch's config event MUST go out before any
             * new-epoch measurement: hosts ignore measurements until the
             * matching echo arrives. */
            MonitorBoundary boundary = check_boundary();
            if (boundary == MONITOR_BOUNDARY_APPLY) {
                send_config_event();
                cycle_discarded = true;
                break;
            }
            if (boundary == MONITOR_BOUNDARY_ACK) {
                send_config_event();
            }
            uint8_t channel = g_channel_list[i];
            esp_err_t tune_err =
                monitor_radio_begin(g_run.active_config.band, channel);
            if (tune_err != ESP_OK) {
                send_channel_error(channel, esp_err_to_name(tune_err));
                /* Bound the error rate; keep polling (not consuming) link. */
                for (uint32_t waited = 0;
                     waited < CHANNEL_ERROR_BACKOFF_LIMIT_MS;
                     waited += DWELL_POLL_STEP_MS) {
                    poll_link();
                    MonitorBoundary backoff_boundary = check_boundary();
                    if (backoff_boundary == MONITOR_BOUNDARY_APPLY) {
                        send_config_event();    /* ack new epoch first */
                        cycle_discarded = true;
                        break;
                    }
                    if (backoff_boundary == MONITOR_BOUNDARY_ACK) {
                        send_config_event();
                    }
                    vTaskDelay(pdMS_TO_TICKS(DWELL_POLL_STEP_MS));
                }
                if (cycle_discarded) {
                    break;
                }
                continue;
            }
            /* Dwell wait: poll every 20 ms; a changed CONFIG applies only
             * at the next channel boundary, never mid-dwell. */
            for (uint32_t waited = 0; waited < dwell_ms;
                 waited += DWELL_POLL_STEP_MS) {
                vTaskDelay(pdMS_TO_TICKS(DWELL_POLL_STEP_MS));
                poll_link();
            }
            MonitorObservation observation;
            monitor_radio_finish(&observation);
            send_channel_event(&observation);
            if (monitor_link_take_invalid_config()) {
                send_error("invalid_config");
            }
            heartbeat_if_due();
        }
        if (!cycle_discarded) {
            uint64_t elapsed_ms =
                (uint64_t)(esp_timer_get_time() - cycle_start_us) / 1000;
            send_cycle_event((uint32_t)elapsed_ms);
        }
        heartbeat_if_due();
    }
}
