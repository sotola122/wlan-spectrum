# ESP32-C5 Wi-Fi Spectrum Analyzer — PC GUI (prototype)

Desktop GUI (PySide6 + pyqtgraph) for a dual-band (2.4 / 5 GHz)
Wi-Fi spectrum analyzer built on an ESP32-C5. Data arrives over USB-UART as
TLV binary frames. A built-in **demo (mock) mode** lets the GUI run without hardware.

![2.4 GHz live](docs/screenshot_cursor_24.png)
![5 GHz band sweep](docs/screenshot_cursor_5.png)

**Design:** the UI follows [`DESIGN.md`](DESIGN.md) (Cursor design system): warm-cream
canvas `#f7f7f4`, warm ink `#26251e`, white hairline cards with no shadows, Cursor Orange
`#f54e00` only on the primary CTAs (▶ Start / Connect), quiet hairline segmented controls,
Inter for UI text and JetBrains Mono for every numeric surface (MHz, dBm, COM, baud,
channel table). Plot accents use the DESIGN.md pastels (blue spectrum, orange peak hold,
blue→lavender→peach→gold waterfall, mint/gold/error utilization bars). All tokens live in
`wifi_spectrum/theme.py`. (Earlier dark-theme screenshots: `docs/screenshot_24_live.png`,
`docs/screenshot_5_sweep.png`.)

## Run

The project is managed with [uv](https://docs.astral.sh/uv/). Dependencies live in
`pyproject.toml`, exact versions are pinned in `uv.lock` (commit both).

```bash
cd wifi-spectrum-gui
uv sync                                # creates .venv and installs locked deps + the package
uv run wifi-spectrum --demo            # start in demo mode (omit --demo for normal start)
# or: uv run python -m wifi_spectrum --demo
```

Install uv if needed: `curl -LsSf https://astral.sh/uv/install.sh | sh` (Linux/macOS) or
`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows).
Requires Python ≥ 3.10; uv downloads a suitable interpreter automatically if none is found.

* **Windows:** the same commands work in PowerShell / cmd (`uv sync`, `uv run wifi-spectrum`).
  COM ports appear as `COM3` etc.
* **Linux:** Qt needs some system libs, e.g.
  `sudo apt install libegl1 libxkbcommon-x11-0 libxcb-cursor0`. For serial access add your
  user to the `dialout` group. Headless (no display): `QT_QPA_PLATFORM=offscreen`.

The UI is in English. Inter and JetBrains Mono are used when installed; otherwise Qt falls back to the system sans / monospace.

Click **Demo** to fill all three graphs with generated data, or pick a COM
port, baud rate and press **Connect** for a real device.

### Build a wheel / sdist

```bash
uv build            # → dist/wifi_spectrum_gui-0.1.0-py3-none-any.whl and dist/wifi_spectrum_gui-0.1.0.tar.gz
```

The wheel includes `wifi_spectrum/assets/*.svg` and installs the `wifi-spectrum` command
(e.g. `uv tool install dist/wifi_spectrum_gui-0.1.0-py3-none-any.whl`, or `pip install` it).

### End-to-end serial test without hardware (Linux/macOS)

```bash
uv run python -m wifi_spectrum.mock --pty   # prints e.g. "Fake ESP32-C5 on /dev/pts/3"
uv run wifi-spectrum                        # in another terminal: type /dev/pts/3 in the COM box → Connect
```

## UI

| Area | Contents |
|---|---|
| Top bar | Live / Band Sweep segmented control, 2.4 GHz / 5 GHz, ▶ Start / Pause, Sweep time (ms), Count (0 = ∞) + progress, Demo, COM port / ⟳ / baud / Connect |
| Left (shared X axis, MHz) | 1. Spectrum — blue current + orange peak hold (dBm), channel markers, hover readout · 2. Waterfall (blue→lavender→peach→gold map, newest row on top) · 3. Channel Utilization bar chart (%) with channel numbers |
| Right panel | Channels (CH / MHz / Util / Peak; click a row to zoom), Show Full Band, FFT size & Sample rate (placeholders, RBW shown), Peak Hold / Waterfall / Channel Display toggles, Reset Peak, dB Range sliders (Max/Min – spectrum Y range and waterfall colour levels), Status (link, bytes, fps, TLV errors, last device status) |

Mouse wheel / drag zooms and pans the frequency axis on all three plots together.

## TLV protocol

Every frame: `type u8 | length u16 LE | payload[length]` (all little endian).
Field layouts, resync rules, and byte examples: [`docs/tlv-protocol.md`](docs/tlv-protocol.md).

| Type | Dir | Payload |
|---|---|---|
| `0x01` SPECTRUM | dev→PC | `f_start_mhz f32, f_step_mhz f32, n u16, n × int16 (dBm×100)` |
| `0x02` CH_UTIL | dev→PC | `band u8 (0=2.4, 1=5), n u8, n × (ch u8, util_pct u8)` |
| `0x03` STATUS | dev→PC | UTF-8 JSON object (`{...}`) **or** binary `band u8, mode u8, sweep_count u32, uptime_ms u32, temp_c i8` |
| `0x10` CONFIG | PC→dev | *(proposal)* `mode u8 (0=live,1=sweep), band u8, sweep_ms u16, fft_size u16, sample_rate_khz u32` |

* Spectrum frames may cover the whole band (live) or a segment (sweep); the GUI
  resamples them onto its display grid (2.4 GHz: 2400–2500 MHz @ 0.5 MHz,
  5 GHz: 5150–5895 MHz @ 1 MHz).
* In band-sweep mode a sweep is counted complete when a segment reaching the top
  of the band arrives; then a waterfall row is added.
* CONFIG is sent on connect and whenever mode / band / sweep time / FFT / sample
  rate change.

## Files

```
wifi_spectrum/
  __main__.py     entry point (wifi-spectrum / python -m wifi_spectrum [--demo])
  main_window.py  window, plot cards, panels, segmented controls
  theme.py        DESIGN.md tokens, Qt stylesheet, plot styling, colormaps
  assets/         small SVG glyphs (checkbox tick, chevrons) used by the stylesheet
  tlv.py          TLV encode/decode + incremental stream parser
  serial_link.py  QThread serial reader (pyserial) → Qt signals; port listing
  mock.py         fake device (TLV bytes) for demo mode and --pty serial test
  bands.py        2.4/5 GHz band ranges and channel tables
```

## Limitations / TODO

* The frame format has no sync word or CRC. The parser resyncs by dropping a byte
  when it sees an unknown type, an oversized length (>8 KiB) or a payload that fails
  to decode — fine for a clean USB-CDC link, but a sync byte + CRC16 is recommended
  for the firmware.
* FFT size / sample rate are placeholders: they only go out in the proposed CONFIG
  frame (the mock follows them in sweep mode). The firmware side doesn't exist yet.
* Mock data is synthetic (OFDM-like masks, duty-cycled APs, BT hops, microwave
  hump), not a model of real ESP32-C5 CSI/RSSI measurements.
* 5 GHz grid covers UNII-1…UNII-3 (ch 36–177, 20 MHz centres); DFS/6 GHz are not handled.
* Pause drops incoming frames (the device keeps streaming).
