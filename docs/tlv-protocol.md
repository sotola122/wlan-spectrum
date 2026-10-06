# TLV protocol

Shared contract for the ESP32-C5 spectrum analyzer UART link. This document
describes the frames implemented in `wifi_spectrum/tlv.py` and how
`wifi_spectrum/main_window.py`, `wifi_spectrum/serial_link.py`,
`wifi_spectrum/bands.py`, and `wifi_spectrum/mock.py` use them. It is the
PC-side codec as it exists in this repository. There is no firmware tree here;
the mock device is the only sender of spectrum, utilization, and status frames.

## 1. Overview

The PC opens a USB-UART port with pyserial (`serial.Serial(port, baud, timeout=0.05)`).
The GUI baud list is `115200`, `460800`, `921600` (default), and `2000000`.
Data bits, parity, and stop bits are left at the pyserial defaults: 8 data bits,
no parity, 1 stop bit, no software or hardware flow control. USB-CDC adapters
often ignore the baud setting; both ends still need the same byte framing.

The link is one bidirectional byte stream. Frames are concatenated with no
separator, padding, or inter-frame gap. Multi-byte integers and IEEE-754
binary32 fields are little-endian.

| Direction | Types | Who sends them in this repo |
|---|---|---|
| Device → PC | `0x01` SPECTRUM, `0x02` CH_UTIL, `0x03` STATUS | `MockDevice.tick` (demo and `--pty`). `SerialReader` emits these three to the GUI. |
| PC → device | `0x10` CONFIG | `MainWindow._send_config` on connect and whenever mode, band, sweep time, FFT size, or sample rate changes. |

The same `TlvParser` accepts all four types on either path. `SerialReader`
discards a decoded CONFIG (it only forwards `Spectrum`, `ChannelUtil`, and
`Status`). The mock applies a decoded CONFIG dict in `MockDevice.handle_rx`.

## 2. Frame layout

Every frame is:

```
offset  size  field
0       1     type     u8
1       2     length   u16 LE   payload size in bytes (the 3-byte header is not included)
3       length payload
```

Packed with `struct` format `<BH` plus the payload bytes. `length` may be 0;
every current payload decoder then fails and the parser resyncs (section 4).

```
type u8 | length u16 LE | payload[length]
```

Known type codes (`KNOWN_TYPES`):

| Code | Name | Payload struct (little-endian) | Payload size |
|---|---|---|---|
| `0x01` | SPECTRUM | `<ffH` header, then `n` × `<h` | `10 + 2n` |
| `0x02` | CH_UTIL | `u8 band, u8 n`, then `n` × `(u8 ch, u8 util_pct)` | `2 + 2n` |
| `0x03` | STATUS | UTF-8 JSON object, or `<BBIIb` | JSON length, or `11` |
| `0x10` | CONFIG | `<BBHHI` | `10` |

Any other type code is unknown. Payload length must be `≤ 8192`
(`MAX_PAYLOAD`). A length of `8193` or more is rejected before the payload is
waited for.

## 3. Message types

### 3.1 `0x01` SPECTRUM (device → PC)

Power spectrum trace in dBm.

| Offset | Field | Type | Meaning |
|---|---|---|---|
| 0 | `f_start_mhz` | f32 | Center frequency of bin 0, MHz |
| 4 | `f_step_mhz` | f32 | Spacing between bins, MHz |
| 8 | `n` | u16 | Number of bins |
| 10 | `dbm[i]` | i16 × `n` | Bin `i` power, `round(dBm × 100)` |

Bin `i` is at `f_start_mhz + i * f_step_mhz` (`Spectrum.freqs`). The codec does
not require `f_step_mhz > 0` or `n ≥ 2`.

**Encode** (`encode_spectrum`): each dBm sample is multiplied by 100, rounded
with NumPy `round` (halfway cases to even), clipped to the int16 range
`[-32768, 32767]`, and stored little-endian. The representable span is about
`-327.68 dBm` … `327.67 dBm` in `0.01 dBm` steps.

**Decode**: `n` must satisfy `len(payload) == 10 + 2n`. The int16 values are
divided by `100` and returned as float32 dBm. A short header or a length that
does not match `n` raises `ValueError`.

The GUI (`_on_spectrum`) uses a frame only while playback is running, the frame
has at least two bins, and its span overlaps the active display grid. Overlapping
grid points are linearly resampled with `numpy.interp`. Grid points outside
`[first bin, last bin]` keep their previous values. A frame with no overlap is
ignored.

