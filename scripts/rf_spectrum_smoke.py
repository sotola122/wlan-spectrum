#!/usr/bin/env python3
"""RF spectrum smoke: verify the 0x04 SPECTRUM_RF contract end to end.

Drives a CONFIG frame over a real serial port (or a POSIX PTY in tests),
then evaluates the decoded stream against handoff v2.1:

- STATUS config ack echoing the request, with valid spectrum caps and
  matching effective values when ``spectrum`` is true;
- 0x04 frames only AFTER the ack, from that epoch, for that band;
- monotonic STATUS cycle markers, frames never after their own marker;
- parser errors stay at zero after the first ack (UART-print risk);
- no demo 0x01 frames; 0x02 only when utilization was declared available.

``--self-test`` runs the same evaluator over SYNTHETIC fixtures through the
real TlvParser - no port, no hardware, no mock generator import.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field

from wifi_spectrum import tlv
from wifi_spectrum.bands import channel_freq
from wifi_spectrum.monitor_data import (SCHEMA, UTIL_CONFIDENCE,
                                        UTIL_SOURCE, parse_channel_util)


# ------------------------------------------------------------------ model
@dataclass(frozen=True)
class ConfigRequest:
    mode: int
    band: int
    sweep_ms: int
    fft_size: int
    sample_rate_khz: int

    def as_tuple(self) -> tuple:
        return (self.mode, self.band, self.sweep_ms, self.fft_size,
                self.sample_rate_khz)


@dataclass
class Report:
    ok: bool = True
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    acked: bool = False
    epoch: int | None = None
    spectrum: bool = False
    utilization: bool = False
    util_events: int = 0        # actual valid Sampled PHY CCA sample events
    frames: int = 0
    cycles: int = 0
    caps: dict | None = None
    eff: dict | None = None
    channels: list[int] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    ap_dropped: int = 0
    tx_dropped: int = 0

    def fail(self, msg: str) -> None:
        self.ok = False
        self.failures.append(msg)


def _config_fields(msg: tlv.Status) -> tuple:
    d = msg.data
    return (d.get("mode"), d.get("band"), d.get("sweep_ms"),
            d.get("fft_size"), d.get("sample_rate_khz"))


def _caps_valid(data: dict) -> str | None:
    """Return a failure reason, or None when caps+effective are coherent."""
    caps, eff = data.get("spectrum_caps"), data.get("spectrum_effective")
    if not isinstance(caps, dict) or not isinstance(eff, dict):
        return "spectrum:true without caps and effective dicts"
    sizes = caps.get("fft_sizes")
    rates = caps.get("rate_codes")
    if not isinstance(sizes, list) or not sizes or \
            not all(type(s) is int and s > 0 for s in sizes):
        return f"invalid fft_sizes {sizes!r}"
    spans: dict[int, int] = {}
    if not isinstance(rates, list) or not rates:
        return f"invalid rate_codes {rates!r}"
    for entry in rates:
        if (not isinstance(entry, dict)
                or type(entry.get("code")) is not int
                or type(entry.get("span_khz")) is not int
                or entry.get("span_khz", 0) <= 0):
            return f"invalid rate entry {entry!r}"
        spans[entry["code"]] = entry["span_khz"]
    if (type(eff.get("fft_size")) is not int
            or eff["fft_size"] not in sizes
            or type(eff.get("rate_code")) is not int):
        return f"effective {eff!r} inconsistent with caps {caps!r}"
    rate_code = eff["rate_code"]
    if (rate_code not in spans
            or eff.get("span_khz") != spans[rate_code]):
        return f"effective {eff!r} inconsistent with caps {caps!r}"
    if caps.get("bin_unit") != "centi_dbfs":
        return f"bin_unit {caps.get('bin_unit')!r} != centi_dbfs"
    return None


# ---------------------------------------------------------------- evaluate
def evaluate(request: ConfigRequest, events: list[tuple[float, object]], *,
             tlv_errors: int = 0, min_cycles: int = 2,
             clean_stream: bool = True) -> Report:
    """``events`` are ``(monotonic_time, decoded_message)`` pairs."""
    rep = Report()
    epoch: int | None = None
    last_cycle: int | None = None
    marker_open: int | None = None    # newest cycle marker already passed
    covered_cycle: int | None = None  # cycle id owning the scopes below
    rf_seen: set[int] = set()         # channels with a validated 0x04 frame
    ch_errors: set[int] = set()       # explicit channel_error gaps
    first_reported: int | None = None  # first signal of the open cycle

    def open_cycle(cid: int) -> None:
        """Bind coverage to one cycle id: a newer cycle never inherits
        another cycle's coverage (a lost marker must not mix cycles)."""
        nonlocal covered_cycle, first_reported
        if covered_cycle is None:
            covered_cycle = cid
        elif cid > covered_cycle:
            rf_seen.clear()
            ch_errors.clear()
            covered_cycle = cid
            first_reported = None

    for _when, msg in events:
        if isinstance(msg, tlv.Spectrum):
            rep.fail("unexpected demo SPECTRUM (0x01) from an RF device")
            continue
        if isinstance(msg, tlv.ChannelUtil):
            if rep.acked and not rep.utilization:
                rep.fail("CH_UTIL (0x02) while utilization is unavailable")
            else:
                rep.notes.append("CH_UTIL received (utilization available)")
            continue
        if isinstance(msg, dict):
            rep.fail("unexpected CONFIG frame from the device")
            continue
        if isinstance(msg, tlv.SpectrumRf):
            if not rep.acked:
                # cold-join: frames queued before the matching ack belong
                # to an unknown epoch - ignored, never counted, never failed
                continue
            if not rep.spectrum:
                rep.fail("0x04 frame while spectrum:false")
                continue
            if msg.epoch != rep.epoch:
                rep.fail(f"0x04 frame from epoch {msg.epoch}, "
                         f"acked {rep.epoch}")
                continue
            if msg.band != request.band:
                rep.fail(f"0x04 frame from band {msg.band}, "
                         f"requested {request.band}")
                continue
            if marker_open is not None and msg.cycle < marker_open:
                rep.fail(f"0x04 cycle {msg.cycle} after marker "
                         f"{marker_open - 1}")
                continue
            if (msg.span_khz <= 0 or msg.source != 0
                    or len(msg.power_dbfs) != msg.fft_size):
                rep.fail("malformed 0x04 header fields")
                continue
            # the effective ack and capability table are the authority:
            # frames must match them exactly, never a fixture constant
            eff = rep.eff or {}
            caps = rep.caps or {}
            if msg.fft_size != eff.get("fft_size"):
                rep.fail(f"frame fft {msg.fft_size} != effective "
                         f"{eff.get('fft_size')}")
                continue
            if msg.span_khz != eff.get("span_khz"):
                rep.fail(f"frame span {msg.span_khz} != effective "
                         f"{eff.get('span_khz')}")
                continue
            entries = [r for r in caps.get("rate_codes", [])
                       if r.get("code") == msg.rate_code]
            if len(entries) != 1 or entries[0]["span_khz"] != msg.span_khz:
                rep.fail(f"rate_code {msg.rate_code} does not match its "
                         f"caps entry for span {msg.span_khz}")
                continue
            if msg.mode != request.mode:
                rep.fail(f"frame mode {msg.mode} != configured mode "
                         f"{request.mode}")
                continue
            if msg.channel not in rep.channels:
                rep.fail(f"frame channel {msg.channel} not in advertised "
                         f"{rep.channels}")
                continue
            centre = int(channel_freq(request.band, msg.channel) * 1000)
            if msg.center_khz != centre:
                rep.fail(f"frame centre {msg.center_khz} kHz != channel "
                         f"centre {centre} kHz")
                continue
            open_cycle(msg.cycle)
            rep.frames += 1
            rf_seen.add(msg.channel)
            if first_reported is None:
                first_reported = msg.channel
            continue
        if not isinstance(msg, tlv.Status):
            rep.fail(f"unexpected frame type {type(msg).__name__}")
            continue
        data = msg.data
        if data.get("schema") != SCHEMA:
            rep.fail(f"status payload without {SCHEMA}: {data.get('schema')!r}")
            continue
        event = data.get("event")

        if event == "config":
            if _config_fields(msg) != request.as_tuple():
                if rep.acked:
                    rep.fail("device reconfigured after acknowledging")
                continue
            new_epoch = data.get("epoch")
            if type(new_epoch) is not int:
                rep.fail("config ack without integer epoch")
                continue
            if new_epoch != epoch:
                last_cycle, marker_open = None, None
                covered_cycle = None     # never count old-epoch data
                rf_seen.clear()
                ch_errors.clear()
                first_reported = None
                epoch = new_epoch
            spectrum = data.get("spectrum")
            if type(spectrum) is not bool:
                rep.fail("config ack without boolean spectrum flag")
                continue
            if spectrum:
                reason = _caps_valid(data)
                if reason:
                    rep.fail(f"invalid RF capabilities: {reason}")
                    continue
            rep.acked, rep.epoch, rep.spectrum = True, epoch, spectrum
            channel_list = data.get("channels")
            if (not isinstance(channel_list, list) or not channel_list
                    or not all(type(c) is int for c in channel_list)):
                rep.fail("config ack without a valid channel list")
                continue
            rep.channels = list(channel_list)
            rep.caps = data.get("spectrum_caps") if spectrum else None
            rep.eff = data.get("spectrum_effective") if spectrum else None
            # NOTE: an identical heartbeat (same epoch) must NEVER reset
            # cycle coverage, first-channel, or marker state - only the
            # epoch-change branch above does.
            util = data.get("utilization")
            if not isinstance(util, dict) or type(util.get("available")) is not bool:
                rep.fail("config ack without boolean utilization.available")
                continue
            rep.utilization = util["available"]
            if rep.utilization and (
                    util.get("source") != UTIL_SOURCE
                    or util.get("confidence") != UTIL_CONFIDENCE):
                # provenance gate: a known-bad capability is not proven
                rep.fail("utilization capability with wrong source/confidence")
                rep.utilization = False
            tx = data.get("tx_dropped")
            if type(tx) is int and tx > 0:
                rep.tx_dropped = max(rep.tx_dropped, tx)
            rep.notes.append(
                f"epoch {epoch} spectrum={spectrum} util={rep.utilization}"
                + (f" eff={data['spectrum_effective']}" if spectrum else ""))
            continue

        if event == "channel":
            if not rep.acked:
                continue                # pre-ack data is never counted
            if data.get("epoch") != epoch:
                rep.fail(f"channel from old epoch {data.get('epoch')!r}")
                continue
            if data.get("band") != request.band:
                rep.fail(f"channel from band {data.get('band')!r}, "
                         f"requested {request.band}")
                continue
            ch = data.get("ch")
            if type(ch) is not int:
                rep.fail("channel event without integer ch")
                continue
            cid = data.get("cycle")
            if type(cid) is not int:
                rep.fail("channel event without integer cycle")
                continue
            if marker_open is not None and cid < marker_open:
                continue          # closed cycle: stale, never counted
            if ch not in rep.channels:
                rep.fail(f"channel {ch} not advertised in the ack")
            if "util" in data:
                # strict contract check; absence is a legal gap, but a
                # present-and-wrong sample (source/provenance/range/bools)
                # is rejected - never clamped, never counted
                if parse_channel_util(data.get("util")) is None:
                    rep.fail(f"ch {ch}: invalid util sample (source/"
                             "confidence/counts/ranges, B<=A, ints only)")
                else:
                    rep.util_events += 1   # actual valid sample event
            open_cycle(cid)       # a channel STATUS alone is NOT RF coverage
            if first_reported is None:
                first_reported = ch
            dropped = data.get("ap_dropped")
            if type(dropped) is int and dropped > 0:
                rep.ap_dropped += dropped
        elif event == "channel_error":
            if not rep.acked:
                continue
            if data.get("epoch") != epoch:
                rep.fail(f"channel_error from old epoch "
                         f"{data.get('epoch')!r}")
                continue
            if data.get("band") != request.band:
                rep.fail(f"channel_error from band {data.get('band')!r}, "
                         f"requested {request.band}")
                continue
            ch = data.get("ch")
            if type(ch) is not int:
                rep.fail("channel_error without integer ch")
                continue
            cid = data.get("cycle")
            if type(cid) is not int:
                rep.fail("channel_error without integer cycle")
                continue
            if marker_open is not None and cid < marker_open:
                continue          # closed cycle: stale, never counted
            open_cycle(cid)       # explicit gap of THIS cycle, not RF data
            ch_errors.add(ch)
            if first_reported is None:
                first_reported = ch
            rep.gaps.append(f"ch {ch}: {data.get('code')}")
        elif event == "cycle":
            if not rep.acked:
                continue
            if data.get("epoch") != epoch:
                rep.fail(f"cycle from old epoch {data.get('epoch')!r}")
                continue
            if data.get("band") != request.band:
                rep.fail(f"cycle from band {data.get('band')!r}, "
                         f"requested {request.band}")
                continue
            cid = data.get("cycle")
            if type(cid) is not int:
                rep.fail("cycle event without integer cycle id")
                continue
            if last_cycle is not None and cid <= last_cycle:
                rep.fail(f"non-monotonic cycle id {cid} after {last_cycle}")
                continue
            # RF coverage is separate from channel STATUS: a cycle is only
            # covered when every advertised channel has a validated 0x04
            # frame or an explicit channel_error gap - never by STATUS
            # alone.  Hard failure except for a legitimate first cycle
            # joined mid-pass (cold-join / partial first cycle).
            missing = sorted(set(rep.channels) - rf_seen - ch_errors)
            if missing:
                joined = (rep.cycles == 0 and
                          (first_reported is None
                           or first_reported != rep.channels[0]))
                if joined:
                    rep.notes.append(
                        f"cycle {cid} joined mid-flight (first-cycle "
                        f"coverage not required): missing RF {missing}")
                else:
                    rep.fail(f"cycle {cid} missing RF for channels {missing}")
            covered_cycle = None   # scopes belong to one cycle id only
            rf_seen.clear()
            ch_errors.clear()
            first_reported = None
            last_cycle = cid
            marker_open = cid + 1        # frames of this cycle are over
            rep.cycles += 1
        elif event == "error":
            rep.notes.append(f"device error: {data.get('code')}")
        else:
            rep.fail(f"unknown monitor event {event!r}")

        continue

    return rep


