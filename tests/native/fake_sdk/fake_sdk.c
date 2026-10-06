/* Host implementations for the fake SDK used by monitor_adapter_fixture.
 *
 * Every fake is deterministic and fixture-controlled: monotonic fake time,
 * bounded RX/TX byte stores, captured Wi-Fi callback, deferred task start.
 */
#include "fake_sdk.h"

#include <pthread.h>
#include <stdlib.h>
#include <string.h>

#include "driver/uart.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

/* ------------------------------------------------------------- esp_err */
const char *esp_err_to_name(esp_err_t code) {
    return code == ESP_OK ? "ESP_OK" : "FAKE_ESP_ERR";
}

/* ------------------------------------------------------------ esp_timer */
static int64_t g_time_us;

int64_t esp_timer_get_time(void) {
    return g_time_us;
}

void fake_time_advance_us(int64_t delta_us) {
    g_time_us += delta_us;
}

/* ------------------------------------------------------------- netif/event */
esp_err_t esp_netif_init(void) {
    return ESP_OK;
}

esp_err_t esp_event_loop_create_default(void) {
    return ESP_OK;
}

/* -------------------------------------------------------------------- uart */
#define FAKE_UART_RX_CAPACITY 8192
#define FAKE_UART_TX_CAPACITY (8 * 4103)

static uint8_t g_uart_rx[FAKE_UART_RX_CAPACITY];
static size_t g_uart_rx_len;
static size_t g_uart_rx_pos;

static uint8_t g_uart_tx[FAKE_UART_TX_CAPACITY];
static size_t g_uart_tx_len;

static size_t g_write_max_per_call; /* 0 = unlimited */
static int g_write_fail_count;      /* next N writes return 0 */
static bool g_installed;
static uart_config_t g_config;
static int g_tx_pin;
static int g_rx_pin;

esp_err_t uart_param_config(uart_port_t uart_num, const uart_config_t *config) {
    if (uart_num != UART_NUM_0 || config == NULL || config->baud_rate <= 0) {
        return ESP_ERR_INVALID_ARG;
    }
    g_config = *config;
    return ESP_OK;
}

esp_err_t uart_set_pin(uart_port_t uart_num, int tx_io_num, int rx_io_num,
                       int rts_io_num, int cts_io_num) {
    (void)rts_io_num;
    (void)cts_io_num;
    if (uart_num != UART_NUM_0) {
        return ESP_ERR_INVALID_ARG;
    }
    g_tx_pin = tx_io_num;
    g_rx_pin = rx_io_num;
    return ESP_OK;
}

esp_err_t uart_driver_install(uart_port_t uart_num, int rx_buffer_size,
                              int tx_buffer_size, int queue_size,
                              QueueHandle_t *uart_queue, int intr_alloc_flags) {
    (void)queue_size;
    (void)uart_queue;
    (void)intr_alloc_flags;
    /* Real v6.0.3 contract: `tx_buffer_size == 0` is explicitly valid
     * (uart.c: `... || (tx_buffer_size == 0)`); RX must be > 0. */
    if (uart_num != UART_NUM_0 || rx_buffer_size <= 0 || tx_buffer_size < 0) {
        return ESP_ERR_INVALID_ARG;
    }
    g_installed = true;
    return ESP_OK;
}

int uart_read_bytes(uart_port_t uart_num, void *buf, uint32_t length,
                    uint32_t ticks_to_wait) {
    (void)ticks_to_wait;
    if (!g_installed || uart_num != UART_NUM_0 || buf == NULL) {
        return -1;
    }
    size_t available = g_uart_rx_len - g_uart_rx_pos;
    size_t take = length < available ? length : available;
    if (take == 0) {
        return 0;
    }
    memcpy(buf, g_uart_rx + g_uart_rx_pos, take);
    g_uart_rx_pos += take;
    if (g_uart_rx_pos == g_uart_rx_len) {
        g_uart_rx_pos = 0;
        g_uart_rx_len = 0;
    }
    return (int)take;
}

/* Mirrors uart_tx_chars (v6.0.3): copies ONLY what fits, returns the
 * partial count immediately (0 when the FIFO is "full" / starved) — the
 * non-blocking contract the TX task's deadline relies on. */
