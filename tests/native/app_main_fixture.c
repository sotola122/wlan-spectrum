/*
 * Host regression for the REAL app_main() acquisition loop (included below).
 *
 * Proves BOTH MONITOR_BOUNDARY_APPLY sites emit the new-epoch config ack
 * BEFORE any new-epoch measurement event:
 *   site1 = main channel-boundary loop
 *   site2 = channel-error backoff loop
 *
 * The adapter layers are faked at their headers: submissions are recorded
 * in order, vTaskDelay is the fixture's scheduling/injection hook (bounded
 * by an iteration cap that fails the test), radio begin can be forced to
 * fail to reach site2. Test scaffolding only (never linked into firmware).
 */
#include "monitor_core.h"
#include "monitor_link.h"
#include "monitor_radio.h"

#include "esp_err.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define RECORD_CAPACITY 512
#define RECORD_BODY_BYTES 1024
#define DELAY_CAP 40000

typedef struct {
    char body[RECORD_BODY_BYTES];
    size_t length_bytes;
} RecordedFrame;

static jmp_buf g_escape;
static RecordedFrame g_records[RECORD_CAPACITY];
static size_t g_record_count;
static int64_t g_time_us;
static long g_delay_count;
static long g_inject_at_delay = -1;
static int g_site;                  /* 1 = main boundary, 2 = backoff */
static int g_escape_reason;         /* 1 = cap (fail), 2 = observed (ok) */

static void inject_pending_config(void);

/* ------------------------------------------------- recorded submissions */
static void record_body(const char *json_body, size_t length_bytes) {
    if (g_record_count >= RECORD_CAPACITY ||
        length_bytes >= RECORD_BODY_BYTES) {
        longjmp(g_escape, 1);       /* overflow = test failure */
    }
    RecordedFrame *rec = &g_records[g_record_count++];
    memcpy(rec->body, json_body, length_bytes);
    rec->body[length_bytes] = '\0';
    rec->length_bytes = length_bytes;

    /* Success: a config ack for epoch 2 exists AND some other epoch-2
     * measurement event followed it. */
    int epoch2_config = -1;
    int epoch2_other = -1;
    for (size_t i = 0; i < g_record_count; i++) {
        const char *b = g_records[i].body;
        if (strstr(b, "\"epoch\":2,") == NULL) {
            continue;
        }
        if (strstr(b, "\"event\":\"config\"") != NULL) {
            if (epoch2_config < 0) {
                epoch2_config = (int)i;
            }
        } else if (epoch2_other < 0) {
            epoch2_other = (int)i;
        }
    }
    if (epoch2_config >= 0 && epoch2_other >= 0) {
        g_escape_reason = 2;
        longjmp(g_escape, 2);
    }
}

/* --------------------------------------------- fake adapter boundary */
esp_err_t monitor_link_init(void) {
    return ESP_OK;
}

void monitor_link_poll(void) {
}

static MonitorConfig g_mailbox_config;
static bool g_mailbox_valid;

bool monitor_link_take_config(MonitorConfig *out_config) {
    if (!g_mailbox_valid) {
        return false;
    }
    *out_config = g_mailbox_config;
    g_mailbox_valid = false;
    return true;
}

bool monitor_link_take_invalid_config(void) {
    return false;
}

bool monitor_link_submit_json(const char *json_body, size_t length_bytes) {
    record_body(json_body, length_bytes);
    return true;
}

uint32_t monitor_link_tx_dropped(void) {
    return 0;
}

static uint8_t g_radio_band;
static uint8_t g_radio_channel;
static bool g_radio_fail_begin;

esp_err_t monitor_radio_init(void) {
    return ESP_OK;
}

esp_err_t monitor_radio_begin(uint8_t band, uint8_t channel) {
    if (g_radio_fail_begin) {
        return ESP_ERR_INVALID_STATE;
    }
    g_radio_band = band;
    g_radio_channel = channel;
    return ESP_OK;
}

