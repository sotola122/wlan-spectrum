# Firmware workflow

## Requirements

Use an ESP32-C5 DevKit and ESP-IDF **v6.0.3**, not a floating release branch.
The build runner checks `idf.py --version` before compiling and the CMake project rejects another IDF version or target.
The project uses Python/uv for the GUI and test runner, GNU Make for shortcuts, and EIM or a supported container engine for the SDK.
Run commands from the repository root.
Firmware code follows the [C/C++ coding guidelines](../firmware/CODING_GUIDELINE.md).

Connect the board's **USB-UART** port for both programming and GUI communication.
The tested board has a CP2102N bridge connected to UART0 (TX GPIO11, RX GPIO12).
The application uses **921600 baud, 8 data bits, no parity, 1 stop bit, no flow control**.
Its TLV data does not use the separate native USB Serial/JTAG port.
Application and bootloader console logging are disabled so logs do not mix with TLV data.
ROM output during reset can still require host parser resynchronization.

## Build with EIM

Install ESP-IDF v6.0.3 through Espressif Installation Manager first.
Verify that EIM can activate it:

```sh
eim run 'idf.py --version' v6.0.3
# Expected: ESP-IDF v6.0.3, after EIM environment messages
uv sync
make build
```

The build runner invokes EIM explicitly; it does not require a previously exported IDF shell.
Artifacts are placed in `build/eim/`, including `wifi_monitor.elf`, `wifi_monitor.bin`, and `flasher_args.json`.
A successful build prints `Project build complete.` and exits zero.

The equivalent command without Make is:

```sh
uv run python scripts/fw.py eim
```

## Build with Podman

Use the official image `docker.io/espressif/idf:v6.0.3`:

```sh
make build-podman
# Equivalent:
uv run python scripts/fw.py podman
```

The runner mounts the repository at `/work`, uses `--userns=keep-id`, checks the SDK version inside the container and writes `build/podman/`.
EIM and Podman use separate build directories; do not copy CMake caches between them.
They share the project dependency manifest/lock and generated `firmware/sdkconfig`, so run these backends sequentially in one checkout.
A container build does not grant access to the board; UART flashing uses host EIM/esptool.

## Build with Windows wslc

This backend targets Microsoft's Windows Linux-container CLI, `wslc.exe`, not Podman running inside a WSL distribution.
Install and start that Windows container environment separately, then use PowerShell from the repository directory:

```powershell
uv sync
uv run python scripts/fw.py wslc --dry-run
uv run python scripts/fw.py wslc
```

With GNU Make installed, the shortcut is:

```powershell
make build-wslc PYTHON=python
```

The runner passes an argument vector to `wslc.exe run`, including the repository bind mount, work directory and the same pinned IDF image.
Spaces in the repository path are retained as part of one mount argument.
Dry-run output uses POSIX shell quoting for display; it is not a PowerShell command to paste back into the shell.
Output is `build/wslc/`.

**Validation status:** command-generation tests cover this backend, but an actual Windows wslc build has not yet been run.
Do not treat a dry run or a Linux container build as Windows verification.

## USB-UART programming

On Linux, identify the board's USB-UART port under `/dev/serial/by-id/`.
The Makefile's default `PORT` is the tested CP2102N bridge; override it for another board.
Do not identify the MCU from the bridge's USB serial alone: esptool must report ESP32-C5 and the intended chip's MAC address before writing.
The host user needs permission to open the serial port. Close the GUI and other serial consumers first.

```sh
eim run 'python -m esptool --port /dev/ttyUSB0 chip-id' v6.0.3
make build
make flash-uart PORT=/dev/ttyUSB0
```

`flash-uart` uses the generated `build/eim/flash_args`, programs the bootloader, partition table and application, verifies their hashes, then resets through the bridge.
Review the generated image list before overwriting another device; do not guess offsets or add erase-all, NVS erase or eFuse commands.
The tested CP2102N route completed programming and reset without a manual BOOT operation.
After programming, open the same port at 921600 baud for the GUI or smoke test.

