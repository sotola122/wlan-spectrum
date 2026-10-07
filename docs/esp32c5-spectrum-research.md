# ESP32-C5 spectrum acquisition research

## Finding

ESP32-C5 can acquire raw RF I/Q through an undocumented receiver diagnostic path. The ESPARGOS ESP-SDR implementation supports both 2.4 GHz and 5 GHz reception and snapshot FFTs. The earlier claim that C5 cannot provide spectrum/waterfall data was incorrect: absence of a public `esp_wifi` spectrum API does not establish a hardware limitation.

This note records the initial source-only investigation, when the repository firmware still collected packet metadata only.
No board reset, flash, or register experiment was performed during that investigation.
The subsequent independently written implementation now captures I/Q and computes FFTs on the pinned v6.0.3 SDK; both-band streaming and the shared GUI have been exercised on hardware.
See [Firmware workflow](firmware.md#rf-spectrum-acceptance) for the later acceptance results and [Wi-Fi monitor](wifi-monitor.md) for current behavior.
The source sections below describe that initial investigation. The final section records the subsequent CCA counter experiments separately.

## Primary implementation

The reviewed ESP-SDR revision is `fb264f96226e6838ba323d97801ccd28c752e4e0`.

- The project's [technical overview](https://espargos.net/espsdr/) describes raw internal-modem ADC capture without an external RF receiver, with spectrum and waterfall display. It explicitly includes the C5's 5 GHz path.
- The [C5 target](https://github.com/ESPARGOS/esp-sdr/blob/fb264f96226e6838ba323d97801ccd28c752e4e0/main/targets/esp32c5/chip.h) calls private `adctrig`, places the I/Q buffer at `0x40830000`, limits it to 16,380 complex sample words, and reserves the entire SRAM bank `0x40820000`–`0x40840000` for modem ownership.
- Its [linker guard](https://github.com/ESPARGOS/esp-sdr/blob/fb264f96226e6838ba323d97801ccd28c752e4e0/main/targets/esp32c5/sram_guard.ld) rejects IRAM, data, or BSS overlapping the capture bank. Reserving heap alone is insufficient.
- Its [tuning adapter](https://github.com/ESPARGOS/esp-sdr/blob/fb264f96226e6838ba323d97801ccd28c752e4e0/main/targets/esp32c5/tuning.h) calls private `phy_set_chanfreq` with frequency in MHz.
- Its [capability table](https://github.com/ESPARGOS/esp-sdr/blob/fb264f96226e6838ba323d97801ccd28c752e4e0/README.md#on-chip-spectrum-streaming) lists C5 snapshot FFTs of 256–2048 bins at 4/8/10/20/40/80 MS/s. Continuous FFT capture is not advertised for C5.
- Its [spectrum protocol and validation](https://github.com/ESPARGOS/esp-sdr/blob/fb264f96226e6838ba323d97801ccd28c752e4e0/docs/spectrum.md) describe on-device FFT, mean/max detectors, CRC-protected output, capture-gap flags, and C5 profile tests. Those upstream C5 tests used native USB; alternate UART wiring was not tested in that validation matrix.

The existence of measured I/Q allows actual frequency-domain traces, peak hold, and waterfall history. It does not require inventing spectral shapes from AP RSSI. Snapshot gaps still mean short transmissions can be missed.

## Exact SDK compatibility

The installed SDK is ESP-IDF `v6.0.3`, commit `76f5dedd9950a3012fee8fb7d5586df21fc67802`, checked with `git describe --tags --always` and `git rev-parse HEAD`.

A read-only `nm -A --defined-only` inspection of its `components/esp_phy/lib/esp32c5/` archives found:

| Archive / object | Defined symbol |
| --- | --- |
| `librftest.a:mac_common.o` | `adctrig` |
| `libphy.a:phy_rfpll.o` | `phy_set_chanfreq` |
| `libphy.a:phy_feature.o` | `phy_get_cca`, `phy_get_cca_cnt`, `phy_set_cca`, `phy_set_cca_cnt` |

The [upstream C5 SDK pin](https://github.com/ESPARGOS/esp-sdr/blob/fb264f96226e6838ba323d97801ccd28c752e4e0/firmware-targets.json) is `25fe69f946311abdaf9ad56591f25fedbc20ac98`. Its [version file](https://github.com/espressif/esp-idf/blob/25fe69f946311abdaf9ad56591f25fedbc20ac98/tools/cmake/version.cmake) declares 6.2.0. It is not this project's v6.0.3 pin.

The key symbols exist in v6.0.3, but that alone does not verify the private ABI, register behavior, memory layout, link dependencies, or RF performance.
At the investigation stage, a port/build and hardware capture test were still necessary; the later tests linked above did not require an SDK upgrade.

The v6.0.3 disassembly of `phy_get_cca_cnt` reads registers `0x600a7c5c` and `0x600a7c60`, masks two 27-bit fields, and writes them through its argument pointer. This confirms a concrete counter-reading candidate, not the counter units or whether they represent busy/idle durations. No hardware register read or write was performed. `phy_get_cca` separately returns a sign-extended byte from `0x600a701c`; it must not automatically be interpreted as a utilization percentage.

The [shared capture implementation](https://github.com/ESPARGOS/esp-sdr/blob/fb264f96226e6838ba323d97801ccd28c752e4e0/main/families/c5_c6_c61/receiver.c#L147-L202) restores SRAM ownership and analog settings after capture. Its explicit 20 ms register-poll deadline belongs to a `SAMPLE_RATE_PROBE` diagnostic branch, not the normal C5 `stock_capture` call. Do not treat that diagnostic deadline as proof that the private production `adctrig` call is bounded. Restoration also does not prove coexistence with this application's promiscuous packet receiver.

## Independent continuous-I/Q evidence

[Twotoz/C5VRX](https://github.com/Twotoz/C5VRX/tree/d02bcf7eb742c9e83bba73861bd2bf7caa52500d) supplies another C5-specific path. Its [RF implementation](https://github.com/Twotoz/C5VRX/blob/d02bcf7eb742c9e83bba73861bd2bf7caa52500d/main/rf.c#L74-L94) configures continuous modem-front-end clocking and routes Q4/I4 through `MODEM_DIAG` to PARLIO GPIO inputs. Its README documents an ESP-IDF 6.0.x build route. This is evidence against a general claim that C5 cannot expose raw RF samples, but is not proof of a drop-in continuous FFT backend for this UART monitor. Its GPIO/peripheral assignments and RF initialization differ from this application.

The separate [SLIIV spectrum analyzer](https://github.com/SLIIV/esp32-spectrum-analyzer/tree/9df7455d0b1bacd66a770af51daa11247a5b447c) is a host-side Python I/Q/FFT viewer, not an independently verified C5 firmware acquisition implementation.

## What can be displayed

| Display | Source-backed path | Qualification |
| --- | --- | --- |
| Spectrum | Captured I/Q followed by FFT | Snapshot rather than guaranteed continuous RF coverage |
| Waterfall | History of measured FFT frames | Preserve gaps and actual capture timing |
| Peak hold | Maximum of measured spectra | Reset on relevant acquisition changes |
| Spectral background/noise | Receiver FFT power | Upstream reports uncalibrated gain/power; dBFS is not calibrated input dBm |
| Channel utilization | Further investigation of local CCA counters or explicitly defined sampled RF occupancy | Packet rate is not utilization; private CCA symbol names alone do not establish usable busy-time counters |

The upstream spectrum protocol encodes normalized FFT power, converted by its viewer to dBFS with Hann-window normalization. Copying this into the existing `dBm` display without calibration would change the meaning of the measurement.

## UART and integration implications

The existing 921600-baud 8N1 link has a theoretical payload ceiling of 92,160 bytes/s before application framing and processing overhead. At 80 MS/s, packed 10-bit I plus 10-bit Q requires 200,000,000 bytes/s. Continuous raw-I/Q export at that rate cannot fit, but short captures or on-MCU FFT followed by compact spectrum frames can.

The [upstream C5 defaults](https://github.com/ESPARGOS/esp-sdr/blob/fb264f96226e6838ba323d97801ccd28c752e4e0/sdkconfig.defaults.esp32c5) enable RF-test/PHY debugging and UART0 on GPIO11/GPIO12 at 2,000,000 baud. This is evidence of a UART acquisition path, not validation at this project's 921600 setting. Keep the current single-cable transport unless an actual test establishes a reason to change it.

The [upstream license](https://github.com/ESPARGOS/esp-sdr/blob/fb264f96226e6838ba323d97801ccd28c752e4e0/LICENSE) is GPL-3.0-or-later. Reusing implementation code requires reviewing license obligations; discovering the hardware mechanism does not itself impose that license on an independently written implementation.

The investigation led to the real I/Q acquisition path feeding the existing Demo-style spectrum/waterfall UI, rather than replacing those plots with an RSSI-only monitor.
Hardware capture has since been exercised; absolute RF calibration remains unverified.

## Subsequent CCA counter experiments

The exact v6.0.3 implementation reads the counter control word at `0x600a7c58`.
On the tested board its idle value was `0x80010000`; its low 27 bits were `65536`.
The experiment passed the currently read low 27 bits to the SDK's `phy_set_cca_cnt(value, 1)`, preserving the configured limit rather than choosing a new threshold or writing arbitrary enable/disable registers.
The getter returns two masked counters, A and B, and a completion flag.

Read-only observations initially found A fixed at the limit, B zero and the completion flag set. That was a completed one-shot, not evidence that the hardware lacked useful counters.
Arming restarted A and cleared the completion flag; A advanced approximately linearly and stopped at the configured limit, with completion observed after roughly 0.8 ms.
In the subsequent paired-sample experiment, 120 armed windows were recorded on each band. Adjacent B increments alternated between zero, approximately the same increment as A, and intermediate values. Some 5 GHz windows also contained independently timestamped receive callbacks.
This temporal behavior supports interpreting B/A as a sampled PHY CCA busy fraction, rather than treating B as a count of received packets. A symbol name alone would not establish that interpretation.

This remains an experimental interpretation of the pinned private PHY interface. The experiments do not establish the energy threshold, calibrated accuracy, equivalence to MAC/NAV airtime, or whole-dwell channel utilization. The approximately 0.8 ms sample covers only a small part of a dwell of at least 120 ms, and is not simultaneous with the later FFT snapshot.
Some completed windows reported B one count above A. The product rejects those windows instead of clamping the result to 100% or assuming undocumented inclusive-endpoint arithmetic.
The protocol carries raw busy/total counters and an elapsed-time upper bound so that a percentage does not conceal its measurement interval. Missing or invalid windows remain unavailable, not zero.

The paired-sample evidence is retained in the local session artifact `cca_ambient_probe-rows-20261007-132407.json` (SHA-256 `d7a10c0037aee64bf25a621568132e5ff6da6a3cad2e90f0eb6ce478da9ab912`). Those diagnostic observations are distinct from the final firmware/GUI acceptance described in the firmware workflow.