def finalize(rep: Report, *, tlv_errors: int, ack_errors: int,
             min_cycles: int, clean_stream: bool = True) -> Report:
    if not rep.acked:
        rep.fail("no config acknowledgement matching the requested fields")
    else:
        if not rep.spectrum:
            # never silently pass: RF acceptance needs the capability
            rep.fail("BLOCKED: device does not advertise RF capability "
                     "(spectrum:false)")
        if rep.frames == 0:
            rep.fail("no 0x04 RF frames received (zero RF)")
        if rep.util_events:
            rep.notes.append(f"util_samples={rep.util_events}")
        if rep.utilization and rep.util_events == 0:
            # capability advertised but no valid sample ever arrived:
            # absence is a gap, never fake coverage - the acceptance gate
            # demands actual valid sample events under the current contract
            rep.fail("no valid utilization samples received (zero util)")
        if rep.tx_dropped:
            rep.notes.append(f"tx_dropped={rep.tx_dropped}")
        if rep.ap_dropped:
            rep.notes.append(f"ap_dropped={rep.ap_dropped}")
        for gap in rep.gaps:
            rep.notes.append(f"gap {gap}")
    if clean_stream and tlv_errors - ack_errors > 0:
        rep.fail(f"{tlv_errors - ack_errors} parser errors after the ack")
    if rep.acked and rep.cycles < min_cycles:
        rep.fail(f"only {rep.cycles} completed cycles, need {min_cycles}")
    return rep


