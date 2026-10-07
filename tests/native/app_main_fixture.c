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
/* Path relative to this file so the host fixture finds the fake regardless
 * of the checker's working directory (the real build keeps -Ifake_sdk). */
#include "fake_sdk/esp_err.h"
#include "monitor_capture.h"
#include "monitor_core.h"
#include "monitor_link.h"
#include "monitor_radio.h"
#include "monitor_spectrum.h"

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

/* Radio-sequence recorder: B = successful begin (dwell opens), T = CCA
 * slot tick, F = finish (dwell closed). App-side scheduling seam. */
static char g_radio_evt[512];
static void radio_evt(char c) {
    size_t n = strlen(g_radio_evt);
    if (n + 1 < sizeof g_radio_evt) {
        g_radio_evt[n] = c;
        g_radio_evt[n + 1] = '\0';
    }
}

/* Every T must sit inside an open B..F dwell, no nested/reordered pairs,
 * and the i-th CLOSED dwell carries exactly expect[i] ticks — the dwell
 * cadence follows the active CONFIG (initial 1000 ms/13 ch sweep vs the
 * injected 3000 ms/20 ch sweep give 12 vs 15 ticks). */
static int radio_sequence_ok(const int *expect, int n_expect) {
    int open = 0, since_b = 0, closed = 0;
    for (const char *p = g_radio_evt; *p; p++) {
        if (*p == 'B') {
            if (open) return 0;
            open = 1;
            since_b = 0;
        } else if (*p == 'F') {
            if (!open || closed >= n_expect) return 0;
            if (since_b != expect[closed]) return 0;
            open = 0;
            closed++;
        } else if (*p == 'T') {
            if (!open) return 0;
            since_b++;
        }
    }
    return !open && closed == n_expect;   /* escape lands on a boundary */
}

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

/* Spectrum frames are binary: record type + provenance instead of running
 * JSON text scans over them (handoff v2.1 ordering: channel event first,
 * then its 0x04 frame). */
typedef struct {
    uint8_t type;
    uint32_t epoch;
    uint32_t cycle;
    uint8_t channel;
} RecordedTlv;
static RecordedTlv g_tlv_records[RECORD_CAPACITY];
static size_t g_tlv_count;