A spectrum frame may be a full-band live trace or one segment of a sweep. The
wire format does not carry a segment index; the GUI infers end-of-sweep from
the frequencies (section 5).

### 3.2 `0x02` CH_UTIL (device → PC)

Per-channel airtime / utilization, percent.

| Offset | Field | Type | Meaning |
|---|---|---|---|
| 0 | `band` | u8 | `0` = 2.4 GHz, `1` = 5 GHz (`BAND_24`, `BAND_5`) |
| 1 | `n` | u8 | Number of channel entries (0 … 255) |
| 2 + 2i | `ch` | u8 | Wi-Fi channel number |
| 3 + 2i | `util_pct` | u8 | Utilization for that channel, percent |

`len(payload)` must equal `2 + 2n`. Encode clamps each percent to `0 … 100`.
Decode does not clamp. If the same channel appears twice, the later entry
replaces the earlier one.

The GUI stores the map only when `band` equals the band currently selected and
playback is running. Channel numbers are the keys; order on the wire is not
significant to the plots. The mock emits this frame about twice a second. That
interval is not part of the frame format.

### 3.3 `0x03` STATUS (device → PC)

Device status. One type code, two payload forms. The first payload byte selects
the form.

**JSON form.** Used when `payload[0] == 0x7B` (`{`). The payload is a UTF-8
JSON object. `encode_status_json` writes compact JSON
(`separators=(",", ":")`, no extra whitespace). Decode accepts any UTF-8 JSON
object whose first byte is `{`, including spaced JSON. `json.loads` must
consume the whole payload. Invalid UTF-8 or invalid JSON fails the frame.
The resulting object is kept as a dict; this codec does not require a fixed
set of keys.

The mock’s JSON object is:

| Key | Mock value |
|---|---|
| `fw` | `"mock-0.1"` |
| `chip` | `"ESP32-C5"` |
| `band` | current band id (`0` or `1`) |
| `mode` | `0` live or `1` sweep |
| `sweep_count` | sweeps the mock has finished |
| `fft` | current FFT size |
| `uptime_ms` | milliseconds since the mock started |

The mock emits that object about once a second.

**Binary form.** Used when the payload does not start with `{`. Length must be
exactly 11 bytes, layout `<BBIIb`:

| Offset | Field | Type | Meaning |
|---|---|---|---|
| 0 | `band` | u8 | `0` = 2.4 GHz, `1` = 5 GHz |
| 1 | `mode` | u8 | `0` = live, `1` = sweep |
| 2 | `sweep_count` | u32 | Completed sweeps |
| 6 | `uptime_ms` | u32 | Uptime, milliseconds |
| 10 | `temp_c` | i8 | Temperature, degrees Celsius (`-128 … 127`) |

Decode maps those fields to a dict with keys `band`, `mode`, `sweep_count`,
`uptime_ms`, and `temp_c`. `encode_status_bin` produces this form. The mock
does not send it; the GUI displays either form as JSON text (truncated to 200
characters) and does this even while playback is paused.

### 3.4 `0x10` CONFIG (PC → device)

Acquisition settings. The module docstring marks this layout as the proposal
the PC already sends. Payload is exactly 10 bytes, `<BBHHI`:

| Offset | Field | Type | Meaning |
|---|---|---|---|
| 0 | `mode` | u8 | `0` = live (`MODE_LIVE`), `1` = sweep (`MODE_SWEEP`) |
| 1 | `band` | u8 | `0` = 2.4 GHz, `1` = 5 GHz |
| 2 | `sweep_ms` | u16 | Requested sweep duration, milliseconds |
| 4 | `fft_size` | u16 | FFT length, bins |
| 6 | `sample_rate_khz` | u32 | Sample rate, kHz |

GUI values that go into this frame:

| Field | GUI source | Values the GUI sends |
|---|---|---|
| `mode` | Live / Band Sweep control | `0` or `1` |
| `band` | 2.4 GHz / 5 GHz control | `0` or `1` |
| `sweep_ms` | Sweep spin box | `100 … 10000`, step `100`, default `1000` |
| `fft_size` | FFT size combo | `64` (default), `128`, `256`, `512`, `1024` |
| `sample_rate_khz` | Sample rate combo | `20000` (`20 MS/s`, default) or `40000` (`40 MS/s`) |

The sweep **count** limit (the Count spin box, `0` = unlimited) stays on the
PC. It is not a CONFIG field.

