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

SCHEMA = "wifi-monitor/1"
MAX_OBSERVATION_APS = 8
MAX_TRACKED_APS = 256            # GUI AP table bound
AP_MAX_AGE_SECONDS = 30.0        # sightings older than this are dropped


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
                sample_rate_khz: int) -> None:
        self._requested = (mode, band, sweep_ms, fft_size, sample_rate_khz)

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
        if not self.ready or self.epoch is None:
            return False
        if data.get("epoch") != self.epoch:
            return False
        return data.get("band") == self.band

    def _clear_measurements(self) -> None:
        self.displayed.clear()
        self.staging.clear()
        self._staging_cycle = None
        self.aps.clear()
        self.cycles = 0
        self._last_cycle = None
        self._cycle_seen.clear()
        self._coverage_cycle = None
        self.ap_dropped = 0
        self.unavailable.clear()
        self.elapsed_ms = None

    def _accept_config(self, data: dict) -> bool:
        fields = tuple(data.get(k) for k in
                       ("mode", "band", "sweep_ms", "fft_size",
                        "sample_rate_khz"))
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
        self.mode, self.band, sweep, fft, rate = fields
        self.channels = tuple(channel_list)
        self.dwell_ms = data.get("dwell_ms")
        tx_dropped = data.get("tx_dropped", 0)
        if type(tx_dropped) is int and tx_dropped >= 0:
            self.tx_dropped = tx_dropped
        return True

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
        # A replay at or below the last completed cycle is late/duplicate:
        # accepting it would resurrect values the bookkeeping superseded.
        if self._last_cycle is not None and cycle <= self._last_cycle:
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
            return False
        self.displayed[observation.channel] = observation
        self._accumulate_aps(observation)
        return True

    def _accept_cycle(self, data: dict) -> bool:
        if not self._measurement_epoch_ok(data):
            return False
        epoch = data.get("epoch")
        cycle = data.get("cycle")
        if type(epoch) is not int or type(cycle) is not int:
            raise ValueError("cycle event requires integer epoch and cycle")
        # Serial delivers cycles in order within an epoch: a id <= the
        # highest accepted one is a duplicate or a stale replay.
        if self._last_cycle is not None and cycle <= self._last_cycle:
            return False
        self._last_cycle = cycle
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
        self.last_error = f"ch {ch}: {code}"
        already = ch in self.unavailable
        self.unavailable.add(ch)
        return not already
