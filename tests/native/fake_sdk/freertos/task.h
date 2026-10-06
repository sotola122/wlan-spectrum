/* Fake FreeRTOS tasks: xTaskCreate records the entry point; the fixture
 * starts recorded tasks on detached pthreads via fake_tasks_start(). */
#ifndef FAKE_SDK_FREERTOS_TASK_H
#define FAKE_SDK_FREERTOS_TASK_H

#include "freertos/FreeRTOS.h"

typedef void (*TaskFunction_t)(void *);

BaseType_t xTaskCreate(TaskFunction_t entry, const char *name,
                       uint32_t stack_bytes, void *arg, UBaseType_t priority,
                       TaskHandle_t *out_handle);
void vTaskDelay(TickType_t ticks);

#endif /* FAKE_SDK_FREERTOS_TASK_H */