int uart_tx_chars(uart_port_t uart_num, const char *buffer, uint32_t len) {
    if (!g_installed || uart_num != UART_NUM_0 || buffer == NULL) {
        return -1;
    }
    if (len == 0) {
        return 0;
    }
    if (g_write_fail_count > 0) {
        g_write_fail_count--;
        return 0;
    }
    size_t allow = len;
    if (g_write_max_per_call != 0 && allow > g_write_max_per_call) {
        allow = g_write_max_per_call;
    }
    if (g_uart_tx_len + allow > sizeof(g_uart_tx)) {
        return -1;
    }
    memcpy(g_uart_tx + g_uart_tx_len, buffer, allow);
    g_uart_tx_len += allow;
    return (int)allow;
}

void fake_uart_rx_push(const uint8_t *bytes, size_t length_bytes) {
    if (g_uart_rx_len + length_bytes > sizeof(g_uart_rx)) {
        abort(); /* fixture bug: bounded fake must not silently drop */
    }
    memcpy(g_uart_rx + g_uart_rx_len, bytes, length_bytes);
    g_uart_rx_len += length_bytes;
}

size_t fake_uart_rx_pending(void) {
    return g_uart_rx_len - g_uart_rx_pos;
}

size_t fake_uart_tx_length(void) {
    return g_uart_tx_len;
}

const uint8_t *fake_uart_tx_data(void) {
    return g_uart_tx;
}

void fake_uart_tx_set_max_per_call(size_t max_bytes) {
    g_write_max_per_call = max_bytes;
}

void fake_uart_tx_fail_next(int count) {
    g_write_fail_count = count;
}

int fake_uart_tx_pin(void) {
    return g_tx_pin;
}

int fake_uart_rx_pin(void) {
    return g_rx_pin;
}

int fake_uart_baud(void) {
    return g_config.baud_rate;
}

/* ----------------------------------------------------------------- wifi */
static wifi_promiscuous_cb_t g_rx_cb;
static bool g_promiscuous;
static uint8_t g_last_channel;
static esp_err_t g_set_channel_ret = ESP_OK;

esp_err_t esp_wifi_init(const wifi_init_config_t *config) {
    (void)config;
    return ESP_OK;
}

esp_err_t esp_wifi_set_storage(wifi_storage_t storage) {
    (void)storage;
    return ESP_OK;
}

esp_err_t esp_wifi_set_mode(wifi_mode_t mode) {
    (void)mode;
    return ESP_OK;
}

esp_err_t esp_wifi_set_country_code(const char *country, bool ieee80211d_enabled) {
    (void)country;
    (void)ieee80211d_enabled;
    return ESP_OK;
}

esp_err_t esp_wifi_start(void) {
    return ESP_OK;
}

esp_err_t esp_wifi_set_ps(wifi_ps_type_t type) {
    (void)type;
    return ESP_OK;
}

esp_err_t esp_wifi_set_promiscuous_filter(const wifi_promiscuous_filter_t *filter) {
    return filter == NULL ? ESP_ERR_INVALID_ARG : ESP_OK;
}

esp_err_t esp_wifi_set_promiscuous_ctrl_filter(const wifi_promiscuous_filter_t *filter) {
    return filter == NULL ? ESP_ERR_INVALID_ARG : ESP_OK;
}

esp_err_t esp_wifi_set_promiscuous_rx_cb(wifi_promiscuous_cb_t cb) {
    g_rx_cb = cb;
    return ESP_OK;
}

esp_err_t esp_wifi_set_promiscuous(bool enable) {
    g_promiscuous = enable;
    return ESP_OK;
}

esp_err_t esp_wifi_set_band_mode(wifi_band_mode_t band_mode) {
    (void)band_mode;
    return ESP_OK;
}

esp_err_t esp_wifi_set_channel(uint8_t primary, wifi_second_chan_t second) {
    (void)second;
    if (g_set_channel_ret != ESP_OK) {
        return g_set_channel_ret;
    }
    g_last_channel = primary;
    return ESP_OK;
}

wifi_promiscuous_cb_t fake_wifi_rx_cb(void) {
    return g_rx_cb;
}

bool fake_wifi_promiscuous_enabled(void) {
    return g_promiscuous;
}

uint8_t fake_wifi_last_channel(void) {
    return g_last_channel;
}

