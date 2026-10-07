idf_component_register(
    SRCS "app_main.c" "monitor_core.c" "monitor_radio.c" "monitor_link.c"
         "monitor_spectrum.c" "monitor_capture.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES esp_wifi esp_event esp_netif esp_timer
                  esp_driver_uart nvs_flash espressif__cjson
)
# Explicit language version for project-owned code (CODING_GUIDELINE: C11).
# gnu11, not strict c11: ESP-IDF headers require GNU extensions (e.g. the
# `asm` keyword in riscv/csr.h). Project-owned code stays within ISO C11.
# The target option is appended after IDF's directory-wide -std=gnu23 on the
# compile line and therefore wins for these files.
target_compile_options(${COMPONENT_LIB} PRIVATE -std=gnu11)

# Linker assertions: static IRAM/data/BSS must end below the 128 KiB IQ
# capture bank 0x40820000-0x40840000 (heap exclusion is handled by
# SOC_RESERVE_MEMORY_REGION in monitor_capture.c). Fails the link loudly if
# placement ever grows into the bank.
target_link_options(${COMPONENT_LIB} INTERFACE
                    "SHELL:-T ${CMAKE_CURRENT_LIST_DIR}/bank_guard.ld")