# ---------------------------------------------------------------- port run
# Resend of the (idempotent) CONFIG while no matching ack has arrived:
# bounded by MAX_RESENDS and by the collection window; the final
# "no config acknowledgement" failure stays the visible outcome.
RESEND_INTERVAL_S = 0.5
MAX_RESENDS = 10


def run_port(port: str, baud: int, request: ConfigRequest, seconds: float,
             min_cycles: int) -> Report:
    import serial

    parser = tlv.TlvParser()
    events: list[tuple[float, object]] = []
    ack_errors = -1
    acked = False
    sends = 1
    cfg = tlv.encode_config(*request.as_tuple())
    t0 = time.monotonic()
    last_send = t0
    with serial.Serial(port, baud, timeout=0.05) as ser:
        ser.write(cfg)
        while time.monotonic() - t0 < seconds:
            now = time.monotonic()
            if (not acked and sends <= MAX_RESENDS
                    and now - last_send >= RESEND_INTERVAL_S):
                # no matching ack yet: resend the same idempotent tuple,
                # bounded by MAX_RESENDS and the collection window; the
                # finalize failure stays visible (a device losing its first
                # CONFIG is the covered class, cause not proven)
                ser.write(cfg)
                sends += 1
                last_send = now
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                continue
            for msg in parser.feed(chunk):
                if ack_errors < 0 and isinstance(msg, tlv.Status) \
                        and msg.data.get("schema") == SCHEMA \
                        and msg.data.get("event") == "config" \
                        and _config_fields(msg) == request.as_tuple():
                    ack_errors = parser.errors   # baseline before sync
                    acked = True                 # stop resending
                events.append((time.monotonic() - t0, msg))
    if ack_errors < 0:
        ack_errors = 0
    rep = evaluate(request, events)
    return finalize(rep, tlv_errors=parser.errors, ack_errors=ack_errors,
                    min_cycles=min_cycles)


