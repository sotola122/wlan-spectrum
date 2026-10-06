/* Fake FreeRTOS core: critical sections map to a pthread mutex so the host
 * seam tests keep the adapter's locking discipline observable. */
#ifndef FAKE_SDK_FREERTOS_H
#define FAKE_SDK_FREERTOS_H

#include <pthread.h>
#include <stdint.h>

typedef int BaseType_t;
typedef unsigned int UBaseType_t;
typedef uint32_t TickType_t;
typedef void *TaskHandle_t;
typedef void *QueueHandle_t;

typedef pthread_mutex_t portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED PTHREAD_MUTEX_INITIALIZER
#define portENTER_CRITICAL(mux) pthread_mutex_lock(mux)
#define portEXIT_CRITICAL(mux) pthread_mutex_unlock(mux)

#define pdTRUE 1
#define pdFALSE 0
#define pdPASS 1
#define portMAX_DELAY 0xFFFFFFFFu
#define pdMS_TO_TICKS(ms) ((TickType_t)(ms))

#endif /* FAKE_SDK_FREERTOS_H */
