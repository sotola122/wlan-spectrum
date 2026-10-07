# ESP32-C5 Wi-Fi monitor

## Measurement scope

The firmware visits one Wi-Fi channel at a time and keeps three measurements separate.
Promiscuous reception reports successfully received management, data and control frames, their peak RSSI, and AP sightings from beacons/probe responses.
After that observation it disables promiscuous reception and captures raw I/Q for an FFT snapshot at the parked channel.
It does not associate, perform active scanning, or transmit probe requests.
RF snapshots are displayed in dBFS relative to the digital complex-I/Q full-scale reference, not calibrated antenna-input dBm.
Packet RSSI is separate from FFT power and does not supply the spectrum trace.
Received packets per second is not channel utilization, CCA busy time, or total RF airtime.
At the start of a successful receive dwell, a short one-shot PHY counter window supplies an experimental sampled CCA busy fraction. It is not an average over the entire dwell or a measurement simultaneous with the later FFT.

The firmware selects country `JP` and uses a conservative receive allowlist:

- 2.4 GHz: channels 1–13.
- 5 GHz: 36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144.

This list is an upper bound, not a guarantee that the SDK accepts every channel.
A channel-selection failure produces `channel_error`; the GUI marks the channel unavailable.
Channel 14, channels 149–177, and 6 GHz are excluded from this firmware.
No active DFS radar detection is implemented.
The wider channel list used by Demo is not the real-device channel policy.

## CONFIG and acquisition timing

The [TLV contract](tlv-protocol.md) defines the CRC32-protected binary CONFIG payload.
Accepted values are mode `0` (Live) or `1` (Sweep), band `0` (2.4 GHz) or `1` (5 GHz), sweep time 100–10000 ms, FFT size 64/128/256/512/1024, and sample rate 20000/40000 kHz.
FFT size and sample rate control the snapshot FFT; STATUS reports both supported capabilities and the active effective settings.
Rate code 2 means 20 MS/s and code 1 means 40 MS/s; codes are identifiers, not indices into the capability array.
Defaults are Live, 2.4 GHz, 1000 ms, 64, and 20000 kHz.

Both modes hop through the selected band's channels.
Live displays each completed channel observation; Sweep stages observations until a cycle event.
The per-channel dwell is `max(120, ceil(sweep_ms / number_of_channels))` ms.
The requested sweep time therefore need not equal the actual cycle time, which also includes tuning, snapshot capture, FFT and scheduling overhead.
`observed_ms` measures reception time for a channel, while `elapsed_ms` measures the full cycle.

The serial reader sends the initial CONFIG after the port opens. The GUI retries an unanswered request at bounded intervals; a matching acknowledgement, disconnect or switch to Demo cancels retrying.
The firmware keeps the latest valid pending CONFIG and applies it at a channel boundary.
A changed CONFIG increments `epoch` and discards the incomplete cycle without emitting its cycle-complete event.
Its new-epoch `config` event is submitted before new-epoch measurements.
An identical CONFIG is acknowledged without changing the epoch or restarting the current cycle.
The `config` event is also sent at startup and approximately once per second as a heartbeat.
The GUI waits for an echo matching all five requested fields and rejects measurements from a different epoch or band.

Pause and the sweep-count limit are PC-side operations, not CONFIG fields.
The board continues receiving and sending while the GUI is paused.

## STATUS JSON: `wifi-monitor/1`

Each event is a compact JSON object inside type `0x03` with the common field `"schema":"wifi-monitor/1"`.
The firmware uses cJSON; the Python decoder uses the standard-library JSON module.
There is no trailing NUL byte in the TLV payload.
JSON object key order has no protocol meaning.

### `config`

| Field | Meaning |
|---|---|
| `event` | `"config"` |
| `fw`, `idf`, `chip`, `country` | `"wifi-monitor-0.1"`, `"v6.0.3"`, `"ESP32-C5"`, `"JP"` |
| `epoch` | Configuration generation, starting at 1 |
| `mode`, `band`, `sweep_ms`, `fft_size`, `sample_rate_khz` | Active CONFIG echo |
| `dwell_ms` | Effective dwell target, at least 120 ms |
| `channels` | Selected band's receive allowlist |
| `spectrum`, `fft_supported` | `true` after spectrum-engine initialization; a failed capture still produces no RF frame |
| `cca` | Indicates support for the experimental sampled PHY CCA path; individual windows can still be invalid |
| `spectrum_caps` | Source `c5_snapshot_iq_fft`, FFT sizes 64/128/256/512/1024, rate entries `{code:1, span_khz:40000}` and `{code:2, span_khz:20000}`, unit `centi_dbfs` |
| `spectrum_effective` | Applied `fft_size`, `rate_code`, and `span_khz` |
| `utilization` | `available:true`, `source:"c5_v6.0.3_phy_cca_cnt"`, `confidence:"experimental_sampled"` for the sampled-counter implementation; unavailable sources instead carry `available:false` and a blocker |
| `tx_dropped` | Cumulative firmware frame-drop counter; does not count bytes lost in the bridge or PC |