# ---------------------------------------------------------------- fixtures
FIXTURE_CHANNELS = {0: [1, 6, 11], 1: [36, 40, 44]}


def _cfg_bytes(request: ConfigRequest, epoch: int, **over) -> bytes:
    d = {"schema": SCHEMA, "event": "config", "fw": "rf-smoke-fixture",
         "idf": "v6.0.3", "chip": "ESP32-C5", "country": "JP",
         "epoch": epoch, "dwell_ms": 120,
         "channels": list(FIXTURE_CHANNELS[request.band]),
         "tx_dropped": 0,
         "spectrum": False, "utilization": {"available": False,
                                            "blocker": "unproven"},
         **request.__dict__}
    d.update(over)
    return tlv.encode_status_json(d)


RF_CAPS = {"source": "c5_snapshot_iq_fft", "fft_sizes": [64, 128, 256],
           "rate_codes": [{"code": 0, "span_khz": 20000},
                          {"code": 1, "span_khz": 40000}],
           "bin_unit": "centi_dbfs"}


def _rf_ack(request: ConfigRequest, epoch: int = 1, *,
            util: dict | None = None) -> bytes:
    rate_code = 0 if request.sample_rate_khz == 20000 else 1
    over = {} if util is None else {"utilization": util}
    return _cfg_bytes(request, epoch, spectrum=True,
                      spectrum_caps=RF_CAPS,
                      spectrum_effective={"fft_size": request.fft_size,
                                          "rate_code": rate_code,
                                          "span_khz": request.sample_rate_khz},
                      **over)