void fake_wifi_set_channel_result(esp_err_t result) {
    g_set_channel_ret = result;
}

/* ---------------------------------------------------------------- queue */
struct fake_queue {
    pthread_mutex_t mutex;
    pthread_cond_t cond;
    size_t item_size;
    size_t capacity;
    size_t head;
    size_t count;
    uint8_t *items;
};

QueueHandle_t xQueueCreate(UBaseType_t queue_length, UBaseType_t item_size) {
    if (queue_length == 0 || item_size == 0) {
        return NULL;
    }
    struct fake_queue *q = calloc(1, sizeof(*q));
    if (q == NULL) {
        return NULL;
    }
    pthread_mutex_init(&q->mutex, NULL);
    pthread_cond_init(&q->cond, NULL);
    q->item_size = item_size;
    q->capacity = queue_length;
    q->items = calloc(queue_length, item_size);
    if (q->items == NULL) {
        free(q);
        return NULL;
    }
    return q;
}

BaseType_t xQueueSend(QueueHandle_t handle, const void *item, TickType_t ticks_to_wait) {
    (void)ticks_to_wait; /* adapters always use timeout 0 */
    struct fake_queue *q = handle;
    pthread_mutex_lock(&q->mutex);
    if (q->count == q->capacity) {
        pthread_mutex_unlock(&q->mutex);
        return pdFALSE;
    }
    memcpy(q->items + (q->head + q->count) % q->capacity * q->item_size, item,
           q->item_size);
    q->count++;
    pthread_cond_signal(&q->cond);
    pthread_mutex_unlock(&q->mutex);
    return pdTRUE;
}

BaseType_t xQueueReceive(QueueHandle_t handle, void *item, TickType_t ticks_to_wait) {
    struct fake_queue *q = handle;
    pthread_mutex_lock(&q->mutex);
    while (q->count == 0) {
        if (ticks_to_wait == 0) {
            pthread_mutex_unlock(&q->mutex);
            return pdFALSE;
        }
        /* Bounded: fake blocks only while the fixture keeps the process
         * alive; process exit tears the thread down. */
        pthread_cond_wait(&q->cond, &q->mutex);
    }
    memcpy(item, q->items + q->head * q->item_size, q->item_size);
    q->head = (q->head + 1) % q->capacity;
    q->count--;
    pthread_cond_signal(&q->cond);
    pthread_mutex_unlock(&q->mutex);
    return pdTRUE;
}

UBaseType_t uxQueueMessagesWaiting(QueueHandle_t handle) {
    struct fake_queue *q = handle;
    pthread_mutex_lock(&q->mutex);
    UBaseType_t waiting = (UBaseType_t)q->count;
    pthread_mutex_unlock(&q->mutex);
    return waiting;
}

/* ----------------------------------------------------------------- tasks */
#define FAKE_MAX_TASKS 4

static TaskFunction_t g_task_entries[FAKE_MAX_TASKS];
static void *g_task_args[FAKE_MAX_TASKS];
static int g_task_count;

BaseType_t xTaskCreate(TaskFunction_t entry, const char *name,
                       uint32_t stack_bytes, void *arg, UBaseType_t priority,
                       TaskHandle_t *out_handle) {
    (void)name;
    (void)stack_bytes;
    (void)priority;
    if (g_task_count >= FAKE_MAX_TASKS) {
        return pdFALSE;
    }
    g_task_entries[g_task_count] = entry;
    g_task_args[g_task_count] = arg;
    g_task_count++;
    if (out_handle != NULL) {
        *out_handle = (TaskHandle_t)(uintptr_t)g_task_count;
    }
    return pdPASS;
}

void vTaskDelay(TickType_t ticks) {
    fake_time_advance_us((int64_t)ticks * 1000); /* deadline observability */
}

static void *fake_task_trampoline(void *index_arg) {
    size_t index = (size_t)(uintptr_t)index_arg;
    g_task_entries[index](g_task_args[index]);
    return NULL;
}

void fake_tasks_start(void) {
    for (int i = 0; i < g_task_count; i++) {
        pthread_t thread;
        pthread_attr_t attr;
        pthread_attr_init(&attr);
        pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
        pthread_create(&thread, &attr, fake_task_trampoline,
                       (void *)(uintptr_t)i);
        pthread_attr_destroy(&attr);
    }
}