On the mock, these fields take effect immediately. Changing band resets the
sweep segment index. In sweep mode the mock splits the display span into
segments of width `sample_rate_khz / 1000` MHz (the sample rate expressed in
MHz), each with `fft_size` bins, and paces the segments across `sweep_ms`. In
live mode the mock still stores FFT size and sample rate, and still reports
them in STATUS, but the live trace uses a fixed bin step (section 5) and does
not follow those two fields.

`struct.unpack` requires all 10 bytes. A shorter or longer payload fails the
frame. Decode returns a plain dict with keys `mode`, `band`, `sweep_ms`,
`fft_size`, `sample_rate_khz`.

The PC also shows a resolution bandwidth of `sample_rate_khz / fft_size` kHz.
That number is computed locally; it is not a wire field.

## 4. Stream parsing and resync

`TlvParser.feed` appends arbitrary chunks and returns every message it can
finish. Bytes that are not yet a full frame stay in the parser buffer.

A candidate header is 3 bytes. From a buffer of at least 3 bytes the parser:

1. Reads `type` and `length`.
2. If `type` is not one of `0x01`, `0x02`, `0x03`, `0x10`, or `length > 8192`,
   it deletes the first buffer byte, adds 1 to `errors`, and tries again.
3. If the buffer holds the header but not all `length` payload bytes, it stops
   and waits for a later `feed` call. It does not drop those bytes.
4. It decodes the payload. Success consumes `3 + length` bytes and appends the
   message. `Spectrum`, `ChannelUtil`, and `Status` are objects; CONFIG is a
   `dict`.
5. Decode failure drops the first byte only, adds 1 to `errors`, and retries.
   The exception types that count as failure are `ValueError`,
   `UnicodeDecodeError`, `json.JSONDecodeError`, and `struct.error`.

`errors` therefore counts discarded bytes, not discarded frames. One bad header
can increment it on every slide. `SerialReader` reports `(bytes_received,
parser.errors)` after each non-empty read; the GUI shows the second value as
“TLV errors”.

Because a known type with `length ≤ 8192` is held until that many payload
bytes arrive, a false header inside noise can stall the stream for up to 8192
payload bytes before step 5 runs. Unknown types and oversized lengths resync
immediately, one byte at a time.

Concrete decode failures that take the one-byte path:

| Type | Failure |
|---|---|
| SPECTRUM | Payload shorter than 10 bytes, or `len(payload) != 10 + 2n` |
| CH_UTIL | Payload shorter than 2 bytes, or `len(payload) != 2 + 2n` |
| STATUS JSON | First byte is `{` but the payload is not a single UTF-8 JSON value |
| STATUS binary | First byte is not `{` and the payload is not exactly 11 bytes |
| CONFIG | Payload is not exactly 10 bytes |
| Any other | `type` not in the known set (also rejected in step 2, before decode) |

## 5. Frequency grids and bands

Band ids on the wire match `wifi_spectrum/bands.py`.

Center frequency of a 20 MHz channel, in MHz (`channel_freq`):

- 2.4 GHz: channel 14 is `2484`. Channels 1–13 are `2407 + 5 × channel`
  (channel 1 = 2412, channel 6 = 2437, channel 11 = 2462, channel 13 = 2472).
- 5 GHz: `5000 + 5 × channel` (channel 36 = 5180, channel 149 = 5745,
  channel 177 = 5885).

### Display grids

The GUI builds its X axis with `numpy.linspace(f_start, f_stop, n_points)` where
`n_points = round((f_stop - f_start) / step) + 1`. Incoming SPECTRUM frames are
resampled onto this grid. The grid is not required to equal the device bin step.

| Band id | Name | `f_start` | `f_stop` | Step | Points | Channels |
|---|---|---|---|---|---|---|
| `0` | 2.4 GHz | 2400 MHz | 2500 MHz | 0.5 MHz | 201 | 1–14 |
| `1` | 5 GHz | 5150 MHz | 5895 MHz | 1 MHz | 746 | 20 MHz centers below |

5 GHz channel numbers (`_CH_5G`), which are UNII-1 through UNII-3:

- 36, 40, 44, 48, 52, 56, 60, 64
- 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144
- 149, 153, 157, 161, 165, 169, 173, 177

Nominal channel width used for markers and the mock masks is 20 MHz. That width
is not a TLV field. DFS rules and 6 GHz are not represented.

### What the mock puts in SPECTRUM frames

Live mode (`mode = 0`), full span from the display `f_start`:

| Band | `f_step_mhz` |
|---|---|
| 2.4 GHz | 0.3125 |
| 5 GHz | 1.0 |

Sweep mode (`mode = 1`):

- Segment width = `sample_rate_khz / 1000` MHz.
- Bin step = segment width / `fft_size`.
- Segment count = `ceil((f_stop - f_start) / segment width)`, at least 1.
- Segment `k` starts at `f_start + k × segment width` and contains `fft_size` bins.

With the GUI defaults (20 MS/s, FFT 64) each sweep segment is 20 MHz wide with
a 0.3125 MHz step, on both bands.

### End-of-sweep heuristic

Sweep completeness is decided in the GUI, from the frequencies inside the
SPECTRUM frame. In live mode every accepted frame appends one waterfall row.
In sweep mode a waterfall row is appended, and the sweep counter increments,
when the frame’s last bin meets either test (`info` is the active band,
`freqs` is the display grid):

```
last_bin + 2 * f_step_mhz >= info.f_stop
```

or

```
last_bin >= freqs[-1]
```

`freqs[-1]` is `f_stop` (2500 MHz or 5895 MHz). The `2 * f_step_mhz` term treats
a segment that lands within two bins of the top of the band as the last
segment. When the GUI Count limit is non-zero and `sweeps_done` reaches it, the
GUI pauses. Pausing stops the PC from applying SPECTRUM and CH_UTIL frames.
The sender is not told to stop; STATUS frames are still shown.

## 6. Known gaps

These are the limits already called out for this prototype.

- **No sync word and no CRC.** Framing is only `type` + `length` + payload.
  Resync is the one-byte drop in section 4. That is enough for a clean USB-CDC
  byte pipe. A sync byte and a CRC16 are the recommended next step before this
  layout is treated as a firmware contract on a noisy link.
- **False headers can stall the parser.** A known type with a plausible length
  is buffered until the declared payload arrives. Only then does a decode
  failure drop a single byte.
- **End of sweep is a frequency heuristic.** The frame has no “last segment” or
  sweep-id field. The PC uses the comparisons in section 5.
- **FFT size and sample rate are placeholders.** They travel in CONFIG, the GUI
  uses them for the on-screen RBW label, and the mock follows them in sweep
  mode. They do not change the mock’s live-mode bin grid. No firmware consumes
  them yet.
- **Pause is local.** The PC drops SPECTRUM and CH_UTIL while paused. The
  device keeps sending.
- **5 GHz coverage stops at UNII-3.** The channel list is 36–177 at 20 MHz
  spacing, including the UNII-2 channels. DFS behavior and 6 GHz are out of
  scope.
- **Mock traces are synthetic.** The demo generator builds OFDM-like masks,
  duty-cycled access points, Bluetooth-like hops, and a microwave hump. It is
  not a model of ESP32-C5 CSI or RSSI.

## 7. Examples

Hex bytes are the exact output of `encode_spectrum` and `encode_config`.
Spaces are visual only.

### SPECTRUM

Two bins at 2412.0 MHz and 2412.5 MHz, `-54.25 dBm` and `-70.00 dBm`
(int16 values `-5425` and `-7000`).

```
01 0e 00  00 c0 16 45  00 00 00 3f  02 00  cf ea  a8 e4
```

| Bytes | Field |
|---|---|
| `01` | type SPECTRUM |
| `0e 00` | length = 14 |
| `00 c0 16 45` | `f_start_mhz` = 2412.0 |
| `00 00 00 3f` | `f_step_mhz` = 0.5 |
| `02 00` | `n` = 2 |
| `cf ea` | bin 0 = `-5425` → `-54.25 dBm` |
| `a8 e4` | bin 1 = `-7000` → `-70.00 dBm` |

### CONFIG

Band Sweep, 5 GHz, 1000 ms, FFT 64, 20 MS/s (`sample_rate_khz = 20000`).

```
10 0a 00  01  01  e8 03  40 00  20 4e 00 00
```

| Bytes | Field |
|---|---|
| `10` | type CONFIG |
| `0a 00` | length = 10 |
| `01` | mode = sweep |
| `01` | band = 5 GHz |
| `e8 03` | `sweep_ms` = 1000 |
| `40 00` | `fft_size` = 64 |
| `20 4e 00 00` | `sample_rate_khz` = 20000 |

A live 2.4 GHz frame with the same timing and acquisition fields differs only
in the two id bytes: `10 0a 00 00 00 e8 03 40 00 20 4e 00 00`.
