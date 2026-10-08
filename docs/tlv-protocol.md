# TLV protocol

Shared byte-stream contract for the ESP32-C5 Wi-Fi monitor and Python GUI.
The codec is implemented in `wifi_spectrum/tlv.py`; the firmware is in
`firmware/main/`. Every frame requires CRC32, including Demo frames.
Old CRC-less senders are incompatible.

Measurement metadata uses STATUS JSON, documented in
[Wi-Fi monitor events](wifi-monitor.md). The additive SPECTRUM_RF format below
carries snapshot FFT power in dBFS, separately from the legacy SPECTRUM dBm
format used by Demo. Source capabilities distinguish packet-only monitoring
from RF acquisition. Demo's frequency-end sweep heuristic does not apply to
RF acquisition, which uses explicit configuration epochs and cycle events.

## 1. Overview

The PC opens a USB-UART port with pyserial (`serial.Serial(port, baud, timeout=0.05)`).
The GUI baud list is `115200`, `460800`, `921600` (default), and `2000000`.
Data bits, parity, and stop bits are left at the pyserial defaults: 8 data bits,
no parity, 1 stop bit, no software or hardware flow control. The ESP32-C5
firmware uses UART0 through the board's USB-UART bridge at **921600 baud**;
the PC must select that rate. The native USB-JTAG connector does not carry
this firmware's TLV stream.

The link is one bidirectional byte stream. Frames are concatenated with no
separator, padding, or inter-frame gap. Multi-byte integers and IEEE-754
binary32 fields are little-endian.

| Direction | Types | Who sends them in this repo |
|---|---|---|
| Device → PC | `0x03` STATUS | Real firmware: `wifi-monitor/1` JSON events. |
| Device → PC | `0x04` SPECTRUM_RF | Raw-I/Q snapshot FFT when advertised by the device's STATUS capabilities. |
| Demo → PC | `0x01` SPECTRUM, `0x02` CH_UTIL, `0x03` STATUS | `MockDevice.tick` (demo and `--pty`). `SerialReader` forwards these types to the GUI. |
| PC → device | `0x10` CONFIG | `MainWindow._send_config` on connect and whenever mode, band, sweep time, FFT size, or sample rate changes. |

The same `TlvParser` accepts the known message types on either path.
`SerialReader` discards a decoded CONFIG and forwards measurement and status
messages. The mock applies a decoded CONFIG dict in `MockDevice.handle_rx`.

## 2. Frame layout

Every frame is:

```
offset  size  field
0       1     type     u8
1       2     length   u16 LE   payload size (excludes header and checksum)
3       length payload
3+length 4    crc32    u32 LE   CRC over header + payload
```

Packed with `struct` format `<BH`, payload bytes, then `<I` checksum.
The full frame occupies `length + 7` bytes.

```
type u8 | length u16 LE | payload[length] | crc32 u32 LE
```

The checksum is CRC-32/ISO-HDLC, compatible with Python `zlib.crc32`:
reflected polynomial `0xEDB88320`, initial value and final XOR `0xFFFFFFFF`,
reflected input/output. It covers the exact three header bytes followed by
the payload, not the CRC trailer. The standard check is
`CRC32("123456789") = 0xCBF43926`.
Both receivers verify the checksum before decoding or applying payload fields.
There is no CRC-less fallback, sync word or version negotiation.

Known type codes (`KNOWN_TYPES`):

| Code | Name | Payload struct (little-endian) | Payload size |
|---|---|---|---|
| `0x01` | SPECTRUM | `<ffH` header, then `n` × `<h` | `10 + 2n` |
| `0x02` | CH_UTIL | `u8 band, u8 n`, then `n` × `(u8 ch, u8 util_pct)` | `2 + 2n` |
| `0x03` | STATUS | UTF-8 JSON object, or `<BBIIb` | JSON length, or `11` |
| `0x04` | SPECTRUM_RF | `<IIBBBBHHII` header, then `n` × `<h` | `24 + 2n` |
| `0x10` | CONFIG | `<BBHHIHB` | `13` |

Any other type code is unknown. Payload length must be `≤ 8192`
(`MAX_PAYLOAD`). A length of `8193` or more is rejected before the payload is
waited for.

## 3. Message types

### 3.1 `0x01` SPECTRUM (Demo / legacy source → PC)

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

### 3.2 `0x02` CH_UTIL (Demo / utilization-capable source → PC)

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