def _rf_frame(request: ConfigRequest, epoch: int = 1, cycle: int = 1,
              ch: int = 6, *, band: int | None = None,
              mode: int | None = None, rate_code: int | None = None,
              source: int = 0, center_khz: int | None = None,
              power: list | None = None) -> bytes:
    b = request.band if band is None else band
    centre = (int(channel_freq(b, ch) * 1000)
              if center_khz is None else center_khz)
    # defaults derive from the request + capability table, never a constant
    code = (rate_code if rate_code is not None
            else (0 if request.sample_rate_khz == 20000 else 1))
    return tlv.encode_spectrum_rf(
        epoch, cycle, b, ch,
        request.mode if mode is None else mode, code, source,
        centre, request.sample_rate_khz,
        power if power is not None else [-40.0] * request.fft_size)


def _ch_event(request: ConfigRequest, ch: int, epoch: int = 1,
              cycle: int = 1, util: dict | None = None) -> bytes:
    d = {"schema": SCHEMA, "event": "channel", "epoch": epoch,
         "cycle": cycle, "band": request.band, "ch": ch,
         "observed_ms": 120, "packets": 12, "peak_rssi_dbm": -55,
         "aps": [], "ap_dropped": 0}
    if util is not None:
        d["util"] = util
    return tlv.encode_status_json(d)


def _ch_err_event(request: ConfigRequest, ch: int, epoch: int = 1,
                  cycle: int = 1) -> bytes:
    return tlv.encode_status_json(
        {"schema": SCHEMA, "event": "channel_error", "epoch": epoch,
         "cycle": cycle, "band": request.band, "ch": ch,
         "code": "spectrum_capture"})


def _cycle_content(request: ConfigRequest, epoch: int = 1, cycle: int = 1,
                   frames: bool = True, errors: tuple[int, ...] = (),
                   util: dict | None = None) -> list:
    """Channel reports (and optional 0x04 frames) for every advertised
    channel, WITHOUT the closing cycle marker."""
    out = []
    for ch in FIXTURE_CHANNELS[request.band]:
        if ch in errors:
            out.append(_ch_err_event(request, ch, epoch, cycle))
            continue
        out.append(_ch_event(request, ch, epoch, cycle, util))
        if frames:
            out.append(_rf_frame(request, epoch, cycle, ch))
    return out


def _full_cycle(request: ConfigRequest, epoch: int = 1, cycle: int = 1,
                frames: bool = True, errors: tuple[int, ...] = (),
                util: dict | None = None) -> list:
    """Channel reports (and optional 0x04 frames) for every advertised
    channel, then the cycle marker."""
    return _cycle_content(request, epoch, cycle, frames, errors, util) + [
        _cycle(epoch, cycle, request.band)]


def _util_sample(busy: int, total: int = 65536, samples: int = 3,
                 attempted: int = 4, window: int = 819,
                 **over) -> dict:
    """Contract-legal pooled util sample (over: fault injection)."""
    d = {"source": UTIL_SOURCE, "confidence": UTIL_CONFIDENCE,
         "busy": busy, "total": total, "samples": samples,
         "attempted": attempted, "window_us_upper": window}
    d.update(over)
    return d


