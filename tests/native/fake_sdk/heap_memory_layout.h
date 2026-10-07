/*
 * Fake of IDF components/heap/include/heap_memory_layout.h for native host
 * tests: only the reserved-region type and macro used by
 * monitor_capture.c. Real SDK header is used for firmware builds.
 */
#ifndef FAKE_HEAP_MEMORY_LAYOUT_H
#define FAKE_HEAP_MEMORY_LAYOUT_H

#include <stddef.h>
#include <stdint.h>

typedef struct {
    intptr_t start;
    intptr_t end;
} soc_reserved_region_t;

#define SOC_RESERVE_MEMORY_REGION(START, END, NAME)                     \
    __attribute__((section(".reserved_memory_address")))                \
    __attribute__((used))                                               \
    static soc_reserved_region_t reserved_region_##NAME = { START, END }

#endif /* FAKE_HEAP_MEMORY_LAYOUT_H */