Acquisition settings shared by the GUI, mock and firmware.
Payload is exactly 13 bytes, `<BBHHIHB`. This is one current version-1
contract: update firmware and host together; the earlier 10-byte CONFIG is
not accepted. STATUS remains `wifi-monitor/1`.

| Offset | Field | Type | Meaning |
|---|---|---|---|
| 0 | `mode` | u8 | `0` = live (`MODE_LIVE`), `1` = sweep (`MODE_SWEEP`) |
| 1 | `band` | u8 | `0` = 2.4 GHz, `1` = 5 GHz |
| 2 | `sweep_ms` | u16 | Requested sweep duration, milliseconds |
| 4 | `fft_size` | u16 | FFT length, bins |
| 6 | `sample_rate_khz` | u32 | Sample rate, kHz |
| 10 | `channel_dwell_ms` | u16 | `0` = automatic; otherwise requested per-channel dwell, 120–2000 ms |
| 12 | `cca_attempts` | u8 | Target distributed CCA attempts per dwell, 1–32 |

GUI values that go into this frame:

| Field | GUI source | Values the GUI sends |
|---|---|---|
| `mode` | Live / Band Sweep control | `0` or `1` |
| `band` | 2.4 GHz / 5 GHz control | `0` or `1` |
| `sweep_ms` | Sweep spin box | `100 … 10000`, step `100`, default `1000` |
| `fft_size` | FFT size combo | `64` (default), `128`, `256`, `512`, `1024` |
| `sample_rate_khz` | Sample rate combo | `20000` (`20 MS/s`, default) or `40000` (`40 MS/s`) |
| `channel_dwell_ms` | Dwell control | `0` (Auto, default) or `120 … 2000` ms |
| `cca_attempts` | CCA attempts control | `1 … 32`, default `16` |

The sweep **count** limit (the Count spin box, `0` = unlimited) stays on the
PC. It is not a CONFIG field.

On the mock, these fields take effect immediately. Changing band resets the
sweep segment index. In sweep mode the mock splits the display span into
segments of width `sample_rate_khz / 1000` MHz (the sample rate expressed in
MHz), each with `fft_size` bins, and paces the segments across `sweep_ms`. In
live mode the mock still stores FFT size and sample rate, and still reports
them in STATUS, but the live trace uses a fixed bin step (section 5) and does
not follow those two fields.

`struct.unpack` requires all 13 bytes. A shorter or longer payload fails the
frame. Decode returns a plain dict with keys `mode`, `band`, `sweep_ms`,
`fft_size`, `sample_rate_khz`, `channel_dwell_ms`, `cca_attempts`.
Dwell and CCA controls describe physical acquisition; they do not turn Demo
percentages into sampled PHY measurements.

Demo shows a nominal bin spacing of `sample_rate_khz / fft_size` kHz.
Packet-only firmware validates and echoes these fields without performing
an RF FFT. An RF-capable device advertises its supported and effective values
in STATUS; an echoed request alone is not evidence of the effective sample
rate. For RF frames, bin spacing is `span_khz / fft_size`. Hann-window noise
bandwidth is distinct from this bin spacing.
See [Wi-Fi monitor events](wifi-monitor.md) for CONFIG application and acknowledgement.

### 3.5 `0x04` SPECTRUM_RF (device → PC)

This message carries FFT power from a raw-I/Q snapshot, not a spectrum
estimated from packet RSSI. Its units are dBFS, not calibrated input dBm.
The common CRC32 trailer is outside the payload.

| Offset | Field | Type | Meaning |
|---|---|---|---|
| 0 | `epoch` | u32 | Applied CONFIG generation |
| 4 | `cycle` | u32 | Channel traversal identifier |
| 8 | `band` | u8 | `0` = 2.4 GHz, `1` = 5 GHz |
| 9 | `channel` | u8 | Receive channel for this snapshot |
| 10 | `mode` | u8 | `0` = Live, `1` = Sweep |
| 11 | `rate_code` | u8 | Matches a capability entry's `code`, not its array position |
| 12 | `fft_size` | u16 | Even number of bins, `n` |
| 14 | `source` | u16 | `0` = C5 snapshot I/Q FFT; other values unsupported |
| 16 | `center_khz` | u32 | Tuned center frequency of this frame, kHz |
| 20 | `span_khz` | u32 | Full sample-rate span, kHz; 20 MS/s is `20000` |
| 24 | `power_dbfs[i]` | i16 × `n` | Bin power in 0.01 dBFS steps |

