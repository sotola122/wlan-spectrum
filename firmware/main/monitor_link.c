#include "monitor_link.h"

#include <string.h>

#include "driver/uart.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "soc/uart_pins.h"

/* Bounds (see CODING_GUIDELINE 1.3): every wait below has a deadline. */
#define CONFIG_POLL_CHUNK_BYTES 64         /* per read() within one poll */
#define CONFIG_POLL_BUDGET_BYTES 256       /* max consumed per monitor_link_poll
                                            * call: a flooding host cannot
                                            * monopolize a dwell tick */
#define TX_QUEUE_DEPTH 4                  /* whole frames, newest may drop */
#define TX_FRAME_WRITE_LIMIT_MS 30000     /* host-stall deadline per frame */
#define INVALID_CONFIG_PERIOD_US 1000000  /* at most one error per second */
#define TX_TASK_STACK_BYTES 4096

/* UART0 transport (board USB-UART bridge on the SoC-default ROM download
 * pins from components/soc/esp32c5/include/soc/uart_pins.h: U0TXD=11,
 * U0RXD=12). Baud 921600 matches the GUI/smoke default; console output is
 * disabled in sdkconfig (ESP_CONSOLE_NONE + log none) so no text can
 * corrupt the CRC-framed TLV stream — only the ROM boot banner appears on
 * reset, which the host parser resynchronizes over (documented behavior). */
#define MONITOR_UART_NUM UART_NUM_0
#define UART_BAUD_RATE 921600
#define UART_RX_RING_BYTES 512          /* >= CONFIG_POLL_BUDGET_BYTES */
/* TX ring 0 is explicitly accepted by uart_driver_install (v6.0.3 source:
 * `tx_buffer_size == 0` branch). The TX task writes via uart_tx_chars,
 * which does xSemaphoreTake(tx_mux, portMAX_DELAY) (uart.c:1612) before
 * copying only what fits into the hardware FIFO and returning the partial
 * count — it does NOT wait for hardware drain. That mutex is uncontended
 * by design: uart_tx_chars is the only tx_mux taker in this firmware
 * (uart_wait_tx_done / uart_write_bytes / uart_write_bytes_with_break are
 * never called; no console on UART0; no ISR or driver task takes tx_mux),
 * and this TX task is the single writer. The deadline in tx_task therefore
 * sits between bounded steps: an immediate mutex acquire, an FIFO copy,
 * and our own vTaskDelay. If a second UART0 writer is ever added, the
 * bound becomes contingent on that writer's tx_mux hold time. */
#define UART_TX_RING_BYTES 0

static MonitorConfigParser g_parser;
static MonitorConfig g_pending_config;
static bool g_pending_config_valid;
static uint32_t g_seen_discarded;
static int64_t g_last_invalid_us;
static bool g_invalid_seen_once;
static portMUX_TYPE g_tx_drop_lock = portMUX_INITIALIZER_UNLOCKED;
static uint32_t g_tx_dropped;
static QueueHandle_t g_tx_queue;
/* In-flight frame for the TX task. Persistent storage: a 4103-byte local
 * would exceed the 4096-byte task stack before any call (ESP-IDF xTaskCreate
 * stack args are bytes). The TX task is the single owner; nobody else touches
 * it. Host regression: test_tx_task_frame_not_on_task_stack. */
static MonitorTlvFrame g_tx_frame;
/* Wrap buffer for monitor_link_submit_json: same pattern on the application
 * task — a 4103-byte local nested inside app_main's call chain wastes half
 * the 8192-byte main-task stack. Application task only (single owner). */
static MonitorTlvFrame g_submit_frame;

static void count_tx_drop(void) {
    portENTER_CRITICAL(&g_tx_drop_lock);
    g_tx_dropped++;
    portEXIT_CRITICAL(&g_tx_drop_lock);
}