## Optional native JTAG identification and programming

JTAG requires a separate connection to the board's native USB Serial/JTAG port; it is not available through the USB-UART bridge.
The current Makefile's JTAG targets select native USB serial `<DEVICE_ID>`.
Review that identifier before using the commands with another board.
The host user needs read/write access to the corresponding USB device node, in addition to permission to open its serial port.
EIM activates SDK tools; it does not bypass Linux USB permissions.

```sh
make jtag-probe
```

A successful probe identifies the exact USB serial, an ESP32-C5 RISC-V core and its revision, then exits zero.
Do not proceed if the identifier or chip is different.
`LIBUSB_ERROR_ACCESS` is a host USB permission failure, not an unsupported chip diagnosis.

```sh
make build
make flash-jtag
```

**Bring-up status:** JTAG identification has succeeded, but this JTAG programming sequence encountered reset/halt failures after writing the bootloader.
Recovery programming through native USB Serial/JTAG with esptool `--no-stub` subsequently wrote and hash-verified all three images; separate JTAG hash verification also succeeded.
Later native-USB debug/recovery operations left the device unresponsive. Normal operation and subsequent firmware updates now use USB-UART; the native-USB reset failure's cause remains unconfirmed.
Do not interpret successful image verification as proof of reliable reset/debug recovery or a completed RF test matrix.
A failed JTAG flash can leave only some images written.
Retain the full OpenOCD output and verify every image before treating the board as programmed.
Do not hide failures with an unbounded retry loop.

The flash target uses EIM's `board/esp32c5-builtin.cfg` and `program_esp_bins` with generated `build/eim/flasher_args.json`.
That manifest includes the bootloader, partition table and application; review it before overwriting an existing device.
Do not guess offsets or issue erase-all, NVS erase or eFuse commands.
The normal target requests verification and reset after programming.
USB re-enumeration can change the device node and its access permissions.

## Host verification

Native tests require a host C compiler with AddressSanitizer/UndefinedBehaviorSanitizer support and the managed cJSON component downloaded by the firmware build.
They exercise the real cJSON implementation, not a substitute serializer.

```sh
QT_QPA_PLATFORM=offscreen make test
uv run python scripts/monitor_smoke.py --self-test
```

The test suite covers framing/CRC, CONFIG, core observation/state handling, FFT normalization against an independent NumPy oracle, shared Qt RF/Demo views, serial connection behavior and smoke checkers.
Offscreen Qt tests and POSIX pseudo-terminals do not constitute real RF or Windows validation.
On Windows, set `QT_QPA_PLATFORM` with PowerShell syntax; POSIX-only PTY tests cannot verify COM hardware there.

### Windows PC validation

Run the following from PowerShell in the repository root. The Windows execution is a separate check; the Linux results above do not establish it.
The host-only test selection excludes `test_firmware_core`, which requires a compatible native C compiler, sanitizer support and the downloaded cJSON source. Missing native prerequisites cause errors rather than automatic skips.

```powershell
uv sync
uv run python -m unittest tests.test_tlv_crc tests.test_serial_config tests.test_monitor_data tests.test_monitor_widget tests.test_monitor_smoke tests.test_rf_smoke tests.test_rf_harness tests.test_fw_build
uv run wifi-spectrum --demo
```

For headless Qt tests, set `$env:QT_QPA_PLATFORM="offscreen"`; leave that variable unset when checking the desktop UI.
POSIX PTY integration tests are skipped on Windows. The GUI harness's `--self-test` is also POSIX-only; use its real-port mode below on Windows.

The tested USB-UART bridge is CP2102N. Install its CP210x driver if needed and identify the COM port in Device Manager.
Close the Demo window, start `uv run wifi-spectrum`, select the COM port and connect at 921600 baud.
After checking the GUI, close it before running these commands sequentially, replacing `COM5` with the actual port:

