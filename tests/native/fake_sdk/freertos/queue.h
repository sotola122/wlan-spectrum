/* Fake FreeRTOS queue: bounded counting queue with pthread condvar blocking,
 * same observable semantics as xQueueSend/xQueueReceive for the adapters. */
#ifndef FAKE_SDK_FREERTOS_QUEUE_H
#define FAKE_SDK_FREERTOS_QUEUE_H

#include "freertos/FreeRTOS.h"

QueueHandle_t xQueueCreate(UBaseType_t queue_length, UBaseType_t item_size);
BaseType_t xQueueSend(QueueHandle_t queue, const void *item, TickType_t ticks_to_wait);
BaseType_t xQueueReceive(QueueHandle_t queue, void *item, TickType_t ticks_to_wait);
UBaseType_t uxQueueMessagesWaiting(QueueHandle_t queue);

#endif /* FAKE_SDK_FREERTOS_QUEUE_H */