def _util_cap_ack(request: ConfigRequest, epoch: int = 1) -> bytes:
    """RF ack advertising the proven Sampled PHY CCA capability."""
    return _rf_ack(request, epoch,
                   util={"available": True, "source": UTIL_SOURCE,
                         "confidence": UTIL_CONFIDENCE})


def _cycle(epoch: int = 1, cycle: int = 1, band: int = 0) -> bytes:
    return tlv.encode_status_json(
        {"schema": SCHEMA, "event": "cycle", "epoch": epoch, "cycle": cycle,
         "band": band, "elapsed_ms": 1500, "uptime_ms": 1500 * cycle})


def _parse(*chunks: bytes) -> tuple[list, int]:
    parser = tlv.TlvParser()
    out: list = []
    for chunk in chunks:
        out.extend(parser.feed(chunk))
    return out, parser.errors


def self_test_cases() -> list[tuple[str, list[bytes], ConfigRequest, bool,
                                   str | None]]:
    """(name, stream, request, expect_ok, required_failure_substring)."""
    live = ConfigRequest(0, 0, 1000, 64, 20000)
    sweep = ConfigRequest(1, 1, 1000, 128, 40000)

    good_live = ([_rf_ack(live)]
                 + _full_cycle(live, cycle=1)
                 + _full_cycle(live, cycle=2))
    good_sweep = ([_rf_ack(sweep)]
                  + _full_cycle(sweep, cycle=1)
                  + _full_cycle(sweep, cycle=2))

    corrupted = bytearray(_rf_frame(live, ch=6))
    corrupted[10] ^= 0xFF                    # payload bit flip -> CRC fail

    return [
        ("good-live", good_live, live, True, None),
        ("good-sweep-5ghz", good_sweep, sweep, True, None),
        ("good-split-byte-at-a-time",
         [_rf_ack(live)] + _full_cycle(live), live, True, None),
        ("channel-error-legal-gap-not-missing",
         # ch11 has an explicit channel_error: a reported gap, while the
         # other two channels carry real RF frames - cycle stays covered
         [_rf_ack(live),
          _ch_event(live, 1), _rf_frame(live, ch=1),
          _ch_event(live, 6), _rf_frame(live, ch=6),
          _ch_err_event(live, 11),
          _cycle()]
         + _full_cycle(live, cycle=2), live, True, None),
        ("midcycle-heartbeat-keeps-rf-coverage",
         # identical heartbeat (same epoch) mid-cycle2 must not reset
         # cycle coverage / first-channel / marker state
         [_rf_ack(live)]
         + _full_cycle(live, cycle=1)
         + _cycle_content(live, cycle=2)      # content, marker withheld
         + [_rf_ack(live), _cycle(cycle=2)], live, True, None),
        ("coldjoin-partial-first-cycle-ok",
         # joined mid-pass: first reported channel is NOT advertised[0],
         # so first-cycle coverage is legitimately partial
         [_rf_ack(live), _ch_event(live, 6), _rf_frame(live, ch=6),
          _cycle()]
         + _full_cycle(live, cycle=2), live, True, None),
        ("preack-events-ignored",
         [_ch_event(live, 1), _cycle(),           # old data: never counted
          _rf_ack(live)]
         + _full_cycle(live, cycle=1), live, True, None),
        ("preack-rf-ignored-until-matching-ack",
         # cold-join: frames queued before the matching ack are ignored,
         # never counted, never failed
         [_rf_frame(live, ch=6), _rf_ack(live)]
         + _full_cycle(live), live, True, None),
        ("stale-epoch-frame",
         [_rf_ack(live, epoch=1), _rf_frame(live, epoch=1, ch=6),
          _rf_ack(live, epoch=2), _rf_frame(live, epoch=1, cycle=1),
          _cycle(epoch=2), _cycle(epoch=2, cycle=2)], live, False,
         "from epoch 1"),
        ("wrong-band-frame",
         [_rf_ack(live), _rf_frame(live, band=1, ch=6), _cycle()],
         live, False, "from band 1"),
        ("frame-fft-not-effective",
         [_rf_ack(live),
          _rf_frame(live, ch=6, power=[-40.0] * 32), _cycle()], live, False,
         "!= effective"),
        ("frame-rate-code-not-in-caps",
         [_rf_ack(live), _rf_frame(live, ch=6, rate_code=1), _cycle()],
         live, False, "does not match its caps entry"),
        ("frame-mode-mismatch",
         [_rf_ack(live), _rf_frame(live, ch=6, mode=1), _cycle()],
         live, False, "!= configured mode"),
        ("frame-channel-unadvertised",
         [_rf_ack(live), _rf_frame(live, ch=9), _cycle()], live, False,
         "not in advertised"),
        ("frame-centre-mismatch",
         [_rf_ack(live),
          _rf_frame(live, ch=6, center_khz=2412000), _cycle()], live, False,
         "!= channel centre"),
        ("missing-channel-second-cycle-fails",
         # cycle2: every channel STATUS is present, but RF frames exist
         # only for ch1 - channel STATUS alone never covers RF
         [_rf_ack(live)] + _full_cycle(live, cycle=1)
         + [_ch_event(live, 1, cycle=2), _rf_frame(live, cycle=2, ch=1),
            _ch_event(live, 6, cycle=2), _ch_event(live, 11, cycle=2),
            _cycle(cycle=2)], live, False,
         "missing RF for channels [6, 11]"),
        ("channel-status-without-rf-fails",
         # all channel STATUS present, zero RF frames of the cycle
         [_rf_ack(live),
          _ch_event(live, 1), _ch_event(live, 6), _ch_event(live, 11),
          _cycle()], live, False,
         "missing RF for channels [1, 6, 11]"),
        ("lost-marker-new-cycle-rf-coverage-not-mixed",
         # cycle1 marker lost: cycle2 coverage starts fresh, cycle1's RF
         # frames must not leak into it
         [_rf_ack(live)] + _cycle_content(live)        # marker1 lost
         + [_ch_event(live, 1, cycle=2),
            _rf_frame(live, cycle=2, ch=1),
            _cycle(cycle=2)], live, False,
         "missing RF for channels [6, 11]"),
        ("corrupt-crc-frame",
         [_rf_ack(live), bytes(corrupted), _rf_frame(live, ch=6),
          _cycle()], live, False, "parser errors after the ack"),
        ("unknown-source-frame",
         [_rf_ack(live), _rf_frame(live, ch=6, source=7),
          _rf_frame(live, ch=6), _cycle()], live, False,
         "parser errors after the ack"),
        ("frame-after-cycle-marker",
         [_rf_ack(live), _rf_frame(live, ch=6), _cycle(),
          _rf_frame(live, ch=6), _cycle(cycle=2)], live, False,
         "after marker 1"),
        ("rf-while-spectrum-false-fails",
         # acked with spectrum:false: RF frames are contract violations,
         # not cold-join noise
         [_cfg_bytes(live, 1), _rf_frame(live, ch=6), _cycle()], live, False,
         "0x04 frame while spectrum:false"),
        ("no-rf-capability-blocked",
         [_cfg_bytes(live, 1)]
         + _full_cycle(live, cycle=1, frames=False)
         + _full_cycle(live, cycle=2, frames=False), live, False,
         "BLOCKED: device does not advertise RF capability"),
        ("zero-rf-frames-never-pass",
         [_rf_ack(live)]
         + _full_cycle(live, cycle=1, frames=False)
         + _full_cycle(live, cycle=2, frames=False), live, False,
         "zero RF"),
        ("util-capability-wrong-source-fails",
         [_rf_ack(live, util={"available": True, "source": "wrong_source",
                              "confidence": UTIL_CONFIDENCE})]
         + _full_cycle(live), live, False,
         "wrong source/confidence"),
        ("util-samples-counted-zero-busy-accepted",
         # busy=0 is a MEASURED zero: valid and counted, never fake
         [_util_cap_ack(live)]
         + _full_cycle(live, util=_util_sample(0)), live, True, None),
        ("util-capability-zero-samples-fails",
         # capability advertised, every sample absent: gap, not coverage
         [_util_cap_ack(live)] + _full_cycle(live), live, False,
         "no valid utilization samples"),
        ("util-sample-wrong-source-fails",
         [_util_cap_ack(live)]
         + _full_cycle(live, util=_util_sample(100, source="wrong")),
         live, False, "invalid util sample"),
        ("util-sample-bool-busy-fails",
         # bools are not ints
         [_util_cap_ack(live)]
         + _full_cycle(live, util={**_util_sample(100), "busy": True}),
         live, False, "invalid util sample"),
        ("util-sample-busy-over-total-fails",
         # B = A+1 endpoint: invalid, never clamped
         [_util_cap_ack(live)]
         + _full_cycle(live, util=_util_sample(65537)), live, False,
         "invalid util sample"),
        ("util-pooled-samples-accepted",
         [_util_cap_ack(live)]
         + _full_cycle(live, util=_util_sample(100, 40000)), live,
         True, None),
        ("util-sample-count-out-of-range-fails",
         [_util_cap_ack(live)]
         + _full_cycle(live, util=_util_sample(100, samples=9)), live,
         False, "invalid util sample"),
        ("util-attempted-below-samples-fails",
         [_util_cap_ack(live)]
         + _full_cycle(live, util=_util_sample(100, attempted=2)), live,
         False, "invalid util sample"),
        ("util-window-sum-over-budget-fails",
         [_util_cap_ack(live)]
         + _full_cycle(live, util=_util_sample(100, window=15001)),
         live, False, "invalid util sample"),
        ("util-total-over-mask-sum-fails",
         [_util_cap_ack(live)]
         + _full_cycle(live,
                       util=_util_sample(100,
                                          total=3 * 0x07FFFFFF + 1)),
         live, False, "invalid util sample"),
        ("caps-inconsistent-with-effective",
         [_cfg_bytes(live, 1, spectrum=True, spectrum_caps=RF_CAPS,
                     spectrum_effective={"fft_size": 1024,
                                         "rate_code": 0,
                                         "span_khz": 20000})],
         live, False, "invalid RF capabilities"),
        ("utilization-while-unavailable",
         [_rf_ack(live), tlv.encode_ch_util(0, {6: 40}),
          _rf_frame(live, ch=6), _cycle()], live, False,
         "utilization is unavailable"),
    ]


