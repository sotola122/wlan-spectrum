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

The test suite covers framing/CRC, CONFIG, core observation/state handling, Qt monitor views, serial connection behavior and the smoke checker.
Offscreen Qt tests and POSIX pseudo-terminals do not constitute real RF or Windows validation.
On Windows, set `QT_QPA_PLATFORM` with PowerShell syntax; POSIX-only PTY tests cannot verify COM hardware there.

## Hardware verification

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
Verified on the connected ESP32-C5 through CP2102N USB-UART:

- EIM and Podman builds using ESP-IDF v6.0.3; UART programming with image-hash verification.
- All 12 default smoke checks, including both bands/modes, invalid mode/band/length, CRC corruption, split and concatenated CONFIG. The entire post-sync session, including inter-case drains, had zero TLV/JSON parse errors.
- A continuous ten-minute stream: 378 cycle events, zero TLV errors. The initial identical-CONFIG join can be a partial cycle; subsequent completed cycles are checked for channel coverage.
- A separate receiver-pause probe using one open port and parser: 60 seconds without reading, then 40 seconds reading. It observed 64 parser errors during recovery and zero in the final ten seconds, with advancing cycles and uptime. Firmware `tx_dropped` stayed zero; it is not a host-loss counter.
- An offscreen GUI harness using the actual `MainWindow` and `SerialReader` against the physical board: non-default initial configuration, Live/Sweep and band switching, measured packet/AP views, disconnect/reconnect and normal close passed. This exercises the real GUI data path, not interactive desktop or Windows visual QA.

These are ambient observations and protocol tests, not calibrated RF measurements.
JTAG/GDB application-breakpoint verification remains unperformed with the native port disconnected. Windows wslc, Windows real-port GUI operation, and controlled-RF accuracy testing also remain unverified.

## Source map

| Path | Responsibility |
|---|---|
| `firmware/main/monitor_core.c` | Portable CONFIG/CRC, AP parsing, aggregation, cJSON events, epoch/cycle state |
| `firmware/main/monitor_radio.c` | ESP-IDF Wi-Fi capture and channel control |
| `firmware/main/monitor_link.c` | UART0 transport through the USB-UART bridge and CONFIG mailbox |
| `firmware/main/app_main.c` | Initialization, dwell scheduling and event production |
| `firmware/sdkconfig.defaults` | Checked-in binary-stream and RTOS defaults |
| `firmware/main/idf_component.yml`, `firmware/dependencies.lock` | Component requirement and resolved versions |
| `scripts/fw.py`, `Makefile` | EIM/Podman/wslc build routes and local commands |
| `scripts/monitor_smoke.py` | Real-port protocol checks and continuous capture |
| `tests/native/`, `tests/test_firmware_core.py` | Native firmware-core fixtures and sanitizer runs |

Keep the manifests, lockfiles and defaults tracked.
Generated `sdkconfig`, managed components and build directories are ignored.
The event semantics and allocation policy are documented in [Wi-Fi monitor](wifi-monitor.md); exact framing is in [TLV protocol](tlv-protocol.md).