### `channel`

| Field | Meaning |
|---|---|
| `event` | `"channel"` |
| `epoch`, `cycle`, `band`, `ch` | Configuration generation, cycle number, band and actual receive channel |
| `observed_ms` | Measured reception duration in milliseconds |
| `packets` | Successfully received frame count during this dwell |
| `peak_rssi_dbm` | Highest received-frame RSSI, or JSON `null` when no frame was received |
| `aps` | At most eight AP sightings for this dwell |
| `ap_dropped` | AP table overflow and sighting-queue drops for this dwell |
| `util` | Optional valid sampled CCA result, defined below; absence means unavailable, not zero |

Each AP contains `bssid` (12 lowercase hexadecimal characters), `ssid_hex` (0–64 hexadecimal characters), `primary_ch` (advertised channel or `null`), and `rssi_dbm` (latest sighting RSSI).
SSID bytes are hex-encoded so arbitrary non-UTF-8 SSIDs remain lossless on the wire.
The host retains these observations separately from the spectrum; the shared spectrum sidebar is not an AP-detail table.
The receive channel and advertised primary channel are distinct; adjacent-channel reception does not change the hardware channel field.

Packet rate is `packets * 1000 / observed_ms`, not a utilization percentage.
Missing RSSI is unavailable, not a fabricated noise-floor value.
The host's AP state is bounded to 256 sightings and expires entries after 30 seconds without a new sighting.

### Sampled PHY CCA

The optional `channel.util` object uses the channel event's `epoch`, `cycle`, `band` and `ch`; it has no independent attribution or replay mechanism.

| Field | Meaning |
|---|---|
| `source` | `"c5_v6.0.3_phy_cca_cnt"` |
| `confidence` | `"experimental_sampled"` |
| `busy` | Final raw B counter from the completed one-shot; integer, `0 <= busy <= total` |
| `total` | Final raw A counter, equal to the preserved configured limit; integer, `1..0x07ffffff` |
| `window_us_upper` | Elapsed time from before arming to after the first completed read; integer microseconds, `1..5000` |

The host computes `100 * busy / total` for the shared utilization bars and channel table. Demo's generated `0x02 CH_UTIL` values remain a separate input path.
JSON booleans are not valid counter or duration values. Unknown additive keys do not change the meaning of these required fields.

The firmware reads the current counter limit, arms through the pinned SDK function, observes the reset/in-progress state, then requires the completion flag and the expected final total. It omits `util` if reset or completion cannot be observed within its bounded polling loop, the elapsed-time check fails, or `busy > total`.
The observed one-count overflow is rejected, not clipped to 100%. An absent or rejected result clears the current channel value rather than reusing an earlier percentage.

The UI identifies this source as sampled PHY CCA (experimental) and exposes its window metadata. Live applies channel results as they arrive; Sweep publishes staged results at cycle completion. Epoch changes, missing observations and incomplete cycles must not retain old values as current measurements.
Capability availability means that the implementation supports this path, not that every window is valid.