def run_self_test() -> bool:
    all_ok = True
    for name, stream, request, expect_ok, substring in self_test_cases():
        if name == "good-split-byte-at-a-time":
            flat = b"".join(stream)
            messages, errors = _parse(*(bytes([b]) for b in flat))
        else:
            messages, errors = _parse(*stream)
        events = [(float(i), m) for i, m in enumerate(messages)]
        rep = evaluate(request, events)
        rep = finalize(rep, tlv_errors=errors, ack_errors=0,
                       min_cycles=1 if expect_ok else 0)
        # problems = harness verdicts only; rep.failures are EXPECTED for
        # the negative fixtures
        problems: list[str] = []
        if expect_ok and not rep.ok:
            problems.append("expected PASS but failed: "
                            + "; ".join(rep.failures))
        if not expect_ok:
            if rep.ok:
                problems.append("expected FAIL but passed")
            elif substring and not any(substring in f
                                       for f in rep.failures):
                problems.append(
                    f"expected failure containing {substring!r} "
                    f"in {rep.failures!r}")
        ok_here = not problems
        status = "PASS" if ok_here else "FAIL"
        if not ok_here:
            all_ok = False
        detail = "" if ok_here else " :: " + "; ".join(problems)
        print(f"[{status}] {name}{detail}")
    print("[ok] rf smoke self-test" if all_ok
          else "[fail] rf smoke self-test")
    return all_ok