```powershell
uv run python scripts/rf_spectrum_smoke.py --port COM5 --mode live --band 0 --seconds 60
uv run python scripts/rf_spectrum_smoke.py --port COM5 --mode live --band 1 --seconds 60
uv run python scripts/rf_gui_harness.py --port COM5 --baud 921600 --ready-timeout 30 --shots shots-win
```

The console smoke test does not need a display; the GUI harness uses Qt and saves screenshots.
Only one process may own the COM port at a time. The [Windows wslc build](#build-with-windows-wslc) is a separate firmware-toolchain check.

## Hardware verification

### RF and sampled-CCA acceptance

The final sampled-CCA implementation was built with EIM and Podman on ESP-IDF v6.0.3. The EIM application image programmed and hash-verified over CP2102N USB-UART has SHA-256 `09af9396660752dc64bf685362f945f017a0feb10e9b940488516b4a7b271971`.
The following tests used that running image and frozen host checkers at 921600 baud:

| Band | Mode | Duration | RF frames | Valid CCA samples | Cycle markers | Exit |
|---|---|---:|---:|---:|---:|---:|
| 2.4 GHz | Live | 60 s | 469 | 448 | 36 | 0 |
| 2.4 GHz | Sweep | 60 s | 468 | 452 | 36 | 0 |
| 5 GHz | Live | 60 s | 468 | 461 | 23 | 0 |
| 5 GHz | Sweep | 60 s | 468 | 467 | 23 | 0 |
| 2.4 GHz | Sweep | 600 s | 4693 | 4505 | 361 | 0 |
| 5 GHz | Sweep | 600 s | 4684 | 4645 | 234 | 0 |

All six runs passed post-acknowledgement parser, RF coverage and sampled-CCA validation. Valid CCA samples need not equal the RF-frame count: invalid counter windows are omitted, not replaced with zero. The ten-minute runs reported 3 and 176 dropped AP sightings respectively; the AP observation queue is bounded and is not lossless.

The physical-device GUI harness passed all 11 steps and exited zero. It verified the shared spectrum/current/peak/waterfall layout, display controls, FFT/rate changes, finite Sweep count, both bands, pause/resume, reconnect and return to Demo. It also checked actual raw-counter ratios against utilization bars and table cells, including the window metadata in tooltips, in 2.4 GHz Live, Sweep and 5 GHz Live. Both bands accumulated 31 received-data-driven waterfall rows before their screenshots, which were visually inspected. This was a Linux offscreen `MainWindow`/`SerialReader` test against the board, not a synthetic source or Windows desktop test.

The final combined suite passed 226 tests, including 34 firmware/native test entries; scoped Pyright and Ruff checks passed. The RF smoke self-test passed 32 cases, and the synthetic PTY GUI test passed separately. Regression tests include lost initial CONFIG, bounded ACK retries, missing cycle markers, Sweep publication timing, rejected legacy utilization frames, invalid counter data and frozen-clock polling termination.

These results verify the implemented data path and display behavior. CCA remains an experimental sampled PHY interpretation, not a calibrated or whole-dwell airtime measurement; FFT power remains relative dBFS. The UART stall limitation described below still applies. Windows validation belongs to the separate Windows procedure, and JTAG breakpoint verification is not covered by this USB-UART acceptance.

The local evidence is retained under `.hermes/agent-sessions/pi/2026-10-06-wifi-monitor/evidence/`: `cca-accept-gate-*.log`, `cca-accept-600s-band{0,1}.log`, `cca-accept-gui.log` and `cca-accept-gui-shots/`. Host logs and the frozen checker hashes are in the adjacent Python session's evidence directory. These session artifacts are intentionally Git-ignored.

### RF spectrum acceptance

These results describe the initial RF implementation, before the subsequent sampled-CCA and CONFIG-retry additions. They are retained as a separate acceptance baseline, not attributed to a newer image.

The RF implementation has built with EIM and Podman using ESP-IDF v6.0.3, and has been programmed over USB-UART with image-hash verification.
A 90-second physical-device probe received 657 RF frames (448 on 2.4 GHz, 209 on 5 GHz) and 44 cycle events.
It exercised FFT 64/512 and 20/40 MS/s configuration changes, with zero channel errors, RF decode errors, CRC rejects, discarded bytes after synchronization, or reported firmware TX drops.
This establishes snapshot acquisition and transport on the connected board, not calibrated RF accuracy.
The final RF checks used the same programmed image and the corrected, frozen host checkers:

- Four 60-second runs covered Live and Sweep on both bands; each received 472 RF frames and exited zero.
- A 600-second 2.4 GHz Sweep run received 4738 RF frames and 364 cycle markers and exited zero, with no post-acknowledgement parser errors or missing-RF coverage failures.
- That long run reported 425 dropped AP sightings (`ap_dropped`), not dropped RF frames. The bounded AP observation path is not lossless.
- The physical-device `MainWindow`/`SerialReader` harness passed all 11 steps and exited zero: real rendering, display controls, FFT/rate changes, Sweep/count, both bands, pause/resume, reconnect and Demo restore.
- Waterfall warm-up verified 31 received-data-driven rows on each band, including row shifting; the actual screenshots were inspected for dBFS units, measured traces, history and unavailable utilization.
- The host suite passed 189 tests; scoped Pyright and Ruff checks passed. Synthetic PTY runs are retained separately from physical-device evidence.

The UART runs used 921600 baud through CP2102N. These results establish operation on the tested board, not calibrated accuracy or lossless transmission under arbitrary host stalls.
Earlier mode/period tests that stopped reading for one second between cases recorded truncated frames; those failures are retained, not reclassified as successful tests.
Two runs of the same matrix with reception continuing through that interval passed without the discarded-byte failure.
This supports a receive-pause dependence; it does not identify the exact driver, bridge or host-buffer loss point.
Keep reading while acquisition runs; GUI Pause stops applying observations but does not stop its serial reader.
Utilization was unavailable in this baseline. The later [CCA experiments](esp32c5-spectrum-research.md#subsequent-cca-counter-experiments) support an experimental sampled PHY counter ratio; its shorter measurement window must not be presented as whole-dwell utilization.

The RF-specific checkers are `scripts/rf_spectrum_smoke.py` and `scripts/rf_gui_harness.py`.
Their `--self-test` modes use fixtures or a synthetic PTY device, never real RF.
For physical runs, use `--port` with the identified board; close other serial consumers first.
The GUI harness exercises `MainWindow` and `SerialReader`; an offscreen run is not Windows or interactive desktop validation.
On headless Linux, set `QT_QPA_PLATFORM=offscreen` when invoking the GUI harness, including `--help` and `--self-test`.

```sh
uv run python scripts/rf_spectrum_smoke.py --port /dev/ttyUSB0 --band 0 --mode sweep --seconds 600
QT_QPA_PLATFORM=offscreen uv run python scripts/rf_gui_harness.py --port /dev/ttyUSB0 --shots build/rf-gui-shots
```

Run the tools sequentially, not with simultaneous access to the port.
Change `--band` to `1` for 5 GHz and `--mode` to `live` for per-snapshot updates.
The initial RF baseline's ten-minute run was on 2.4 GHz; its 5 GHz runs were shorter protocol tests and GUI acceptance. The later RF and sampled-CCA acceptance above includes separate ten-minute runs on both bands.

### Earlier packet-only monitor evidence

The commands and results below describe the previously verified packet-monitor path.
They are retained for protocol regression coverage and do not substitute for RF spectrum acceptance.
`make smoke` and `scripts/monitor_smoke.py --port` expect the earlier packet-only capability flags and will reject the RF-enabled image.
Do not use those commands to validate the current RF firmware; use `scripts/rf_spectrum_smoke.py --port` instead.

Close other serial consumers before running the smoke tool.
Use the actual serial port of the identified board:

```sh
make smoke PORT=/dev/ttyUSB0
uv run python scripts/monitor_smoke.py --port /dev/ttyUSB0 --cases live-2.4,sweep-5
uv run python scripts/monitor_smoke.py --port /dev/ttyUSB0 --stream --seconds 600
```

On Windows, replace the port with the corresponding `COMx` name and invoke the Python script directly.
The default run checks Live/Sweep on both bands plus CONFIG/CRC behavior.
`--seconds` is the per-case deadline in the default mode; add `--stream` for a continuous-duration capture.
Exit zero means all requested checks passed, one means a protocol/measurement check failed, and two means an I/O or usage error.
The self-test never opens real hardware.

Hardware qualification includes observable AP traffic on both bands, a ten-minute stream without resets, and receiver-pause recovery. Verify firmware queue saturation separately: with UART flow control disabled, stopping PC reads does not backpressure the MCU.
Empty RF observations are not evidence of working reception on an otherwise unverified band.
Inspect the real GUI with the board as well as running the CLI checks.
Verified on the earlier packet-only firmware through CP2102N USB-UART:

- EIM and Podman builds using ESP-IDF v6.0.3; UART programming with image-hash verification.
- All 12 default smoke checks, including both bands/modes, invalid mode/band/length, CRC corruption, split and concatenated CONFIG. The entire post-sync session, including inter-case drains, had zero TLV/JSON parse errors.
- A continuous ten-minute stream: 378 cycle events, zero TLV errors. The initial identical-CONFIG join can be a partial cycle; subsequent completed cycles are checked for channel coverage.
- A separate receiver-pause probe using one open port and parser: 60 seconds without reading, then 40 seconds reading. It observed 64 parser errors during recovery and zero in the final ten seconds, with advancing cycles and uptime. Firmware `tx_dropped` stayed zero; it is not a host-loss counter.
- An offscreen GUI harness using the actual `MainWindow` and `SerialReader` against the physical board: non-default initial configuration, Live/Sweep and band switching, measured packet/AP views, disconnect/reconnect and normal close passed. This exercises the real GUI data path, not interactive desktop or Windows visual QA.

These are ambient observations and protocol tests, not calibrated RF measurements.
JTAG/GDB application-breakpoint verification remains unperformed with the native port disconnected; it is separate from the USB-UART runtime checks. Windows wslc and Windows real-port GUI validation are performed by the user. Controlled-RF accuracy comparison was excluded from this acceptance scope; no calibration claim is made.

## Source map

| Path | Responsibility |
|---|---|
| `firmware/main/monitor_core.c` | Portable CONFIG/CRC, AP parsing, aggregation, cJSON events, epoch/cycle state |
| `firmware/main/monitor_radio.c` | ESP-IDF Wi-Fi capture and channel control |
| `firmware/main/monitor_capture.c`, `bank_guard.ld` | Private SDK I/Q snapshot adapter and capture-bank exclusion |
| `firmware/main/monitor_spectrum.c` | Fixed-workspace complex FFT, dBFS normalization and RF encoding |
| `firmware/main/monitor_link.c` | UART0 transport through the USB-UART bridge and CONFIG mailbox |
| `firmware/main/app_main.c` | Initialization, dwell scheduling and event production |
| `firmware/sdkconfig.defaults` | Checked-in binary-stream and RTOS defaults |
| `firmware/main/idf_component.yml`, `firmware/dependencies.lock` | Component requirement and resolved versions |
| `scripts/fw.py`, `Makefile` | EIM/Podman/wslc build routes and local commands |
| `scripts/monitor_smoke.py` | Real-port protocol checks and continuous capture |
| `scripts/rf_spectrum_smoke.py`, `scripts/rf_gui_harness.py` | RF protocol checker and shared-GUI integration harness |
| `tests/native/`, `tests/test_firmware_core.py` | Native firmware-core fixtures and sanitizer runs |

Keep the manifests, lockfiles and defaults tracked.
Generated `sdkconfig`, managed components and build directories are ignored.
The event semantics and allocation policy are documented in [Wi-Fi monitor](wifi-monitor.md); exact framing is in [TLV protocol](tlv-protocol.md).
