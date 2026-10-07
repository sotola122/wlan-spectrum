/*
 * Linker assertions for the ESP32-C5 modem capture bank.
 *
 * The v6.0.3 SDK's snapshot capture stores I/Q words at a fixed address
 * 0x40830000, and the modem ownership bit it toggles covers the whole
 * 128 KiB bank 0x40820000-0x40840000. Heap is excluded from this bank by
 * SOC_RESERVE_MEMORY_REGION (main/monitor_capture.c); this script fails the
 * link if static IRAM/data/BSS placement would still reach into it.
 * Appended via target_link_options in main/CMakeLists.txt.
 */
ASSERT(_iram_end <= 0x40820000,
       "IRAM overflows into the 128 KiB IQ capture bank 0x40820000-0x40840000 (see main/bank_guard.ld)");
ASSERT(_bss_end <= 0x40820000,
       "Static data/BSS overflows into the 128 KiB IQ capture bank 0x40820000-0x40840000 (see main/bank_guard.ld)");
ASSERT(_heap_start <= 0x40820000,
       "DRAM placement end overflows into the 128 KiB IQ capture bank 0x40820000-0x40840000 (see main/bank_guard.ld)");