# ----------------------------------------------------------------- CLI
def _cli_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", help="serial device (POSIX PTY or COM port)")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--mode", choices=["live", "sweep"], default="live")
    ap.add_argument("--band", type=int, choices=[0, 1], default=0)
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--min-cycles", type=int, default=2)
    ap.add_argument("--self-test", action="store_true",
                    help="run SYNTHETIC fixtures (no hardware)")
    return ap


def build_request_args(argv: list[str] | None) -> ConfigRequest:
    """CLI flags -> CONFIG request (shared with the tests)."""
    ap = _cli_parser()
    args = ap.parse_args(argv)
    if not args.port:
        ap.error("--port is required unless --self-test is given")
    return ConfigRequest(0 if args.mode == "live" else 1, args.band,
                         1000, 64, 20000)


def main(argv: list[str] | None = None) -> int:
    ap = _cli_parser()
    args = ap.parse_args(argv)

    if args.self_test:
        return 0 if run_self_test() else 1
    if not args.port:
        ap.error("--port is required unless --self-test is given")
    request = build_request_args(argv)
    print(f"[run] {args.port} @ {args.baud} request={request.as_tuple()} "
          f"for {args.seconds}s")
    rep = run_port(args.port, args.baud, request, args.seconds,
                   args.min_cycles)
    for note in rep.notes:
        print(f"[note] {note}")
    if rep.ok:
        print(f"[ok] frames={rep.frames} cycles={rep.cycles} "
              f"epoch={rep.epoch}")
        return 0
    for failure in rep.failures:
        print(f"[fail] {failure}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