void monitor_radio_finish(MonitorObservation *out) {
    memset(out, 0, sizeof(*out));
    out->band = g_radio_band;
    out->channel = g_radio_channel;
    out->observed_ms = 120;
}

/* ----------------------------------------------------- platform fakes */
esp_err_t nvs_flash_init(void) {
    return ESP_OK;
}

const char *esp_err_to_name(esp_err_t code) {
    return code == ESP_OK ? "ESP_OK" : "FAKE_ESP_ERR";
}

int64_t esp_timer_get_time(void) {
    return g_time_us;
}

void vTaskDelay(TickType_t ticks) {
    g_time_us += (int64_t)ticks * 1000;
    g_delay_count++;
    if (g_inject_at_delay > 0 && g_delay_count == g_inject_at_delay) {
        inject_pending_config();
    }
    if (g_delay_count > DELAY_CAP) {
        g_escape_reason = 1;
        longjmp(g_escape, 1);
    }
}

BaseType_t xTaskCreate(TaskFunction_t entry, const char *name,
                       uint32_t stack_bytes, void *arg, UBaseType_t priority,
                       TaskHandle_t *out_handle) {
    (void)entry;
    (void)name;
    (void)stack_bytes;
    (void)arg;
    (void)priority;
    if (out_handle != NULL) {
        *out_handle = (TaskHandle_t)1;
    }
    return pdPASS;
}

/* The real loop under test (overridable for the negative control): */
#ifndef APP_MAIN_PATH
#define APP_MAIN_PATH "../../firmware/main/app_main.c"
#endif
#include APP_MAIN_PATH

/* ----------------------------------------------------------- scenario */
static void inject_pending_config(void) {
    /* A changed CONFIG (sweep/5 GHz/different timing) awaiting a boundary. */
    g_mailbox_config.mode = 1;
    g_mailbox_config.band = 1;
    g_mailbox_config.sweep_ms = 3000;
    g_mailbox_config.fft_size = 128;
    g_mailbox_config.sample_rate_khz = 40000;
    g_mailbox_valid = true;
    g_inject_at_delay = -1;             /* one-shot */
}

int main(int argc, char **argv) {
    if (argc != 2 || (argv[1][0] != '1' && argv[1][0] != '2')) {
        fprintf(stderr, "usage: %s <1|2>\n", argv[0]);
        return 2;
    }
    g_site = argv[1][0] - '0';
    if (g_site == 2) {
        g_radio_fail_begin = true;      /* force the channel-error backoff */
        g_inject_at_delay = 6;          /* inject inside the backoff loop */
    } else {
        g_inject_at_delay = 8;          /* inject mid-dwell of channel 1 */
    }

    int jumped = setjmp(g_escape);
    if (jumped == 0) {
        app_main();                     /* escaped via longjmp */
    }
    if (g_escape_reason != 2) {
        fprintf(stderr, "site%d: no epoch-2 ack+measurement observed "
                "within %ld delays (%zu frames recorded)\n",
                g_site, g_delay_count, g_record_count);
        for (size_t i = 0; i < g_record_count && i < 12; i++) {
            fprintf(stderr, "  [%zu] %.140s\n", i, g_records[i].body);
        }
        return 1;
    }

    /* THE assertion: the FIRST epoch-2 event is the config ack — i.e. the
     * ack precedes every new-epoch measurement on this apply site. */
    for (size_t i = 0; i < g_record_count; i++) {
        const char *b = g_records[i].body;
        if (strstr(b, "\"epoch\":2,") == NULL) {
            continue;
        }
        if (strstr(b, "\"event\":\"config\"") == NULL) {
            fprintf(stderr,
                    "site%d: FAIL: epoch-2 measurement before ack at [%zu]: "
                    "%.140s\n", g_site, i, b);
            return 1;
        }
        printf("site%d ack-first ok (%zu frames, %ld delays)\n",
               g_site, g_record_count, g_delay_count);
        return 0;
    }
    fprintf(stderr, "site%d: unreachable\n", g_site);
    return 1;
}