Payload length is exactly `24 + 2*n`, within the firmware's 4096-byte payload
bound. Bin order is FFT-shifted, from low to high frequency:

```
frequency_khz(i) = center_khz - span_khz/2 + i*span_khz/n
```

The lower edge is included; the upper edge is excluded. DC is at `i=n/2`.
Each frame's center is authoritative because acquisition hops channels; a
single CONFIG-level center cannot describe an entire traversal.

Power uses a full-scale complex-I/Q reference. The complex mean of each
snapshot is subtracted before applying the periodic Hann window:
`y = x - mean(x)`, `X = FFT(y*w)`, `W = sum(w)`, `P = abs(X)^2/W^2`, and
`dBFS = 10*log10(P)`.
A unit-amplitude complex tone on a non-DC FFT-bin center is 0 dBFS.
Mean removal suppresses receiver DC offset, but also suppresses a genuine
constant complex component at the tuned center. With this window, the
correction affects the center bin and its two immediate neighbors; those
bins must not be treated as an unbiased measurement of an exact-center tone.
No power bins are replaced by an interpolated floor. There is no extra
factor of four from a real-signal, one-sided spectrum convention. Integer
encoding rounds `100*dBFS` and clamps to int16. Missing captures are absent
frames, not fabricated floor samples; spectral background is not calibrated
antenna-input noise power.

STATUS `config` advertises `spectrum`, optional `spectrum_caps` with
`source`, `fft_sizes`, `rate_codes` (`code`, `span_khz`), and
`bin_unit: "centi_dbfs"`. `spectrum_effective` supplies the active
`fft_size`, `rate_code`, and `span_khz`. Capability values are device-reported;
the GUI must not infer hardware support from the CONFIG echo alone.

The matching CONFIG acknowledgement precedes RF data from a new epoch.
Within a cycle, the channel STATUS precedes its RF frame; the cycle STATUS
follows all channel frames. Live publishes accepted frames as they arrive;
Sweep publishes at the cycle marker. Frames from stale epochs or already
completed cycles must not restore old measurements. A missing capture or
lost cycle marker must not carry an old spectrum into a later cycle.

