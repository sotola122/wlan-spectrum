"""Reproducible hardware smoke test for the ESP32-C5 wifi-monitor stream.

Contract
--------
``monitor_smoke.py --port DEV [--seconds N] [--baud B] [--cases LIST] [--stream]``
``monitor_smoke.py --self-test``

* ``--port`` is required for a hardware run (unless ``--self-test``); only
  the given serial device is opened. The mock generator is never imported.
* ``--seconds`` (default 30) is the per-case collection window; each case
  stops early once its protocol obligations are met. ``--stream`` runs one
  continuous case for the whole window instead (long-run mode).
* Four mode/band cases (live/sweep x 2.4/5 GHz) are followed by protocol
  cases: 100 ms requested sweep (advertised dwell must stay >= 120 ms),
  FFT 1024 / 40 MS/s echo, an invalid CONFIG, a corrupted-CRC CONFIG
  (both must leave the active configuration unchanged), a byte-by-byte
  split CONFIG, and two concatenated CONFIGs where the newest must win.
* Every frame is decoded by the real ``wifi_spectrum.tlv.TlvParser`` and
  every channel observation by ``wifi_spectrum.monitor_data.decode_channel``.
* ``--self-test`` replays SYNTHETIC protocol fixtures (constructed
  bytes, not device captures: good, missing config,
  missing cycle, wrong epoch, malformed JSON, negative observation time,
  absent channel, unexpected SPECTRUM/CH_UTIL, empty RF) through the same checker
  plus known CRC vectors. No hardware is touched.

Exit status: 0 all checks passed, 1 at least one check failed,
2 usage or device I/O error.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import cast

import serial

from wifi_spectrum import tlv
from wifi_spectrum.monitor_data import SCHEMA, decode_channel
from wifi_spectrum.tlv import TlvParser

CONFIG_FIELDS = ("mode", "band", "sweep_ms", "fft_size",
                 "sample_rate_khz", "channel_dwell_ms", "cca_attempts")
CASES: tuple[tuple[str, int, int], ...] = (
    ("live-2.4", 0, 0),
    ("sweep-2.4", 1, 0),
    ("live-5", 0, 1),
    ("sweep-5", 1, 1),
)
DEFAULT_SWEEP_MS = 1000
DEFAULT_FFT = 64
DEFAULT_RATE_KHZ = 20000
SYNC_TIMEOUT_S = 10.0
MIN_DWELL_MS = 120

ConfigRequest = tuple[int, int, int, int, int, int, int]


# ---------------------------------------------------------------- checker
@dataclass
class CaseReport:
    name: str
    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cycles: int = 0
    rx_bytes: int = 0
    tlv_errors: int = 0
    ap_count: int = 0
    tx_dropped: int = 0
    ap_dropped: int = 0
    apply_ms: float | None = None
    dwell_ms: int | None = None
    cycle_ms: int | None = None

    def fail(self, reason: str) -> None:
        self.ok = False
        self.reasons.append(reason)

    def detail_lines(self) -> list[str]:
        seen: dict[str, None] = {}
        for text in self.reasons + self.notes:
            seen.setdefault(text)
        return [f"- {text}" for text in seen]

    def summary(self) -> str:
        def fmt(value: float | int | None) -> str:
            if value is None:
                return "-"
            return f"{value:.0f}" if isinstance(value, float) else str(value)

        status = "PASS" if self.ok else "FAIL"
        return (f"[{status}] {self.name}: cycles={self.cycles} "
                f"rx={self.rx_bytes}B tlv_errors={self.tlv_errors} "
                f"aps={self.ap_count} apply_ms={fmt(self.apply_ms)} "
                f"dwell_ms={fmt(self.dwell_ms)} cycle_ms={fmt(self.cycle_ms)}")


def _is_config(msg: object) -> bool:
    return isinstance(msg, tlv.Status) and msg.data.get("event") == "config"


def _config_fields(msg: tlv.Status) -> tuple:
    return tuple(msg.data.get(key) for key in CONFIG_FIELDS)


def evaluate(request: ConfigRequest, events: list[tuple[float, object]], *,
             tlv_errors: int = 0, min_cycles: int = 2,
             sent_at: float | None = None, name: str = "case",
             same_config: bool = False) -> CaseReport:
    """Validate one measured window against the wire/measurement contract.

    ``events`` are ``(monotonic_time, decoded_message)`` pairs in arrival
    order, typically produced from a real ``TlvParser`` feed. Returns a
    report whose ``reasons`` name every violated obligation.

    ``same_config`` marks a stream where the request equals the device's
    already-active config. Per protocol an identical CONFIG re-ack must NOT
    restart the running cycle, so the first completed cycle after joining
    may legitimately be partial (its first observed channel is not
    ``advertised[0]``). Only that joined cycle is exempt from the
    coverage check; every later cycle, and every first cycle of a CHANGED
    config (which must restart aligned under a new epoch), must be
    complete. Pre-ack events never count toward coverage or cycles.
    """
    rep = CaseReport(name=name)
    rep.tlv_errors = tlv_errors
    if tlv_errors:
        rep.fail(f"{tlv_errors} TLV/JSON parse errors during the session")

    epoch: int | None = None
    advertised: list[int] = []
    covered: set[int] = set()
    cycle_first_ch: int | None = None
    aps: set[str] = set()
    cycles = 0
    acked = False
    prev_cycle_id: int | None = None
    prev_uptime: int | None = None

    for when, msg in events:
        if isinstance(msg, tlv.Spectrum):
            rep.fail("unexpected SPECTRUM frame from a monitor device")
            continue
        if isinstance(msg, tlv.ChannelUtil):
            rep.fail("unexpected CH_UTIL frame from a monitor device")
            continue
        if isinstance(msg, dict):
            rep.fail("unexpected CONFIG frame from the device")
            continue
        if not isinstance(msg, tlv.Status):
            rep.fail("unexpected frame from a monitor device")
            continue
        data = msg.data
        if data.get("schema") != SCHEMA:
            rep.fail(f"status payload without {SCHEMA} schema: "
                     f"{data.get('schema')!r}")
            continue
        event = data.get("event")

        if event == "config":
            fields = _config_fields(msg)
            if fields != request:
                if acked:
                    rep.fail(f"device echoed config {fields} after "
                             f"acknowledging {request}")
                continue
            channels = data.get("channels")
            if (not isinstance(channels, list) or not channels
                    or not all(type(c) is int for c in channels)):
                rep.fail("config echo carries no valid channel list")
                continue
            new_epoch = data.get("epoch")
            if type(new_epoch) is not int:
                rep.fail("config echo without integer epoch")
                continue
            if new_epoch != epoch:
                covered.clear()
                cycle_first_ch = None
                prev_cycle_id = None
                prev_uptime = None
                if not acked and sent_at is not None:
                    rep.apply_ms = (when - sent_at) * 1000.0
            epoch, advertised, acked = new_epoch, list(channels), True
            dwell = data.get("dwell_ms")
            if type(dwell) is not int or dwell < MIN_DWELL_MS:
                rep.fail(f"advertised dwell_ms {dwell!r} below the "
                         f"{MIN_DWELL_MS} ms floor")
            else:
                rep.dwell_ms = dwell
                attempts = data.get("cca_attempts")
                if type(attempts) is int and dwell < 5 * attempts:
                    # effective dwell must cover the configured attempt
                    # budget (item 2: max(120, ..., 5 * cca_attempts))
                    rep.fail(f"dwell_ms {dwell} below the "
                             f"5 * cca_attempts = {5 * attempts} budget")
            for flag in ("spectrum", "cca", "fft_supported"):
                if data.get(flag) is not False:
                    rep.fail(f"config echo must declare {flag}: false")
            tx_dropped = data.get("tx_dropped")
            if type(tx_dropped) is int and tx_dropped > 0:
                rep.tx_dropped = tx_dropped
            continue

        if event in ("channel", "channel_error", "cycle"):
            if not acked:
                continue    # measurement of the device's previous config
            if data.get("epoch") != epoch:
                rep.fail(f"{event} event from old epoch {data.get('epoch')!r} "
                         f"after acknowledgement {epoch}")
                continue
            if data.get("band") != request[1]:
                rep.fail(f"{event} event from band {data.get('band')!r}, "
                         f"requested {request[1]}")
                continue

        if event == "channel":
            ms = data.get("observed_ms")
            if type(ms) is not int or ms <= 0:
                rep.fail(f"non-positive observed_ms {ms!r} on channel "
                         f"{data.get('ch')!r}")
                continue
            ch = data.get("ch")
            if ch not in advertised:
                rep.fail(f"observation for unadvertised channel {ch!r}")
            try:
                obs = decode_channel(data)
            except ValueError as exc:
                rep.fail(f"malformed channel observation: {exc}")
                continue
            covered.add(obs.channel)
            if cycle_first_ch is None:
                cycle_first_ch = obs.channel   # marker: where this cycle began
            for ap in obs.aps:
                aps.add(ap["bssid"])
            dropped = data.get("ap_dropped")
            if type(dropped) is int and dropped > 0:
                rep.ap_dropped += dropped
        elif event == "channel_error":
            ch = data.get("ch")
            if ch not in advertised:
                rep.fail(f"channel_error for unadvertised channel {ch!r}")
            if type(ch) is int:
                covered.add(ch)     # coverage: the device reported it
                if cycle_first_ch is None:
                    cycle_first_ch = ch
            rep.notes.append(f"channel {ch}: {data.get('code')}")
        elif event == "cycle":
            cid = data.get("cycle")
            if type(cid) is not int:
                rep.fail("cycle event without integer cycle id")
            elif prev_cycle_id is not None and cid <= prev_cycle_id:
                rep.fail(f"non-monotonic cycle id {cid} after {prev_cycle_id}")
            else:
                prev_cycle_id = cid
            elapsed = data.get("elapsed_ms")
            if type(elapsed) is not int or elapsed < 0:
                rep.fail(f"invalid elapsed_ms {elapsed!r}")
            else:
                rep.cycle_ms = elapsed
            uptime = data.get("uptime_ms")
            if type(uptime) is not int:
                rep.fail("cycle event without integer uptime_ms")
            elif prev_uptime is not None and uptime < prev_uptime:
                rep.fail(f"uptime decreased {uptime} < {prev_uptime}")
            else:
                prev_uptime = (uptime if prev_uptime is None
                               else max(prev_uptime, uptime))
            missing = sorted(set(advertised) - covered)
            if missing:
                joined = (cycles == 0 and same_config
                          and (cycle_first_ch is None
                               or cycle_first_ch != advertised[0]))
                if joined:
                    rep.notes.append(
                        f"cycle {cid} joined mid-flight after identical "
                        f"CONFIG; first-cycle coverage not required")
                else:
                    rep.fail(f"cycle {cid} missing channels {missing}")
            covered.clear()
            cycle_first_ch = None
            cycles += 1
        elif event == "error":
            rep.notes.append(f"device error: {data.get('code')}")
        else:
            rep.fail(f"unknown monitor event {event!r}")

    rep.cycles = cycles
    rep.ap_count = len(aps)
    if not acked:
        rep.fail("no config acknowledgement matching the requested fields")
    elif cycles < min_cycles:
        rep.fail(f"only {cycles} completed cycles, need {min_cycles}")
    if rep.ok and not aps:
        rep.notes.append("NO_AP_OBSERVED")
    return rep


# ---------------------------------------------------------------- self-test
def _cfg_bytes(request: ConfigRequest, epoch: int = 1,
               dwell: int = MIN_DWELL_MS) -> bytes:
    mode, band, sweep, fft, rate, channel_dwell_ms, cca_attempts = request
    return tlv.encode_status_json({
        "schema": SCHEMA, "event": "config", "fw": "wifi-monitor-0.1",
        "idf": "v6.0.3", "chip": "ESP32-C5", "country": "JP", "epoch": epoch,
        "mode": mode, "band": band, "sweep_ms": sweep, "fft_size": fft,
        "sample_rate_khz": rate, "channel_dwell_ms": channel_dwell_ms,
        "cca_attempts": cca_attempts, "dwell_ms": dwell,
        "channels": list(range(1, 14)), "spectrum": False, "cca": False,
        "fft_supported": False, "tx_dropped": 0,
    })


def _ch_bytes(ch: int, epoch: int = 1, cycle: int = 1, band: int = 0,
              packets: int = 24, peak: int | None = -48, ms: int = 120,
              ap: dict | None = None) -> bytes:
    return tlv.encode_status_json({
        "schema": SCHEMA, "event": "channel", "epoch": epoch, "cycle": cycle,
        "band": band, "ch": ch, "observed_ms": ms, "packets": packets,
        "peak_rssi_dbm": peak,
        "aps": [ap] if ap else [], "ap_dropped": 0,
    })


def _cy_bytes(epoch: int = 1, cycle: int = 1, band: int = 0,
              elapsed: int = 1600, uptime: int = 1600) -> bytes:
    return tlv.encode_status_json({
        "schema": SCHEMA, "event": "cycle", "epoch": epoch, "cycle": cycle,
        "band": band, "elapsed_ms": elapsed, "uptime_ms": uptime,
    })


def _full_cycle(cycle: int, epoch: int = 1, band: int = 0,
                packets: int = 24, peak: int | None = -48,
                skip: tuple[int, ...] = ()) -> bytes:
    """SYNTHETIC device fixture (constructed bytes, not a capture): one
    channel sweep plus its cycle event."""
    ap = {"bssid": "001122334455", "ssid_hex": "74657374",
          "primary_ch": 6, "rssi_dbm": -48} if packets > 0 else None
    out = b"".join(
        _ch_bytes(ch, epoch=epoch, cycle=cycle, band=band, packets=packets,
                  peak=peak, ap=ap if ch == 6 else None)
        for ch in range(1, 14) if ch not in skip)
    return out + _cy_bytes(epoch=epoch, cycle=cycle, band=band,
                           elapsed=1600 * cycle,
                           uptime=(epoch * 10000 + cycle * 1600))


def check_session_errors(errors_since_sync: int) -> CaseReport:
    """Session-wide verdict: corruption landing in an inter-case drain
    window is fed by ``clear_input`` *before* the next per-case baseline
    and would otherwise vanish. Startup sync noise is excluded by reading
    ``Device.session_baseline`` (captured after sync returns)."""
    rep = CaseReport(name="session")
    rep.tlv_errors = errors_since_sync
    if errors_since_sync:
        rep.fail(f"{errors_since_sync} TLV/JSON parse errors since sync "
                 f"(inter-case drains included)")
    return rep


def self_test_fixtures() -> list[tuple[str, bytes, ConfigRequest, bool,
                                       bool, str | None]]:
    """(name, SYNTHETIC byte stream, requested config, same_config,
    expect_ok, must_contain). ``same_config`` marks streams where the
    request equals the device's already-active config (identical CONFIG
    re-ack: the device must NOT restart its running cycle). Streams are
    constructed in code - valid tests, not hardware captures."""
    request: ConfigRequest = (0, 0, 1000, 64, 20000, 0, 16)
    return [
        ("good-two-cycles",
         _cfg_bytes(request) + _full_cycle(1) + _full_cycle(2),
         request, True, True, None),
        ("missing-config",
         _full_cycle(1) + _full_cycle(2),
         request, True, False, "no config acknowledgement"),
        ("missing-cycle",
         _cfg_bytes(request) + _full_cycle(1),
         request, True, False, "completed cycles"),
        ("wrong-epoch",
         _cfg_bytes(request, epoch=1) + _full_cycle(1, epoch=2),
         request, False, False, "old epoch"),
        ("malformed-json",
         _cfg_bytes(request) + tlv.frame(tlv.T_STATUS, b"{broken json")
         + _full_cycle(1) + _full_cycle(2),
         request, True, False, "parse errors"),
        ("negative-observation",
         _cfg_bytes(request) + _ch_bytes(1, ms=-5) + _full_cycle(1)
         + _full_cycle(2),
         request, True, False, "observed_ms"),
        ("absent-channel",
         _cfg_bytes(request) + _full_cycle(1, skip=(13,))
         + _full_cycle(2, skip=(13,)),
         request, True, False, "missing channels"),
        ("unexpected-spectrum",
         _cfg_bytes(request) + tlv.encode_spectrum(2400.0, 0.5, [-50.0])
         + _full_cycle(1) + _full_cycle(2),
         request, True, False, "SPECTRUM"),
        ("unexpected-ch-util",
         _cfg_bytes(request) + tlv.encode_ch_util(0, {6: 40})
         + _full_cycle(1) + _full_cycle(2),
         request, True, False, "CH_UTIL"),
        ("empty-observations",
         _cfg_bytes(request) + _full_cycle(1, packets=0, peak=None)
         + _full_cycle(2, packets=0, peak=None),
         request, True, True, "NO_AP_OBSERVED"),
        # SYNTHETIC reproduction of the UART smoke failure: identical
        # CONFIG re-acked while cycle 26 was already at channels 12-13
        # (protocol-correct: an identical CONFIG must not restart it).
        ("identical-config-mid-cycle-join",
         _cfg_bytes(request)
         + _ch_bytes(12, cycle=26) + _ch_bytes(13, cycle=26)
         + _cy_bytes(cycle=26, elapsed=1586, uptime=41600)
         + _full_cycle(27),
         request, True, True, "joined mid-flight after identical CONFIG"),
        # The leniency above is for the joined cycle ONLY: a later cycle
        # that misses a channel must still fail.
        ("late-incomplete-cycle-fails",
         _cfg_bytes(request)
         + _ch_bytes(12, cycle=26) + _ch_bytes(13, cycle=26)
         + _cy_bytes(cycle=26, elapsed=1586, uptime=41600)
         + _full_cycle(27) + _full_cycle(28, skip=(13,)),
         request, True, False, "missing channels"),
        # A window that never completes a cycle stays a failure.
        ("no-cycle-timeout",
         _cfg_bytes(request) + _ch_bytes(12, cycle=26)
         + _ch_bytes(13, cycle=26),
         request, True, False, "completed cycles"),
        # CHANGED config: the device must discard the running cycle and
        # restart aligned at advertised[0] under a new epoch - a partial
        # first cycle here is a defect, never a join.
        ("changed-epoch-partial-first-cycle",
         _cfg_bytes(request, epoch=2)
         + _ch_bytes(12, epoch=2, cycle=1) + _ch_bytes(13, epoch=2, cycle=1)
         + _cy_bytes(epoch=2, cycle=1)
         + _full_cycle(2, epoch=2),
         request, False, False, "missing channels"),
        # Measurements emitted BEFORE their config acknowledgement (the
        # early-data/ACK defect) must not satisfy the case: pre-ack events
        # never count toward cycles or coverage.
        ("changed-epoch-early-data-before-ack",
         _full_cycle(1, epoch=2) + _full_cycle(2, epoch=2)
         + _cfg_bytes(request, epoch=2),
         request, False, False, "completed cycles"),
    ]


def _run_bytes(name: str, request: ConfigRequest, blob: bytes,
               min_cycles: int = 2, same_config: bool = False) -> CaseReport:
    """Feed a SYNTHETIC stream through the real parser (in odd chunks) and
    hand the decoded messages to the same checker used for hardware."""
    parser = TlvParser()
    events: list[tuple[float, object]] = []
    stamp = 0.0
    for offset in range(0, len(blob), 7):
        stamp += 0.001
        for msg in parser.feed(blob[offset:offset + 7]):
            events.append((stamp, msg))
    return evaluate(request, events, tlv_errors=parser.errors,
                    min_cycles=min_cycles, name=name,
                    same_config=same_config)


def corrupt_config_checks() -> list[str]:
    """Host-side wire vectors the smoke relies on when corrupting configs."""
    failures: list[str] = []
    golden = bytes.fromhex(
        "10 0d 00 01 01 e8 03 40 00 20 4e 00 00 00 00 10 2f 2a aa ff")
    parser = TlvParser()
    messages = parser.feed(golden)
    expected = [{"mode": 1, "band": 1, "sweep_ms": 1000, "fft_size": 64,
                 "sample_rate_khz": 20000, "channel_dwell_ms": 0,
                 "cca_attempts": 16}]
    if messages != expected:
        failures.append(f"golden CONFIG decoded as {messages!r}")
    if tlv.crc32(b"123456789") != 0xCBF43926:
        failures.append("CRC-32/ISO-HDLC check value mismatch")
    corrupted = bytearray(golden)
    corrupted[4] ^= 0x01                    # payload bit flip -> CRC failure
    parser = TlvParser()
    messages = parser.feed(bytes(corrupted))
    if messages:
        failures.append("corrupted CONFIG was decoded instead of rejected")
    if parser.errors == 0:
        failures.append("corrupted CONFIG was not counted as an error")
    parser = TlvParser()
    if parser.feed(golden[:16]):           # legacy CRC-less frame
        failures.append("legacy CRC-less frame accepted")
    return failures


def run_self_test() -> tuple[bool, list[str]]:
    lines: list[str] = []
    ok = True
    for name, blob, request, same_config, want_ok, want_in \
            in self_test_fixtures():
        report = _run_bytes(name, request, blob, same_config=same_config)
        haystack = " | ".join(report.reasons + report.notes)
        good = report.ok == want_ok and (want_in is None or want_in in haystack)
        marker = "OK" if good else "BAD"
        ok = ok and good
        lines.append(f"[{marker}] fixture {name}: {report.summary()}")
        for line in report.detail_lines():
            lines.append("       " + line)
    for failure in corrupt_config_checks():
        ok = False
        lines.append(f"[BAD] wire vector: {failure}")
    if not lines:
        lines.append("[BAD] no fixtures executed")
        ok = False
    return ok, lines


# ---------------------------------------------------------------- hardware
class Device:
    """Thin serial session: real decoder in front of one serial port.

    The parser is never reset after synchronisation, so framing stays
    aligned across phases; ``clear_input`` feeds every pending byte through
    it and only drops the decoded messages (they belong to the previous
    phase). Per-case ``tlv_errors`` are counted from a baseline recorded
    after each clear; ``session_baseline`` (captured once after sync
    returns) covers the whole measured session so corruption landing in a
    drain window cannot hide behind a per-case baseline.
    """

    def __init__(self, port: str, baud: int) -> None:
        self.ser = serial.Serial(port, baud, timeout=0.1)
        self.parser = TlvParser()
        self.rx = 0
        self.sync_fields: ConfigRequest | None = None
        self.session_baseline: int = 0

    def close(self) -> None:
        self.ser.close()

    def send(self, blob: bytes) -> None:
        self.ser.write(blob)

    def clear_input(self) -> None:
        hard_deadline = time.monotonic() + 1.0
        while time.monotonic() < hard_deadline:
            chunk = self.ser.read(4096)
            if not chunk:
                return              # one quiet read: buffer drained
            self.rx += len(chunk)
            self.parser.feed(chunk)

    def sync(self, timeout_s: float = SYNC_TIMEOUT_S) -> bool:
        """Wait for the first wifi-monitor config event (startup tolerant)."""
        self.clear_input()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            chunk = self.ser.read(4096)
            if chunk:
                self.rx += len(chunk)
                for msg in self.parser.feed(chunk):
                    if (isinstance(msg, tlv.Status)
                            and msg.data.get("event") == "config"):
                        self.sync_fields = cast(
                            ConfigRequest, tuple(_config_fields(msg)))
                        return True
        return False

    def collect(self, window_s: float,
                stop_pred=None) -> list[tuple[float, object]]:
        events: list[tuple[float, object]] = []
        deadline = time.monotonic() + window_s
        while time.monotonic() < deadline:
            chunk = self.ser.read(4096)
            if chunk:
                self.rx += len(chunk)
                for msg in self.parser.feed(chunk):
                    events.append((time.monotonic(), msg))
            if stop_pred is not None and stop_pred(events):
                break
        return events


def _case_done(events: list[tuple[float, object]],
               request: ConfigRequest, min_cycles: int) -> bool:
    """Stop predicate aligned with ``evaluate``: only cycles arriving AFTER
    the matching acknowledgement, in the acknowledged epoch and requested
    band, count. Pre-ack or old-epoch cycles (early-data defects) must not
    end collection early."""
    acked = False
    epoch: object = None
    cycles = 0
    for _, msg in events:
        if not isinstance(msg, tlv.Status):
            continue
        data = msg.data
        event = data.get("event")
        if event == "config" and _config_fields(msg) == request:
            acked = True
            epoch = data.get("epoch")
        elif (event == "cycle" and acked
              and data.get("epoch") == epoch
              and data.get("band") == request[1]):
            cycles += 1
    return acked and cycles >= min_cycles


def _echoes_seen(events: list[tuple[float, object]], needed: int = 2) -> bool:
    return sum(1 for _, msg in events if _is_config(msg)) >= needed


def run_case(dev: Device, name: str, request: ConfigRequest,
             window_s: float, min_cycles: int = 2,
             chunks: list[bytes] | None = None,
             prev_active: ConfigRequest | None = None) -> CaseReport:
    """``prev_active`` is the device's config before this request; when the
    request equals it the identical-CONFIG mid-cycle join rule applies."""
    dev.clear_input()
    baseline = dev.parser.errors
    rx0 = dev.rx
    sent_at = time.monotonic()
    for piece in (chunks if chunks is not None
                  else [tlv.encode_config(*request)]):
        dev.send(piece)
    events = dev.collect(window_s,
                         lambda ev: _case_done(ev, request, min_cycles))
    rep = evaluate(request, events, tlv_errors=dev.parser.errors - baseline,
                   min_cycles=min_cycles, sent_at=sent_at, name=name,
                   same_config=prev_active is not None
                   and request == prev_active)
    rep.rx_bytes = dev.rx - rx0
    return rep


def run_active_config_check(dev: Device, name: str,
                            previous: ConfigRequest, send_blob: bytes,
                            window_s: float) -> CaseReport:
    """Send garbage the device must reject; the active config must not
    change (heartbeats keep echoing ``previous``)."""
    dev.clear_input()
    baseline = dev.parser.errors
    rx0 = dev.rx
    dev.send(send_blob)
    events = dev.collect(window_s, lambda ev: _echoes_seen(ev))
    rep = CaseReport(name=name)
    rep.tlv_errors = dev.parser.errors - baseline
    rep.rx_bytes = dev.rx - rx0
    if rep.tlv_errors:
        rep.fail(f"{rep.tlv_errors} TLV/JSON parse errors during the session")
    echoes = [f for _, msg in events if _is_config(msg)
              and isinstance(msg, tlv.Status)
              for f in [_config_fields(msg)]]
    if not echoes:
        rep.fail("no heartbeat config echo; device stopped reporting")
    for fields in echoes:
        if fields != previous:
            rep.fail(f"device applied unrequested config {fields}; active "
                     f"configuration must stay {previous}")
            break
    for _, msg in events:
        if (isinstance(msg, tlv.Status)
                and msg.data.get("schema") == SCHEMA
                and msg.data.get("event") == "error"):
            rep.notes.append(f"device error: {msg.data.get('code')}")
    if rep.ok:
        rep.notes.append(f"active configuration unchanged {previous}")
    return rep


def _corrupt_config_blob() -> bytes:
    blob = bytearray(tlv.encode_config(0, 0, 9999, 64, DEFAULT_RATE_KHZ))
    blob[4] ^= 0x01                 # payload flip: CRC32 no longer matches
    return bytes(blob)


def _select_cases(spec: str | None) -> list[tuple[str, int, int]]:
    if spec is None:
        return list(CASES)
    by_name = {name: (name, mode, band) for name, mode, band in CASES}
    wanted = [part.strip() for part in spec.split(",") if part.strip()]
    unknown = [name for name in wanted if name not in by_name]
    if unknown:
        raise ValueError(f"unknown case(s): {', '.join(unknown)}; "
                         f"choose from {', '.join(n for n, _, _ in CASES)}")
    return [by_name[name] for name in wanted]


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="monitor_smoke",
        description="Protocol smoke test for the real ESP32-C5 wifi-monitor "
                    "stream (never imports the mock generator).")
    parser.add_argument("--port", help="serial device (required unless "
                                       "--self-test)")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--seconds", type=float, default=30.0,
                        help="per-case collection window (default 30; "
                             "with --stream: total run time)")
    parser.add_argument("--cases", metavar="LIST",
                        help="comma-separated subset of "
                             + ",".join(name for name, _, _ in CASES))
    parser.add_argument("--stream", action="store_true",
                        help="one continuous protocol-checked case for the "
                             "whole --seconds window (long-run mode)")
    parser.add_argument("--self-test", action="store_true",
                        help="replay recorded fixtures through the checker "
                             "and verify wire vectors; no port needed")
    return parser


def _print_report(rep: CaseReport) -> None:
    print(rep.summary())
    for line in rep.detail_lines():
        print("       " + line)


def _run_hardware(args: argparse.Namespace,
                  parser: argparse.ArgumentParser) -> int:
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    try:
        selected = _select_cases(args.cases)
    except ValueError as exc:
        parser.error(str(exc))
    if args.stream:
        selected = selected[:1]

    try:
        dev = Device(args.port, args.baud)
    except (serial.SerialException, OSError, ValueError) as exc:
        print(f"error: cannot open {args.port}: {exc}", file=sys.stderr)
        return 2

    reports: list[CaseReport] = []
    session = check_session_errors(0)
    try:
        print(f"wifi-monitor smoke: port={args.port} baud={args.baud} "
              f"window={args.seconds:g}s")
        if not dev.sync():
            print(f"[FAIL] sync: no wifi-monitor config within "
                  f"{SYNC_TIMEOUT_S:g}s ({dev.rx}B received)")
            return 1
        print(f"[ok] sync: device config received ({dev.rx}B)")
        # startup sync noise is excluded from the session-wide verdict
        dev.session_baseline = dev.parser.errors
        active: ConfigRequest | None = dev.sync_fields

        if args.stream:
            name, mode, band = selected[0]
            request = (mode, band, DEFAULT_SWEEP_MS, DEFAULT_FFT,
                       DEFAULT_RATE_KHZ, 0, 16)
            dev.clear_input()
            baseline = dev.parser.errors
            rx0 = dev.rx
            sent_at = time.monotonic()
            dev.send(tlv.encode_config(*request))
            events = dev.collect(args.seconds)
            rep = evaluate(request, events,
                           tlv_errors=dev.parser.errors - baseline,
                           min_cycles=2, sent_at=sent_at,
                           name=f"stream-{name}",
                           same_config=active is not None
                           and request == active)
            rep.rx_bytes = dev.rx - rx0
            reports.append(rep)
        else:
            for name, mode, band in selected:
                request = (mode, band, DEFAULT_SWEEP_MS, DEFAULT_FFT,
                           DEFAULT_RATE_KHZ, 0, 16)
                rep = run_case(dev, name, request, args.seconds,
                               prev_active=active)
                reports.append(rep)
                if rep.apply_ms is not None:
                    active = request

            if args.cases is None:
                extra_window = min(args.seconds, 15.0)
                dwell_req: ConfigRequest = (0, 0, 100, DEFAULT_FFT,
                                            DEFAULT_RATE_KHZ, 0, 16)
                rep = run_case(dev, "dwell-100ms", dwell_req, extra_window,
                               min_cycles=1, prev_active=active)
                reports.append(rep)
                if rep.apply_ms is not None:
                    active = dwell_req

                fft_req: ConfigRequest = (0, 0, DEFAULT_SWEEP_MS, 1024, 40000,
                                          0, 16)
                rep = run_case(dev, "fft-1024-rate-40000", fft_req,
                               extra_window, min_cycles=1,
                               prev_active=active)
                reports.append(rep)
                if rep.apply_ms is not None:
                    active = fft_req

                if active is not None:
                    reports.append(run_active_config_check(
                        dev, "invalid-config", active,
                        tlv.encode_config(5, 0, DEFAULT_SWEEP_MS, DEFAULT_FFT,
                                          DEFAULT_RATE_KHZ),
                        extra_window))
                    reports.append(run_active_config_check(
                        dev, "corrupt-config", active, _corrupt_config_blob(),
                        extra_window))
                    reports.append(run_active_config_check(
                        dev, "invalid-band2", active,
                        tlv.encode_config(0, 2, DEFAULT_SWEEP_MS,
                                          DEFAULT_FFT, DEFAULT_RATE_KHZ),
                        extra_window))
                    reports.append(run_active_config_check(
                        dev, "wrong-length-config", active,
                        tlv.frame(tlv.T_CONFIG,
                                  b"\x00" * (tlv.CONFIG.size - 1)),
                        extra_window))

                split_req: ConfigRequest = (1, 0, DEFAULT_SWEEP_MS,
                                            DEFAULT_FFT, DEFAULT_RATE_KHZ,
                                            0, 16)
                split_blob = tlv.encode_config(*split_req)
                rep = run_case(dev, "split-config-bytewise", split_req,
                               extra_window, min_cycles=1,
                               prev_active=active,
                               chunks=[split_blob[i:i + 1]
                                       for i in range(len(split_blob))])
                reports.append(rep)
                if rep.apply_ms is not None:
                    active = split_req

                oldest: ConfigRequest = (1, 1, DEFAULT_SWEEP_MS, DEFAULT_FFT,
                                         DEFAULT_RATE_KHZ, 0, 16)
                newest: ConfigRequest = (0, 0, DEFAULT_SWEEP_MS, DEFAULT_FFT,
                                         DEFAULT_RATE_KHZ, 0, 16)
                rep = run_case(dev, "concat-config-newest-wins", newest,
                               extra_window, min_cycles=1,
                               prev_active=active,
                               chunks=[tlv.encode_config(*oldest)
                                       + tlv.encode_config(*newest)])
                reports.append(rep)
        session = check_session_errors(dev.parser.errors
                                       - dev.session_baseline)
    except (serial.SerialException, OSError) as exc:
        print(f"error: serial failure during the run: {exc}", file=sys.stderr)
        return 2
    finally:
        dev.close()

    for rep in reports:
        _print_report(rep)
    if session.ok:
        print("[ok] session: 0 TLV/JSON parse errors since sync "
              "(inter-case drains included)")
    else:
        _print_report(session)
    failed = [rep.name for rep in reports if not rep.ok]
    if failed:
        print(f"result: FAIL ({len(failed)}/{len(reports)} checks failed: "
              + ", ".join(failed) + ")")
    elif not session.ok:
        print("result: FAIL (session parser errors since sync)")
    else:
        print(f"result: PASS ({len(reports)} checks)")
    return 0 if not failed and session.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        ok, lines = run_self_test()
        for line in lines:
            print(line)
        print("self-test:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    if not args.port:
        parser.error("--port is required unless --self-test is given")
    return _run_hardware(args, parser)


if __name__ == "__main__":
    sys.exit(main())