static uint32_t read_u32_le(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

bool monitor_link_submit_frame(const MonitorTlvFrame *frame) {
    if (frame == NULL || frame->length_bytes < 7) {
        return false;
    }
    if (g_tlv_count >= RECORD_CAPACITY) {
        longjmp(g_escape, 1);       /* overflow = test failure */
    }
    RecordedTlv *rec = &g_tlv_records[g_tlv_count++];
    rec->type = frame->bytes[0];
    rec->epoch = read_u32_le(frame->bytes + 3);
    rec->cycle = read_u32_le(frame->bytes + 7);
    rec->channel = frame->bytes[3 + 9];
    return true;
}

bool monitor_link_submit_spectrum(const MonitorSpectrumEvent *event) {
    static MonitorTlvFrame s_frame;
    if (event == NULL || !monitor_tlv_wrap_spectrum(&s_frame, event)) {
        return false;
    }
    return monitor_link_submit_frame(&s_frame);
}

/* Spectrum engine fakes: capability present, deterministic bins. The real
 * adapter (monitor_capture.c) needs the MCU and is verified on hardware.
 * g_capture_fail drives scenario 3: snapshot failure must surface as
 * channel_error "spectrum_capture" with NO 0x04 frame (handoff v2.1
 * missing semantics). */
static bool g_capture_fail;

esp_err_t monitor_capture_init(void) {
    return ESP_OK;
}

MonitorCaptureStatus monitor_capture_snapshot(uint16_t fft_size,
                                              uint8_t rate_code,
                                              int16_t *out_bins,
                                              uint32_t *elapsed_us) {
    if (g_capture_fail) {
        return MONITOR_CAPTURE_TIMEOUT;
    }
    if (out_bins == NULL || fft_size == 0) {
        return MONITOR_CAPTURE_BAD_ARG;
    }
    for (uint16_t i = 0; i < fft_size; i++) {
        out_bins[i] = (int16_t)(-(int32_t)i);
    }
    (void)rate_code;
    if (elapsed_us != NULL) {
        *elapsed_us = 1000;
    }
    return MONITOR_CAPTURE_OK;
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
    radio_evt('B');
    return ESP_OK;
}

void monitor_radio_finish(MonitorObservation *out) {
    radio_evt('F');
    memset(out, 0, sizeof(*out));
    out->band = g_radio_band;
    out->channel = g_radio_channel;
    out->observed_ms = 120;
}

void monitor_radio_cca_tick(uint32_t dwell_ms) {
    (void)dwell_ms;
    radio_evt('T');
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
    if (g_site == 3 && g_delay_count >= 14) {
        /* 10 ms dwell steps: ch1 dwell = 12 delays, channel_error fires at
         * the ch1 boundary, ch2 adds 2 more delays -> stop there. */
        g_escape_reason = 3;            /* scenario 3: enough dwells seen */
        longjmp(g_escape, 3);
    }
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
    if (argc != 2 || (argv[1][0] < '1' || argv[1][0] > '3')) {
        fprintf(stderr, "usage: %s <1|2|3>\n", argv[0]);
        return 2;
    }
    g_site = argv[1][0] - '0';
    if (g_site == 2) {
        g_radio_fail_begin = true;      /* force the channel-error backoff */
        g_inject_at_delay = 6;          /* inject inside the backoff loop */
    } else if (g_site == 1) {
        g_inject_at_delay = 8;          /* inject mid-dwell of channel 1 */
    } else {
        g_capture_fail = true;          /* site 3: snapshot always fails */
    }

    int jumped = setjmp(g_escape);
    if (jumped == 0) {
        app_main();                     /* escaped via longjmp */
    }
    if (g_site == 3) {
        if (g_escape_reason != 3) {
            fprintf(stderr, "site3: never stopped (%ld delays)\n",
                    g_delay_count);
            return 1;
        }
        if (g_tlv_count != 0) {
            fprintf(stderr, "site3: FAIL: %zu 0x04 frames despite capture "
                    "failure\n", g_tlv_count);
            return 1;
        }
        for (size_t i = 0; i < g_record_count; i++) {
            const char *b = g_records[i].body;
            if (strstr(b, "\"event\":\"channel_error\"") != NULL &&
                strstr(b, "\"code\":\"spectrum_capture\"") != NULL) {
                printf("site3 capture-failure propagation ok "
                       "(%zu records, 0 spectrum frames)\n",
                       g_record_count);
                return 0;
            }
        }
        fprintf(stderr, "site3: FAIL: no channel_error spectrum_capture\n");
        for (size_t i = 0; i < g_record_count && i < 12; i++) {
            fprintf(stderr, "  [%zu] %.140s\n", i, g_records[i].body);
        }
        return 1;
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
        if (g_site == 1) {
            /* Scheduling seam: ticks only inside an open dwell; block 1 =
             * initial sweep (dwell 120 ms -> 12 ticks), block 2 = epoch-2
             * injected sweep 3000 ms/20 ch (dwell 150 ms -> 15 ticks). */
            static const int expect[2] = {12, 15};
            if (g_radio_evt[0] != 'B' || !radio_sequence_ok(expect, 2)) {
                fprintf(stderr, "site1: FAIL: radio sequence (%.80s)\n",
                        g_radio_evt);
                return 1;
            }
            printf("site1 dwell-tick-schedule ok (%.64s)\n", g_radio_evt);
        } else {
            /* Backoff: begin never succeeds -> no dwell, no CCA ticks. */
            if (strchr(g_radio_evt, 'B') != NULL ||
                strchr(g_radio_evt, 'T') != NULL) {
                fprintf(stderr, "site2: FAIL: ticks during backoff (%.80s)\n",
                        g_radio_evt);
                return 1;
            }
            printf("site2 backoff-no-ticks ok\n");
        }
        return 0;
    }
    fprintf(stderr, "site%d: unreachable\n", g_site);
    return 1;
}
