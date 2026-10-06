idf_component_register(
    SRCS "app_main.c" "monitor_core.c" "monitor_radio.c" "monitor_link.c"
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