The hardware experiments support a clocked, gated busy-counter interpretation on the tested C5 and v6.0.3 SDK. They do not establish calibrated accuracy, the energy threshold, equivalence to MAC/NAV airtime, or whole-dwell utilization. The typical counter window was approximately 0.8 ms within a dwell of at least 120 ms; `window_us_upper` includes polling and software overhead and is not an exact RF integration duration. See the [counter investigation](esp32c5-spectrum-research.md#subsequent-cca-counter-experiments).

### RF spectrum

The channel STATUS precedes its `0x04 SPECTRUM_RF` frame; the cycle marker follows all channel snapshots.
The [RF frame contract](tlv-protocol.md#35-0x04-spectrum_rf-device--pc) defines its header, bin frequencies and normalization.
The GUI resamples bins within each captured span onto the shared frequency grid; it does not infer measurements outside those spans.
Current and waterfall use the real FFT data; peak hold intentionally retains historical maxima until reset.
Coverage and Sweep staging are tied to epoch and cycle IDs, not Demo's end-frequency heuristic.
Acquisition controls follow device capabilities, while peak hold, waterfall, zoom and range controls remain available.
Utilization uses the separate sampled PHY counters above; it is not inferred from the FFT or received packet count.

### `cycle`

Fields: `event:"cycle"`, `epoch`, `cycle`, `band`, `elapsed_ms`, `uptime_ms`.
The event closes a traversal of the selected channel list; channels that failed tuning remain unavailable.
Cycle identifiers may have gaps when configuration changes discard a partial traversal.
The GUI ignores duplicate cycle events and applies its finite sweep-count limit only in Sweep mode.

### `error` and `channel_error`

An `error` event has `event:"error"` and `code`.
Examples include `invalid_config` and SDK initialization errors.
Malformed or CRC-invalid CONFIG never changes active settings; invalid-input reporting is rate-limited to at most once per second.

A `channel_error` adds `epoch`, `cycle`, `band`, `ch`, and `code`.
A failed observation is not reported as zero packets or a made-up RSSI.
`spectrum_capture` reports a failed I/Q capture or FFT; no substitute RF frame is emitted.
NVS initialization failure is reported without automatically erasing NVS.

## Allocation and transport

The SDK-independent core is `firmware/main/monitor_core.c`.
Radio and UART/FreeRTOS adapters are `monitor_radio.c` and `monitor_link.c`; `app_main.c` owns scheduling and configuration state.
`monitor_capture.c` calls the exact SDK's private `adctrig` entry; `monitor_spectrum.c` implements the independently written periodic-Hann complex FFT and RF frame encoder.
The capture bank at `0x40820000`–`0x40840000` is excluded from the SDK heap, and `bank_guard.ld` rejects overlapping static placement.
The requested capture range and guard words are checked before the FFT; incomplete captures are not published.
DSP workspace is fixed-size and owned by the application task, with no per-snapshot allocation.
This private SDK path is version-specific, not a portable public Wi-Fi API; see the [source investigation](esp32c5-spectrum-research.md).
The inspected SDK capture loop exits on completion or an internal counter deadline, but that counter's wall-clock rate has not been established.
Measured successful capture times do not establish a worst-case execution-time guarantee, and the current task-watchdog configuration must not be treated as a guaranteed reset recovery path.
Project C files select GNU C11 because ESP-IDF headers require GNU extensions; application code uses C11 constructs and no exceptions.

Initialization creates the UART driver resources, RTOS queues/tasks and Wi-Fi driver state before acquisition begins.
Application packet accounting, CONFIG parsing and CRC processing do not allocate per packet.
The Wi-Fi callback performs bounded accounting and queues a management-frame prefix; JSON formatting and UART writes run outside it.
Vendor Wi-Fi allocation behavior remains SDK-managed, not a guarantee that the SDK never allocates after initialization.

cJSON is an explicit runtime-allocation exception:

- One transient tree per event, only in application-task context.
- At most 20 channel numbers or eight AP records per event, with fixed event keys and bounded hexadecimal strings.
- `cJSON_PrintPreallocated` writes into a static 4096-byte output buffer; no heap-allocated printed string.
- All create/add/print failures discard the event before submission; `cJSON_Delete` frees the partial or complete tree before return.
- This is best-effort monitoring, not a hard real-time allocation-latency guarantee.

The dependency range is declared in `firmware/main/idf_component.yml`; `firmware/dependencies.lock` pins the resolved component.
Host native tests link the real managed cJSON source, including allocation-failure tests.

The link queues four complete frames without waiting for queue space.
Overflow drops an observation and increments `tx_dropped`.
The UART transport runs at 921600 baud, 8N1, with flow control disabled.
PC-side pauses do not stop transmission; unread data can be lost in bridge or host buffers without incrementing `tx_dropped`.
The single TX task uses `uart_tx_chars` with no driver TX ring, retaining partial frames and checking a 30-second per-frame deadline.
The SDK takes a TX mutex inside that call; the ownership contract excludes other UART writers, including console output, so that mutex is uncontended. FIFO writes do not wait for the hardware to drain.
If that deadline expires after a prefix was sent, the byte stream contains a truncated frame; the host must recover through CRC validation and resynchronization, not assume all transport drops occur before transmission.
The framing limitation and lack of a wall-clock resynchronization bound are documented in [TLV parsing](tlv-protocol.md#4-stream-parsing-and-resync).

Build and hardware verification commands are in [Firmware workflow](firmware.md).