static void tx_task(void *arg) {
    (void)arg;
    MonitorTlvFrame *frame = &g_tx_frame;   /* single owner, not on stack */
    for (;;) {
        if (xQueueReceive(g_tx_queue, frame, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        size_t offset = 0;
        int64_t deadline_us =
            esp_timer_get_time() + (int64_t)TX_FRAME_WRITE_LIMIT_MS * 1000;
        while (offset < frame->length_bytes) {
            if (esp_timer_get_time() > deadline_us) {
                /* Host stalled beyond the bound: abandon the remainder and
                 * count a whole-frame drop. The stream is treated as needing
                 * resynchronization by the receiver after such a gap. */
                count_tx_drop();
                break;
            }
            /* uart_tx_chars: bounded FIFO copy (critical section only),
             * returns bytes accepted (may be 0 when FIFO is full). */
            int written = uart_tx_chars(
                MONITOR_UART_NUM, (const char *)(frame->bytes + offset),
                (uint32_t)(frame->length_bytes - offset));
            if (written <= 0) {
                vTaskDelay(1);              /* yield before retrying */
                continue;
            }
            offset += (size_t)written;
        }
    }
}

esp_err_t monitor_link_init(void) {
    uart_config_t uart_config = {
        .baud_rate = UART_BAUD_RATE,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .rx_flow_ctrl_thresh = 0,
        .source_clk = UART_SCLK_DEFAULT,
    };
    esp_err_t err = uart_param_config(MONITOR_UART_NUM, &uart_config);
    if (err != ESP_OK) {
        return err;
    }
    err = uart_set_pin(MONITOR_UART_NUM, U0TXD_GPIO_NUM, U0RXD_GPIO_NUM,
                       UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    if (err != ESP_OK) {
        return err;
    }
    err = uart_driver_install(MONITOR_UART_NUM, UART_RX_RING_BYTES,
                              UART_TX_RING_BYTES, 0, NULL, 0);
    if (err != ESP_OK) {
        return err;
    }
    monitor_config_parser_init(&g_parser);
    g_pending_config_valid = false;
    g_tx_queue = xQueueCreate(TX_QUEUE_DEPTH, sizeof(MonitorTlvFrame));
    if (g_tx_queue == NULL) {
        return ESP_ERR_NO_MEM;
    }
    BaseType_t created = xTaskCreate(tx_task, "monitor_tx", TX_TASK_STACK_BYTES,
                                     NULL, 5, NULL);
    if (created != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void monitor_link_poll(void) {
    uint8_t chunk[CONFIG_POLL_CHUNK_BYTES];
    size_t consumed = 0;
    while (consumed < CONFIG_POLL_BUDGET_BYTES) {
        uint32_t want = CONFIG_POLL_CHUNK_BYTES;
        if (want > CONFIG_POLL_BUDGET_BYTES - consumed) {
            want = (uint32_t)(CONFIG_POLL_BUDGET_BYTES - consumed);
        }
        int read_bytes = uart_read_bytes(MONITOR_UART_NUM, chunk, want, 0);
        if (read_bytes <= 0) {
            break;
        }
        consumed += (size_t)read_bytes;
        for (int i = 0; i < read_bytes; i++) {
            MonitorConfig parsed;
            if (monitor_config_feed(&g_parser, chunk[i], &parsed)) {
                g_pending_config = parsed;      /* newest valid wins */
                g_pending_config_valid = true;
            }
        }
    }
}

bool monitor_link_take_config(MonitorConfig *out_config) {
    if (out_config == NULL || !g_pending_config_valid) {
        return false;
    }
    *out_config = g_pending_config;
    g_pending_config_valid = false;
    return true;
}

bool monitor_link_take_invalid_config(void) {
    uint32_t discarded = g_parser.discarded_bytes;
    if (discarded == g_seen_discarded) {
        return false;
    }
    g_seen_discarded = discarded;
    int64_t now_us = esp_timer_get_time();
    if (g_invalid_seen_once &&
        now_us - g_last_invalid_us < INVALID_CONFIG_PERIOD_US) {
        return false;
    }
    g_invalid_seen_once = true;
    g_last_invalid_us = now_us;
    return true;
}

bool monitor_link_submit_json(const char *json_body, size_t length_bytes) {
    if (g_tx_queue == NULL) {
        return false;
    }
    MonitorTlvFrame *frame = &g_submit_frame;   /* app task: persistent */
    if (!monitor_tlv_wrap_status(frame, json_body, length_bytes)) {
        count_tx_drop();                        /* invalid body never transmits */
        return false;
    }
    if (xQueueSend(g_tx_queue, frame, 0) != pdTRUE) {
        count_tx_drop();                        /* whole observation dropped */
        return false;
    }
    return true;
}

uint32_t monitor_link_tx_dropped(void) {
    portENTER_CRITICAL(&g_tx_drop_lock);
    uint32_t dropped = g_tx_dropped;
    portEXIT_CRITICAL(&g_tx_drop_lock);
    return dropped;
}
