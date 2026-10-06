/*
 * UART0 link for the wifi-monitor/1 TLV stream (board USB-UART bridge,
 * 921600 8N1, no flow control; console/log output disabled in sdkconfig so
 * the binary stream carries only CRC-framed TLV plus the ROM boot banner
 * after a reset, which the host parser resynchronizes over).
 *
 * One TX task owns all UART writes (uart_tx_chars into the hardware FIFO);
 * the application task never writes UART directly and the radio callback
 * never touches this module at all.
 *
 * Concurrency: monitor_link_poll / take_config / take_invalid_config /
 * submit_json are application-task-only (single consumer). The TX task only
 * drains the frame queue. tx_dropped is written from BOTH tasks (app submit
 * drop and TX-task frame abandon) and read by monitor_link_tx_dropped — all
 * three under g_tx_drop_lock (portMUX); queue exhaustion is bounded by the
 * fixed 4-frame queue.
 *
 * Memory: initialization-only allocation (UART RX ring 512 B; TX ring 0 —
 * the TX task writes the hardware FIFO directly with uart_tx_chars; one
 * frame queue of 4 x sizeof(MonitorTlvFrame) — do not hardcode the byte
 * size: size_t width differs between host and target). The in-flight
 * and wrap frames (g_tx_frame, g_submit_frame) are persistent statics, not
 * stack objects. No allocation after monitor_link_init.
 */
#ifndef MONITOR_LINK_H
#define MONITOR_LINK_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#include "monitor_core.h"

/* Install the UART0 driver and TX task. Bounded resources:
 * RX ring 512 B, TX ring 0 (direct FIFO writes), frame queue depth 4.
 * Returns the first SDK error. */
esp_err_t monitor_link_init(void);

/* Read pending UART RX bytes (bounded per call) and feed the CONFIG parser.
 * Application task only; call at least every dwell-wait tick (20 ms). */
void monitor_link_poll(void);

/* Take the newest complete valid CONFIG (newest wins, mailbox cleared).
 * Returns false when none is pending. */
bool monitor_link_take_config(MonitorConfig *out_config);

/* True at most once per second when the parser discarded bytes since the
 * previous call (rate-limited invalid_config signal). */
bool monitor_link_take_invalid_config(void);

/* Wrap json_body (length_bytes, bounded by MONITOR_JSON_CAPACITY_BYTES) in
 * a STATUS TLV frame and enqueue it. Returns false when the body is invalid
 * or the queue is full; then the whole not-yet-started frame is dropped and
 * counted in tx_dropped (never a partial frame on the wire). */
bool monitor_link_submit_json(const char *json_body, size_t length_bytes);

/* Whole frames dropped because the TX queue was full or a write exceeded
 * its 30 s deadline. Monotonic; incremented under g_tx_drop_lock from the
 * application task (submit path) and the TX task (abandon path). */
uint32_t monitor_link_tx_dropped(void);

#endif /* MONITOR_LINK_H */