Channel utilization remains a separate measurement. An unavailable source
is reported as `utilization.available: false` with a blocker string, not as
zero utilization. Neither packets/s nor spectral power is silently relabelled
as a CCA busy-time percentage.
The pinned C5 implementation carries experimental sampled PHY CCA counters
in the optional STATUS `channel.util` object, using the same channel-event
epoch and cycle. See [sampled PHY CCA](wifi-monitor.md#sampled-phy-cca) for its
raw counter sums, valid/attempted counts and window-time upper bounds.
The existing `wifi-monitor/1` contract pools up to 32 valid windows distributed
across a dwell, without a separate utilization-version field. It does not reuse
Demo's `0x02 CH_UTIL` frames or claim whole-dwell airtime coverage.

An independent codec fixture (eight bins for the wire-format test, not a
claim of hardware FFT support) uses epoch 1, cycle 0, band 0, channel 1,
mode 0, rate code 0, source 0, center 2412000 kHz, span 20000 kHz, and
bins `[0, -100, -200, -300, -400, -500, -600, -700]` in centi-dBFS:

```
04280001000000000000000001000008000000e0cd2400204e000000009cff38ffd4fe70fe0cfea8fd44fd9f7a8723
```

This frame is 47 bytes; its CRC32 is `0x23877a9f`, encoded `9f 7a 87 23`.

## 4. Stream parsing and resync

`TlvParser.feed` appends arbitrary chunks and returns every message it can
finish. Bytes that are not yet a full frame stay in the parser buffer.

A candidate header is 3 bytes. From a buffer of at least 3 bytes the parser:

1. Reads `type` and `length`.
2. If `type` is not one of `0x01`, `0x02`, `0x03`, `0x04`, `0x10`, or `length > 8192`,
   it deletes the first buffer byte, adds 1 to `errors`, and tries again.
   It also rejects impossible type-specific lengths: CONFIG must be 13 bytes,
   SPECTRUM must be at least 10 with an even number of sample bytes, and
   CH_UTIL must be at least 2 with an even number of entry bytes.
   SPECTRUM_RF must be 28–4096 bytes with `(length - 24) % 4 == 0`.
3. If the buffer holds the header but not all payload and checksum bytes, it stops
   and waits for a later `feed` call unless a received fixed prefix already
   contradicts the length: SPECTRUM declares its sample count in payload bytes
   8–9; CH_UTIL declares its entry count in payload byte 1; SPECTRUM_RF
   declares its bin count in payload bytes 12–13, requiring `length == 24 + 2*n`.
   Contradictory prefixes take the one-byte discard path; valid fragments remain buffered.
4. It verifies CRC32 over the header and payload. A mismatch drops one byte,
   increments `errors`, and retries without decoding the payload.
   A valid checksum permits decoding. Success consumes `7 + length` bytes and appends the
   message. `Spectrum`, `SpectrumRf`, `ChannelUtil`, and `Status` are objects; CONFIG is a
   `dict`.
5. Decode failure drops the first byte only, adds 1 to `errors`, and retries.
   The exception types that count as failure are `ValueError`,
   `UnicodeDecodeError`, `json.JSONDecodeError`, and `struct.error`.

`errors` therefore counts discarded bytes, not discarded frames. One bad header
can increment it on every slide. `SerialReader` reports `(bytes_received,
parser.errors)` after each non-empty read; the GUI shows the second value as
“TLV errors”.

An incomplete candidate with a plausible length is retained, so a false header
inside noise can delay later valid frames until its advertised payload and
checksum arrive. The residual buffer is bounded by the maximum frame size,
but recovery has no wall-clock bound when incoming traffic stops.
CRC protects integrity; it does not remove this framing ambiguity.
The firmware CONFIG-only parser rejects any header except type `0x10`, length
`13`, and retains at most 20 bytes.

Concrete decode failures that take the one-byte path:

| Type | Failure |
|---|---|
| SPECTRUM | Payload shorter than 10 bytes, or `len(payload) != 10 + 2n` |
| SPECTRUM_RF | Short header; zero, odd or oversized bin count; inconsistent length; invalid band/mode/channel; unknown source; non-positive span |
| CH_UTIL | Payload shorter than 2 bytes, or `len(payload) != 2 + 2n` |
| STATUS JSON | First byte is `{` but the payload is not a single UTF-8 JSON value |
| STATUS binary | First byte is not `{` and the payload is not exactly 11 bytes |
| CONFIG | Payload is not exactly 13 bytes |
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

### Demo end-of-sweep heuristic

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

- **No sync word.** CRC32 is mandatory, but recovery still uses the one-byte
  drop in section 4. CRC-less peers must be upgraded together with the GUI.
- **False headers can stall the parser.** A known type with a plausible length
  is buffered until the declared payload arrives. Only then does a decode
  failure drop a single byte.
- **Demo end of sweep is a frequency heuristic.** SPECTRUM has no last-segment
  field. Real monitoring instead uses explicit `epoch` and `cycle` events.
- **Demo Live does not use the acquisition controls for its bin grid.**
  The mock follows FFT size/sample rate in Sweep mode, not its Live grid.
  Real RF firmware uses these controls for snapshot acquisition and reports
  effective values. The GUI's RBW label is nominal bin spacing, not calibrated
  receiver resolution or Hann-window noise bandwidth.
- **Pause is local.** The PC drops SPECTRUM and CH_UTIL while paused. The
  device keeps sending.
- **Demo 5 GHz coverage stops at UNII-3.** The channel list is 36–177 at 20 MHz
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
01 0e 00  00 c0 16 45  00 00 00 3f  02 00  cf ea  a8 e4  4e 4a 17 6f
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
| `4e 4a 17 6f` | CRC32 = `0x6F174A4E` |

### CONFIG

Band Sweep, 5 GHz, 1000 ms, FFT 64, 20 MS/s (`sample_rate_khz = 20000`),
automatic dwell and 16 CCA attempts.

```
10 0d 00  01  01  e8 03  40 00  20 4e 00 00  00 00  10  2f 2a aa ff
```

| Bytes | Field |
|---|---|
| `10` | type CONFIG |
| `0d 00` | length = 13 |
| `01` | mode = sweep |
| `01` | band = 5 GHz |
| `e8 03` | `sweep_ms` = 1000 |
| `40 00` | `fft_size` = 64 |
| `20 4e 00 00` | `sample_rate_khz` = 20000 |
| `00 00` | `channel_dwell_ms` = 0 (Auto) |
| `10` | `cca_attempts` = 16 |
| `2f 2a aa ff` | CRC32 = `0xFFAA2A2F` |

A live 2.4 GHz frame changes both identifiers and its checksum:
`10 0d 00 00 00 e8 03 40 00 20 4e 00 00 00 00 10 c5 bf 99 b9`.
