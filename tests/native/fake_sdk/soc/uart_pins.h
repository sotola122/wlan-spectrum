/* Mirrors components/soc/esp32c5/include/soc/uart_pins.h for host builds.
 * tests/test_firmware_core.py cross-checks these values against the real
 * IDF header when it is present, so the constants cannot silently drift. */
#ifndef FAKE_SDK_SOC_UART_PINS_H
#define FAKE_SDK_SOC_UART_PINS_H

#define U0RXD_GPIO_NUM 12
#define U0TXD_GPIO_NUM 11

#endif /* FAKE_SDK_SOC_UART_PINS_H */
