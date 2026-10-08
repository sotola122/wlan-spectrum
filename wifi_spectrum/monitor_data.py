"""Real Wi-Fi monitor data model: schema decoding and display state.

The firmware sends versioned JSON objects inside the existing ``0x03`` STATUS
TLV frame with ``"schema": "wifi-monitor/1"``. This module converts those
objects into typed observations and tracks which measurements are currently
displayable (epoch/band/acknowledgement aware). No synthetic spectrum or
utilization values are ever produced here.

Concurrency: MonitorState is manipulated by the GUI thread only (the serial
reader emits signals that are queued to the GUI thread).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from .tlv import SpectrumRf

SCHEMA = "wifi-monitor/1"
MAX_OBSERVATION_APS = 8
MAX_TRACKED_APS = 256            # GUI AP table bound
UTIL_HISTORY_MAX = 16            # bounded per-channel visit history (item 4)
AP_MAX_AGE_SECONDS = 30.0        # sightings older than this are dropped

# Sampled PHY CCA contract (STATUS channel `util` + capability metadata).
# Experimental sampled measurement - NOT NAV and not dwell-equivalent.
UTIL_SOURCE = "c5_v6.0.3_phy_cca_cnt"
UTIL_CONFIDENCE = "experimental_sampled"
UTIL_TOTAL_MAX = 0x07ffffff       # raw A is a 27-bit counter field
UTIL_WINDOW_MAX_US = 5000         # arm -> first-done upper bound


def parse_channel_util(util) -> dict | None:
    """Strict validation of the additive STATUS channel ``util`` object:
    the single CURRENT pooled contract (no communication versioning).
    Returns the raw sample with its valid/attempted counts, or None for a
    gap - never clamped, never zero-filled. Known keys must match the
    contract exactly (bools are not ints); unknown additive keys are
    tolerated. Every window contributes >= 1 to both sums, so
    total/window >= samples; ``samples == 1`` is simply one valid
    measurement."""
    if not isinstance(util, dict):
        return None
    busy = util.get("busy")
    total = util.get("total")
    window = util.get("window_us_upper")
    if (type(busy) is not int or type(total) is not int
            or type(window) is not int):
        return None                 # bools are not ints either
    if (util.get("source") != UTIL_SOURCE
            or util.get("confidence") != UTIL_CONFIDENCE):
        return None
    samples = util.get("samples")
    attempted = util.get("attempted")
    if type(samples) is not int or not 1 <= samples <= 32:
        return None
    if type(attempted) is not int or not samples <= attempted <= 32:
        return None
    if busy < 0 or busy > total:    # B > A (incl. +1 endpoint) = invalid
        return None
    if total < samples or total > samples * UTIL_TOTAL_MAX:
        return None
    if window < samples or window > UTIL_WINDOW_MAX_US * samples:
        return None
    return {"source": util["source"], "confidence": util["confidence"],
            "busy": busy, "total": total, "samples": samples,
            "attempted": attempted, "window_us_upper": window}


@dataclass(frozen=True)
class ChannelObservation:
    channel: int
    observed_ms: int
    packets: int
    peak_rssi_dbm: int | None
    aps: tuple[dict, ...]

    @property
    def packets_per_second(self) -> float:
        return self.packets * 1000.0 / self.observed_ms


def decode_channel(data: dict) -> ChannelObservation:
    """Validate one ``event=channel`` payload. Raises ValueError when the
    payload is malformed; outputs are never invented on failure."""
    ch, ms, count = (data[k] for k in ("ch", "observed_ms", "packets"))
    if any(type(v) is not int for v in (ch, ms, count)):
        raise ValueError("integer observation fields required")
    if not 1 <= ch <= 177 or ms <= 0 or count < 0:
        raise ValueError("invalid observation bounds")
    peak = data["peak_rssi_dbm"]
    if peak is not None and (type(peak) is not int or not -128 <= peak <= 127):
        raise ValueError("invalid RSSI")
    if (count == 0) != (peak is None):
        raise ValueError("RSSI presence does not match packet count")
    aps = data.get("aps", [])
    if not isinstance(aps, list) or len(aps) > MAX_OBSERVATION_APS:
        raise ValueError("invalid AP list")
    decoded = []
    for ap in aps:
        try:
            bssid = bytes.fromhex(ap["bssid"])
            ssid = bytes.fromhex(ap["ssid_hex"])
        except (KeyError, ValueError) as exc:
            raise ValueError("invalid AP identity") from exc
        if len(bssid) != 6 or len(ssid) > 32:
            raise ValueError("invalid AP identity")
        decoded.append({**ap, "ssid": ssid.decode("utf-8", errors="replace")})
    result = ChannelObservation(ch, ms, count, peak, tuple(decoded))
    if not math.isfinite(result.packets_per_second):
        raise ValueError("invalid packet rate")
    return result


class MonitorState:
    """Display state for the real-device monitor view.

    ``request`` records the five CONFIG fields the GUI just transmitted;
    ``accept`` processes one ``wifi-monitor/1`` payload and returns True only
    when visible data changed. Measurements from a stale epoch or the wrong
    band are discarded. Config and error events are processed even while
    acquisition is paused (the caller gates channel/cycle events instead).
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._requested: tuple | None = None
        self.reset()

    # ------------------------------------------------------------ outgoing
    def request(self, mode: int, band: int, sweep_ms: int, fft_size: int,
                sample_rate_khz: int, channel_dwell_ms: int,
                cca_attempts: int) -> None:
        self._requested = (mode, band, sweep_ms, fft_size, sample_rate_khz,
                           channel_dwell_ms, cca_attempts)

    @property
    def requested(self) -> tuple | None:
        return self._requested

    def reset(self) -> None:
        """Forget everything (connect/disconnect/band switch)."""
        self.epoch: int | None = None
        self.mode: int | None = None
        self.band: int | None = None
        self.channels: tuple[int, ...] = ()
        self.dwell_ms: int | None = None
        self.ready = False
        self.displayed: dict[int, ChannelObservation] = {}
        self.staging: dict[int, ChannelObservation] = {}
        self.aps: dict[tuple[str, int], dict] = {}
        self.cycles = 0
        # Ordered serial semantics: the highest accepted cycle id per epoch
        # detects duplicates and stale replays in O(1) - no set of every
        # cycle id accumulates on long streams.
        self._last_cycle: int | None = None
        self._cycle_seen: set[int] = set()
        self._coverage_cycle: int | None = None
        self._staging_cycle: int | None = None
        self.tx_dropped = 0
        self.ap_dropped = 0
        self.last_error: str | None = None
        self.unavailable: set[int] = set()
        self.elapsed_ms: int | None = None
        self.uptime_ms: int | None = None
        # RF spectrum capabilities from the config ack (handoff v2 section 5)
        self.spectrum = False
        self.spectrum_caps: dict | None = None
        self.spectrum_effective: dict | None = None
        self.utilization_available = False
        self.util_samples: dict[int, dict] = {}   # ch -> published util
        self.util_stage: dict[int, dict] = {}     # cycle stage, bounded
        self.util_history: dict[int, list] = {}   # ch -> bounded visits
        self._util_cycle: dict[int, int] = {}     # ch -> measured cycle
        self._util_stage_cycle: int | None = None
        self.rf_stage: dict[int, SpectrumRf] = {}
        self.rf_flushed: list[SpectrumRf] = []
        self._rf_stage_cycle: int | None = None

    @property
    def rf_ready(self) -> bool:
        """True while a valid capability ack is current: RF frames are
        accepted and the spectrum UI may bind to the advertised caps."""
        return (self.spectrum and self.spectrum_caps is not None
                and self.spectrum_effective is not None)

    # ------------------------------------------------------------ incoming
    def accept(self, data: dict) -> bool:
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise ValueError("payload is not wifi-monitor/1")
        event = data.get("event")
        if event == "config":
            return self._accept_config(data)
        if event == "channel":
            return self._accept_channel(data)
        if event == "cycle":
            return self._accept_cycle(data)
        if event == "error":
            code = str(data.get("code", "unknown"))
            changed = code != self.last_error
            self.last_error = code
            return changed
        if event == "channel_error":
            return self._accept_channel_error(data)
        raise ValueError(f"unknown monitor event: {event!r}")

    def _measurement_epoch_ok(self, data: dict) -> bool:
        epoch = data.get("epoch")
        band = data.get("band")
        if type(epoch) is not int or type(band) is not int:
            return False          # bools are not ints (True == 1 trap)
        if not self.ready or self.epoch is None:
            return False
        if epoch != self.epoch:
            return False
        return band == self.band

    def _util_scope(self, cycle: int) -> bool:
        """Bind util staging to ONE cycle id. A newer cycle starts a fresh
        stage scope (a lost marker can never mix cycles), an older event
        touches nothing. LIVE retention (item 1): the PUBLISHED set is
        never wholesale-cleared here - each channel's last measurement
        survives until its replacement, a known invalid/error, closed-
        cycle missing coverage, or an epoch/band/source reset. The stage
        is a dict keyed by channel, so it stays bounded by the advertised
        channel list."""
        if self._util_stage_cycle is None:
            self._util_stage_cycle = cycle
            return True
        if cycle > self._util_stage_cycle:
            self.util_stage.clear()               # stale older stage
            if self.mode == 0:
                # lost marker: the superseded cycle is closed here, so
                # values measured more than one cycle ago are truly
                # missing and expire; values from the immediately
                # previous cycle survive while the new scan visits
                # channels (item 1 retention), never wholesale-cleared.
                for c in [c for c in self.util_samples
                          if self._util_cycle.get(c, cycle)
                          < cycle - 1]:
                    self._retire_util(c)
            self._util_stage_cycle = cycle
            return True
        return cycle == self._util_stage_cycle

    def _publish_util(self, ch: int, sample: dict, cycle: int) -> None:
        """One replacement: the latest raw sample stays distinct from the
        bounded visit history. The history records each cycle's
        measurement exactly once (duplicate/heartbeat events at the SAME
        cycle never double count) with its arrival time for the span
        readout."""
        self.util_samples[ch] = sample
        hist = self.util_history.setdefault(ch, [])
        if hist and hist[-1][0] == cycle:
            # same-cycle duplicate/heartbeat: refresh THIS visit's
            # contribution instead of double counting (count unchanged,
            # latest raw and aggregate stay coherent)
            hist[-1] = (cycle, sample["busy"], sample["total"],
                        self._clock())
        else:
            hist.append((cycle, sample["busy"], sample["total"],
                         self._clock()))
            del hist[:-UTIL_HISTORY_MAX]          # bounded to GUI max
        self._util_cycle[ch] = cycle

    def _retire_util(self, ch: int) -> None:
        """Known invalid / missing coverage: expire the published value
        AND its history (missing is a gap, never a stale average)."""
        self.util_samples.pop(ch, None)
        self.util_history.pop(ch, None)
        self._util_cycle.pop(ch, None)

    def _clear_measurements(self) -> None:
        self.displayed.clear()
        self.staging.clear()
        self._staging_cycle = None
        self.rf_stage.clear()
        self.rf_flushed = []
        self._rf_stage_cycle = None
        self.aps.clear()
        self.cycles = 0
        self._last_cycle = None
        self._cycle_seen.clear()
        self._coverage_cycle = None
        self.ap_dropped = 0
        self.unavailable.clear()
        self.util_samples.clear()   # no utilization across an epoch/band
        self.util_stage.clear()
        self.util_history.clear()
        self._util_cycle.clear()
        self._util_stage_cycle = None
        self.elapsed_ms = None

    def _accept_config(self, data: dict) -> bool:
        fields = tuple(data.get(k) for k in
                       ("mode", "band", "sweep_ms", "fft_size",
                        "sample_rate_khz", "channel_dwell_ms",
                        "cca_attempts"))
        if self._requested is None or fields != self._requested:
            return False                 # stale or unrequested device config
        new_epoch = data.get("epoch")
        epoch_changed = self.epoch is not None and new_epoch != self.epoch
        channel_list = data.get("channels")
        if not isinstance(channel_list, list) or \
                not all(type(c) is int for c in channel_list):
            raise ValueError("invalid channels in config")
        # A matching echo is always visible: it is the acknowledgement line
        # (epoch/dwell/channels) the widget renders.
        if epoch_changed:
            self._clear_measurements()
        self.ready = True
        self.epoch = new_epoch
        self.mode, self.band, sweep, fft, rate = fields[:5]
        self.channels = tuple(channel_list)
        self.dwell_ms = data.get("dwell_ms")
        tx_dropped = data.get("tx_dropped", 0)
        if type(tx_dropped) is int and tx_dropped >= 0:
            self.tx_dropped = tx_dropped
        spec = data.get("spectrum")
        self.spectrum = type(spec) is bool and spec
        caps, effective = (self._parse_spectrum(data) if self.spectrum
                           else (None, None))
        self.spectrum_caps = caps
        self.spectrum_effective = effective
        avail = (util.get("available")
                 if isinstance((util := data.get("utilization")), dict)
                 else None)
        # capability gate: known source + exact confidence metadata (unknown
        # additive keys tolerated, wrong/missing known metadata not)
        self.utilization_available = (
            type(avail) is bool and avail and isinstance(util, dict)
            and util.get("source") == UTIL_SOURCE
            and util.get("confidence") == UTIL_CONFIDENCE)
        if not self.utilization_available:
            # capability lost or provenance broken (even at the SAME epoch):
            # gap everything - no metadata without a proven capability.
            # The cycle HIGH-WATER mark (_util_stage_cycle) intentionally
            # SURVIVES: late older-cycle events still cannot re-seed.
            self.util_samples.clear()
            self.util_stage.clear()
            self.util_history.clear()
            self._util_cycle.clear()
        return True

    def _parse_spectrum(self, data: dict) -> tuple[dict | None, dict | None]:
        """Validate the additive RF capability keys (handoff v2 section 5).
        Fail-closed: any missing, malformed, or mutually inconsistent field
        disables the RF path entirely instead of guessing, while the config
        ack itself stays valid. Called only for a strictly-true
        ``spectrum`` flag."""
        caps = data.get("spectrum_caps")
        eff = data.get("spectrum_effective")
        if not isinstance(caps, dict) or not isinstance(eff, dict):
            return None, None
        if caps.get("bin_unit") != "centi_dbfs":
            # wrong unit declaration: no conversion exists, reject closed
            return None, None
        sizes = caps.get("fft_sizes")
        rates = caps.get("rate_codes")
        if not isinstance(sizes, list) or not sizes or \
                not all(type(s) is int and s > 0 for s in sizes):
            return None, None
        if not isinstance(rates, list) or not rates:
            return None, None
        spans: dict[int, int] = {}
        for entry in rates:
            if not isinstance(entry, dict):
                return None, None
            code, span = entry.get("code"), entry.get("span_khz")
            if type(code) is not int or type(span) is not int or span <= 0:
                return None, None
            spans[code] = span
        # effective values must agree with the capability table: the GUI
        # labels axes from rate-code spans and offers fft sizes from caps.
        if (type(eff.get("fft_size")) is not int
                or eff["fft_size"] not in sizes
                or type(eff.get("rate_code")) is not int
                or eff["rate_code"] not in spans
                or type(eff.get("span_khz")) is not int
                or eff["span_khz"] != spans[eff["rate_code"]]):
            return None, None
        return (
            {"fft_sizes": list(sizes),
             "rate_codes": [{"code": c, "span_khz": s}
                            for c, s in spans.items()],
             "bin_unit": caps.get("bin_unit")},
            {"fft_size": eff["fft_size"], "rate_code": eff["rate_code"],
             "span_khz": eff["span_khz"]},
        )

    def accept_rf(self, frame: SpectrumRf) -> bool:
        """Gate one 0x04 SPECTRUM_RF frame. True = apply to the display now
        (live). Sweep frames stage and are returned by the next accepted
        cycle marker via ``rf_flushed``. Stale epochs, the wrong band,
        frames from closed cycles, metadata incoherent with the
        acknowledged effective/caps/mode/channel contract, and any state
        without a valid RF capability ack are dropped (fail closed)."""
        if (not self.rf_ready or frame.epoch != self.epoch
                or frame.band != self.band):
            return False
        if self._last_cycle is not None and frame.cycle <= self._last_cycle:
            return False
        if not self._rf_coherent(frame):
            return False
        if self.mode == 1:               # sweep stages until the cycle marker
            if (self._rf_stage_cycle is not None
                    and frame.cycle != self._rf_stage_cycle):
                self.rf_stage.clear()    # partial cycle dropped (pause/abort)
            self._rf_stage_cycle = frame.cycle
            # bounded: at most one frame per advertised channel per cycle;
            # a duplicate replaces instead of growing the stage
            self.rf_stage[frame.channel] = frame
            return False
        return True

    def _rf_coherent(self, frame: SpectrumRf) -> bool:
        """Frame metadata must agree with the acknowledged contract:
        effective fft/span, active mode, advertised channel, and the
        rate_code -> caps entry lookup (by code, never array position)."""
        eff = self.spectrum_effective or {}
        if frame.fft_size != eff.get("fft_size"):
            return False
        if frame.span_khz != eff.get("span_khz"):
            return False
        if frame.mode != self.mode:
            return False
        if frame.channel not in self.channels:
            return False
        caps = self.spectrum_caps or {}
        entries = [r for r in caps.get("rate_codes", [])
                   if r.get("code") == frame.rate_code]
        return (len(entries) == 1
                and entries[0].get("span_khz") == frame.span_khz)

    def _accumulate_aps(self, observation: ChannelObservation) -> None:
        for ap in observation.aps:
            key = (ap["bssid"], observation.channel)
            self.aps[key] = {
                "bssid": ap["bssid"],
                "ssid": ap["ssid"],
                "ssid_hex": ap["ssid_hex"],
                "rx_channel": observation.channel,
                "advertised_channel": ap.get("primary_ch"),
                "rssi_dbm": ap.get("rssi_dbm"),
                "last_seen": self._clock(),
            }
        if len(self.aps) > MAX_TRACKED_APS:
            for key in sorted(self.aps, key=lambda k: self.aps[k]["last_seen"])[:8]:
                del self.aps[key]

    def prune_aps(self) -> None:
        now = self._clock()
        stale = [k for k, v in self.aps.items()
                 if now - v["last_seen"] > AP_MAX_AGE_SECONDS]
        for key in stale:
            del self.aps[key]

    def _accept_channel(self, data: dict) -> bool:
        if not self._measurement_epoch_ok(data):
            return False
        cycle = data.get("cycle")
        if type(cycle) is not int:
            raise ValueError("channel event requires integer cycle")
        if not 0 <= cycle <= 0xFFFFFFFF:
            raise ValueError("channel cycle out of uint32 range")
        # A replay at or below the last completed cycle is late/duplicate:
        # accepting it would resurrect values the bookkeeping superseded.
        if self._last_cycle is not None and cycle <= self._last_cycle:
            return False
        # Stale scope rejection BEFORE any side effect: a late older-cycle
        # observation after a newer cycle was seen must not poison the
        # newer coverage/staging (coverage is mutated only for cycles at
        # or above every known high-water mark).
        marks = [c for c in (self._coverage_cycle, self._staging_cycle,
                             self._util_stage_cycle) if c is not None]
        if marks and cycle < max(marks):
            return False
        observation = decode_channel(data)
        dropped = data.get("ap_dropped", 0)
        if type(dropped) is int and dropped >= 0:
            self.ap_dropped += dropped
        self.unavailable.discard(observation.channel)
        # Coverage (and staging) belong to exactly one cycle id: the first
        # observation of a newer cycle discards the previous cycle's
        # coverage, so a lost cycle event cannot keep old channels current.
        if self._coverage_cycle != cycle:
            self._cycle_seen.clear()
            self._coverage_cycle = cycle
        self._cycle_seen.add(observation.channel)
        if self.mode == 1:               # sweep stages until the cycle
            if self._staging_cycle is not None and cycle != self._staging_cycle:
                self.staging.clear()     # partial cycle dropped (pause/abort)
            self._staging_cycle = cycle
            self.staging[observation.channel] = observation
            # coverage wins over util: the staging scope is decided FIRST,
            # only then may this channel's util be staged (or gap)
            self._accept_util(data, observation, cycle)
            return False
        self._accept_util(data, observation, cycle)
        self.displayed[observation.channel] = observation
        self._accumulate_aps(observation)
        return True

    def _accept_util(self, data: dict, observation: ChannelObservation,
                     cycle: int) -> None:
        """Cycle-scoped, channel-bounded util write for ONE accepted
        channel event: only advertised channels may enter the maps (at most
        len(self.channels) entries), and a scope rejection touches NOTHING.
        Absence/invalid/unproven = gap - never stale, never zero-filled."""
        if not self._util_scope(cycle):
            return                  # older scope: no write, no replace
        sample = (parse_channel_util(data.get("util"))
                  if self.utilization_available
                  and observation.channel in self.channels else None)
        if sample is None:
            self.util_stage.pop(observation.channel, None)
            if self.mode == 0:      # LIVE: known invalid expires NOW;
                self._retire_util(observation.channel)  # SWEEP keeps its
                # old value until the matching marker retires/replaces it
        else:
            self.util_stage[observation.channel] = sample
            if self.mode == 0:      # live publishes immediately
                self._publish_util(observation.channel, sample, cycle)

    def _accept_cycle(self, data: dict) -> bool:
        if not self._measurement_epoch_ok(data):
            return False
        epoch = data.get("epoch")
        cycle = data.get("cycle")
        if type(epoch) is not int or type(cycle) is not int:
            raise ValueError("cycle event requires integer epoch and cycle")
        if not 0 <= cycle <= 0xFFFFFFFF:
            raise ValueError("cycle id out of uint32 range")
        # Serial delivers cycles in order within an epoch: a id <= the
        # highest accepted one is a duplicate or a stale replay.
        if self._last_cycle is not None and cycle <= self._last_cycle:
            return False
        self._last_cycle = cycle
        self.rf_flushed = []              # consume-once per accepted marker
        self.cycles += 1
        # The marker closes only its OWN cycle's coverage. When the marker's
        # observations never arrived (lost frames, or a marker ahead of all
        # coverage), the coverage set is empty: every advertised channel is
        # unavailable instead of borrowed from an older cycle.
        observed = (self._cycle_seen
                    if self._coverage_cycle == cycle else set())
        self.unavailable.update(set(self.channels) - observed)
        self._cycle_seen.clear()
        self._coverage_cycle = None
        if self.staging:
            if self._staging_cycle == cycle:
                self.displayed.update(self.staging)
                for observation in self.staging.values():
                    self._accumulate_aps(observation)
            self.staging.clear()     # never flush an older cycle as current
        self._staging_cycle = None
        # RF sweep staging: only frames whose OWN cycle matches the marker
        # are flushed; an older partial stage is dropped, never current.
        if self.rf_stage:
            if self._rf_stage_cycle == cycle:
                self.rf_flushed = list(self.rf_stage.values())
            self.rf_stage.clear()
        self._rf_stage_cycle = None
        # util retention (item 1): this marker closes cycle `cycle`'s
        # coverage. Staged values of THIS cycle replace their channel;
        # any published value not measured during the closed cycle is
        # truly missing and expires (missing never survives closure).
        # An OLDER marker never replaces newer staged/published state.
        # The cycle id stays the scope HIGH-WATER mark (never reset to
        # None): lost-marker jumps stay detectable by _util_scope.
        if (self._util_stage_cycle is not None
                and cycle < self._util_stage_cycle):
            pass                        # older marker: state stays intact
        else:
            if self._util_stage_cycle == cycle:
                for ch, staged_sample in self.util_stage.items():
                    self._publish_util(ch, staged_sample, cycle)
            self.util_stage.clear()
            if (self._util_stage_cycle is not None
                    and cycle > self._util_stage_cycle):
                self._util_stage_cycle = cycle
            for ch in [c for c in self.util_samples
                       if self._util_cycle.get(c) != cycle]:
                self._retire_util(ch)   # closed-cycle missing coverage
        self.elapsed_ms = data.get("elapsed_ms")
        self.uptime_ms = data.get("uptime_ms")
        return True

    def _accept_channel_error(self, data: dict) -> bool:
        if not self._measurement_epoch_ok(data):
            return False
        ch = data.get("ch")
        code = str(data.get("code", "unknown"))
        if type(ch) is not int:
            raise ValueError("channel_error without channel")
        # high-water guard runs FIRST: an older cycle's error can never
        # invalidate newer util; a newer/unknown cycle may (and starts a
        # fresh scope via _util_scope before the pops)
        cycle = data.get("cycle")
        if type(cycle) is not int or self._util_scope(cycle):
            # a failed dwell invalidates that channel's util NOW - staged
            # or published - so a failed channel cannot retain old percent
            self.util_stage.pop(ch, None)
            self._retire_util(ch)
        self.last_error = f"ch {ch}: {code}"
        already = ch in self.unavailable
        self.unavailable.add(ch)
        return not already
