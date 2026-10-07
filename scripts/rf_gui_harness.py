#!/usr/bin/env python3
"""Physical GUI harness: drive the REAL MainWindow + SerialReader through
RF acceptance steps and capture screenshots with assertions.

Modes
-----
``--self-test``  POSIX PTY + a SYNTHETIC device (fixtures only, no hardware;
                 owner verification). The synthetic spectra are clearly
                 labelled in the report - they are NOT measured RF.
``--port PATH``  physical device (FW worker's acceptance run). Same steps;
                 waits become tolerant of real timing, and a capability
                 timeout is reported as BLOCKED, not passed.

Steps cover: Demo baseline, RF capability ack, frame rendering into the
shared plots/sidebar, display toggles, FFT/rate acquisition controls,
sweep staging per STATUS cycle marker, finite sweep count, band switch,
pause/resume, disconnect/reconnect, demo restore, and a proper close.

Exit codes: 0 all steps passed, 1 assertion failed, 2 blocked (e.g. no RF
capability on hardware yet).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import threading
import time

import numpy as np
from PySide6.QtWidgets import QApplication

from wifi_spectrum import tlv
from wifi_spectrum.bands import BANDS, channel_freq
from wifi_spectrum.main_window import DISCONNECTED_TEXT, MainWindow
from wifi_spectrum.monitor_data import UTIL_CONFIDENCE, UTIL_SOURCE

APP = QApplication.instance() or QApplication([])

SYNTHETIC_CHANNELS = {0: [1, 3, 6, 9, 11, 13],
                      1: [36, 40, 44, 100, 149, 153]}
SYNTH_FFT_SIZES = [64, 128, 256]
# True only for --self-test (PTY + SYNTHETIC device): fixture-specific
# expectations (measured-zero cell, positive cell, intentional gap channel)
# are REGRESSION coverage for the synthetic path and must NEVER run
# against a real device on --port.
SYNTHETIC_RUN = False
SYNTH_RATES = [{"code": 0, "span_khz": 20000},
               {"code": 1, "span_khz": 40000}]


class HarnessError(RuntimeError):
    pass


class Blocked(RuntimeError):
    pass


def wait(cond, timeout: float = 8.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if cond():
            return
        time.sleep(0.01)
    if not cond():
        raise HarnessError(f"timeout waiting for {what}")


def check(cond: object, msg: str) -> None:
    if not cond:
        raise HarnessError(msg)


def header_text(win: MainWindow, col: int) -> str:
    item = win.ch_table.horizontalHeaderItem(col)
    if item is None:
        raise HarnessError(f"missing header col {col}")
    return item.text()


def check_util_column(win: MainWindow, synthetic: bool) -> None:
    """Real utilization acceptance (physical --port AND PTY): contract
    facts only for the ambient path - nonempty valid raw samples, the
    exact 100*busy/total mapping into cells/bar heights, pooled-window
    identification plus raw valid/attempted metadata in the tooltip, no
    percent without raw data, the honest UNAVAILABLE state (a legitimate all-zero
    real snapshot passes). synthetic=True (PTY/unit) additionally runs
    the fixture-property regression checks at the end."""
    channels = BANDS[win.band].channels
    cells = [cell_text(win, r, 2) for r in range(win.ch_table.rowCount())]
    if not win.monitor_state.utilization_available:
        check("UNAVAILABLE" in win.util_card.cap_lbl.text(),
              "without a proven capability the util card must read "
              "UNAVAILABLE")
        check(not any(c.endswith("%") for c in cells),
              "no percent may render while utilization is unavailable")
        check(not win._util,
              "no util values may persist while unavailable")
        print("[util] unavailable: no samples published")
        return
    raw = win.monitor_state.util_samples
    check(bool(raw) and bool(win._util),
          "capability proven but ZERO valid util samples - missing actual "
          "util data must fail")
    check(len(win._util) <= len(channels), "util map must stay bounded")
    expected_h = [win._util.get(ch, 0) for ch in channels]
    heights = list(getattr(win.util_bars, "opts", {}).get("height") or [])
    check(len(heights) == len(expected_h),
          "util bar series must match the channel list")
    check(all(abs(float(a) - float(b)) < 1e-6
              for a, b in zip(heights, expected_h, strict=True)),
          "bar heights must equal the raw 100*busy/total values")
    for r, ch in enumerate(channels):
        item = win.ch_table.item(r, 2)
        text = item.text() if item is not None else ""
        if ch in win._util:
            sample = raw.get(ch)
            if sample is None:
                raise HarnessError(
                    f"ch {ch}: rendered without a raw sample")
            check(win._util[ch]
                  == 100.0 * sample["busy"] / sample["total"],
                  f"ch {ch}: display value must equal 100*busy/total")
            check(text == f"{win._util[ch]:.0f} %",
                  f"ch {ch}: cell must show the rounded percent, "
                  f"got {text!r}")
            tip = item.toolTip() if item is not None else ""
            check(f"busy {sample['busy']}" in tip
                  and f"total {sample['total']}" in tip
                  and f"window_us_upper {sample['window_us_upper']}" in tip,
                  f"ch {ch}: tooltip must expose raw busy/total/window")
            check("not the dwell span" in tip,
                  f"ch {ch}: tooltip must identify the pooled window")
            check(f"{sample['samples']} valid of "
                  f"{sample['attempted']} attempted" in tip,
                  f"ch {ch}: tooltip must show valid/attempted counts")
        else:
            check(text in ("—", ""),
                  f"ch {ch}: absent raw sample must render no percent "
                  f"(got {text!r})")
    card_tip = win.util_card.toolTip() or ""
    check("Sampled PHY CCA (experimental)" in card_tip,
          "util tooltip must label Sampled PHY CCA (experimental)")
    check("not the dwell span" in card_tip,
          "card tooltip must identify pooled (not dwell) windows")
    windows = [s["window_us_upper"] for s in raw.values()]
    fractions = [100.0 * s["busy"] / s["total"] for s in raw.values()]
    print(f"[util] n={len(raw)} "
          f"window_us=[{min(windows)}..{max(windows)}] "
          f"raw busy/total=[{min(s['busy'] for s in raw.values())}.."
          f"{max(s['total'] for s in raw.values())}] "
          f"fraction=[{min(fractions):.3f}..{max(fractions):.3f}]%")
    if synthetic:
        # SYNTHETIC-fixture properties only (PTY/unit): regression
        # coverage, never an ambient/physical requirement. The snapshot
        # may land mid-cycle (e.g. right after a band switch): pump Qt
        # until the fixture's positive sample has been published.
        wait(lambda: any(v > 0 for v in win._util.values()), 5.0,
             "synthetic positive util sample to settle")
        cells = [cell_text(win, r, 2)
                 for r in range(win.ch_table.rowCount())]
        check(any(c == "0 %" for c in cells),
              "synthetic: measured busy=0 must render as 0 %")
        check(any(c.endswith(" %") and c != "0 %" for c in cells),
              "synthetic: a positive sample must render a positive %")
        check(SYNTHETIC_CHANNELS[win.band][-1] not in win._util,
              "synthetic: the intentional gap channel must carry no value")


def cell_text(win: MainWindow, row: int, col: int) -> str:
    item = win.ch_table.item(row, col)
    if item is None:
        raise HarnessError(f"missing cell ({row}, {col})")
    return item.text()


def config_from_frame(blob: bytes) -> dict:
    for msg in tlv.TlvParser().feed(blob):
        if isinstance(msg, dict):
            return msg
    raise HarnessError("no CONFIG frame in write")


def shot(win: MainWindow, path) -> None:
    APP.processEvents()
    time.sleep(0.05)
    APP.processEvents()
    if not win.grab().save(str(path)):
        raise HarnessError(f"screenshot failed: {path}")
    print(f"[shot] {path}")


# ------------------------------------------------------- synthetic device
class SyntheticDevice(threading.Thread):
    """SYNTHETIC RF device on a PTY master fd: parses CONFIG, acks with
    capability JSON, then paces 0x04 frames per channel plus STATUS cycle
    markers. Not a measurement source - fixtures only."""

    def __init__(self, master: int) -> None:
        super().__init__(daemon=True, name="synthetic-rf")
        self.master = master
        self.parser = tlv.TlvParser()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.epoch = 0
        self.cycle = 0
        self.cfg: dict | None = None
        self._next_step = 0.0
        self._step_idx = 0

    def stop(self) -> None:
        self._stop.set()

    # -- output ---------------------------------------------------------
    def _send(self, blob: bytes) -> None:
        with contextlib.suppress(OSError):
            os.write(self.master, blob)

    def _ack(self, cfg: dict) -> bytes:
        eff_fft = min(cfg["fft_size"], SYNTH_FFT_SIZES[-1])
        rate = min(SYNTH_RATES,
                   key=lambda r: abs(r["span_khz"] - cfg["sample_rate_khz"]))
        band = cfg["band"]
        return tlv.encode_status_json({
            "schema": "wifi-monitor/1", "event": "config",
            "fw": "synthetic-rf-0.1", "idf": "v6.0.3",
            "chip": "ESP32-C5", "country": "JP", "epoch": self.epoch,
            "dwell_ms": 120, "channels": SYNTHETIC_CHANNELS[band],
            "tx_dropped": 0, "mode": cfg["mode"], "band": band,
            "sweep_ms": cfg["sweep_ms"], "fft_size": cfg["fft_size"],
            "sample_rate_khz": cfg["sample_rate_khz"],
            "spectrum": True,
            "spectrum_caps": {"source": "c5_snapshot_iq_fft",
                              "fft_sizes": SYNTH_FFT_SIZES,
                              "rate_codes": SYNTH_RATES,
                              "bin_unit": "centi_dbfs"},
            "spectrum_effective": {"fft_size": eff_fft,
                                   "rate_code": rate["code"],
                                   "span_khz": rate["span_khz"]},
            "utilization": {"available": True,
                            "source": UTIL_SOURCE,
                            "confidence": UTIL_CONFIDENCE},
        })

    def _frame(self, ch: int) -> bytes:
        cfg = self.cfg
        assert cfg is not None
        rate = min(SYNTH_RATES,
                   key=lambda r: abs(r["span_khz"] - cfg["sample_rate_khz"]))
        n = min(cfg["fft_size"], SYNTH_FFT_SIZES[-1])
        center_khz = int(channel_freq(cfg["band"], ch) * 1000)
        span = rate["span_khz"]
        # SYNTHETIC tone at the channel centre + flat floor (fixture, not RF)
        power = [-68.0] * n
        for k in (-1, 0, 1):
            power[n // 2 + k] = -25.0
        return tlv.encode_spectrum_rf(
            self.epoch, self.cycle, cfg["band"], ch, cfg["mode"],
            rate["code"], 0, center_khz, span, power)

    def _channel_event(self, ch: int) -> bytes:
        cfg = self.cfg
        assert cfg is not None
        payload = {
            "schema": "wifi-monitor/1", "event": "channel",
            "epoch": self.epoch, "cycle": self.cycle, "band": cfg["band"],
            "ch": ch, "observed_ms": 120, "packets": 12,
            "peak_rssi_dbm": -55, "aps": [], "ap_dropped": 0}
        channels = SYNTHETIC_CHANNELS[cfg["band"]]
        if ch != channels[-1]:
            # Sampled PHY CCA fixture: valid raw sample except the last
            # channel (an intentional GAP - a 100%-valid subset is never
            # required). busy=0 on the first channel is a measured zero;
            # total stays inside the 27-bit contract.
            total = 65536
            busy = (0 if ch == channels[0]
                    else (ch * 997 + self.cycle * 31) % (total + 1))
            payload["util"] = {"source": UTIL_SOURCE,
                               "confidence": UTIL_CONFIDENCE,
                               "busy": busy, "total": total,
                               "samples": 3, "attempted": 4,
                               "window_us_upper": 2400}
        return tlv.encode_status_json(payload)

    def _cycle_marker(self) -> bytes:
        cfg = self.cfg
        assert cfg is not None
        return tlv.encode_status_json({
            "schema": "wifi-monitor/1", "event": "cycle",
            "epoch": self.epoch, "cycle": self.cycle, "band": cfg["band"],
            "elapsed_ms": 900, "uptime_ms": 900 * self.cycle})

    # -- input / loop ---------------------------------------------------
    def _on_config(self, cfg: dict) -> bytes:
        """Apply one CONFIG per the contract (docs/wifi-monitor.md:42):
        an identical request is acknowledged WITHOUT changing the epoch or
        restarting the current cycle; only an actual change advances the
        epoch and abandons the incomplete pass."""
        with self._lock:
            if cfg != self.cfg:
                self.epoch += 1
                self.cycle += 1
                self.cfg = cfg
                self._step_idx = 0
                self._next_step = time.monotonic() + 0.05
        return self._ack(cfg)

    def run(self) -> None:
        # non-blocking: pacing must keep running between CONFIG bursts
        os.set_blocking(self.master, False)
        while not self._stop.is_set():
            chunk = b""
            with contextlib.suppress(OSError):
                chunk = os.read(self.master, 4096)
            for msg in self.parser.feed(chunk):
                if isinstance(msg, dict):          # CONFIG from the GUI
                    self._send(self._on_config(msg))
            now = time.monotonic()
            with self._lock:
                cfg, due = self.cfg, self._next_step
                if cfg is None or now < due:
                    continue
                channels = SYNTHETIC_CHANNELS[cfg["band"]]
                if self._step_idx < len(channels):
                    ch = channels[self._step_idx]
                    blob = (self._channel_event(ch) + self._frame(ch))
                    self._step_idx += 1
                    self._next_step = now + 0.02
                else:
                    blob = self._cycle_marker()
                    self.cycle += 1
                    self._step_idx = 0
                    self._next_step = now + 0.10
            self._send(blob)
            time.sleep(0.005)


# ---------------------------------------------------------------- steps
def finite_frac(a: np.ndarray) -> float:
    return float(np.isfinite(a).mean())


def step_demo_baseline(win: MainWindow, shots) -> None:
    win.demo_btn.setChecked(True)
    wait(lambda: np.isfinite(win.cur).any(), 8.0, "demo frames")
    check(not np.isnan(win.cur).any(), "demo must show its floor background")
    check(win.spec_card.cap_lbl.text() == "POWER · dBm",
          "demo units must be dBm")
    check(header_text(win, 3) == "Peak",
          "demo table header must be plain Peak")
    print("[ok] demo baseline renders in dBm")
    shot(win, shots / "01-demo.png") if shots else None
    win.demo_btn.setChecked(False)


def step_connect(win: MainWindow) -> None:
    win.connect_btn.setChecked(True)
    check(win.connect_btn.isChecked(), "connect must engage")
    print(f"[ok] connect requested for "
          f"{win.port_combo.currentText() or '<no port>'}")


def step_rf_ready(win: MainWindow, shots, timeout: float) -> None:
    try:
        wait(lambda: win.monitor_state.rf_ready, timeout,
             "RF capability ack (hardware pending?)")
    except HarnessError as exc:
        raise Blocked(str(exc)) from exc
    # ALL expectations derive from the RECEIVED capability ack (real FW
    # advertises fft sizes through 1024) - never from fixture constants.
    caps = win.monitor_state.spectrum_caps or {}
    eff = win.monitor_state.spectrum_effective or {}
    sizes = caps.get("fft_sizes", [])
    rates = caps.get("rate_codes", [])
    check(sizes, "ack must advertise non-empty fft_sizes")
    check(rates, "ack must advertise non-empty rate_codes")
    items = [win.fft_combo.itemText(i)
             for i in range(win.fft_combo.count())]
    check(items == [str(s) for s in sizes],
          f"FFT items must mirror received caps {sizes}, got {items}")
    rate_items = [f"{int(r['span_khz']) // 1000} MS/s" for r in rates]
    got_rates = [win.sr_combo.itemText(i)
                 for i in range(win.sr_combo.count())]
    check(got_rates == rate_items,
          f"rate items from received caps {rate_items}, got {got_rates}")
    # explicit effective-vs-requested consistency against the caps table
    check(eff.get("fft_size") in sizes,
          f"effective fft {eff.get('fft_size')} must be in caps {sizes}")
    entries = [r for r in rates if r.get("code") == eff.get("rate_code")]
    check(len(entries) == 1
          and entries[0]["span_khz"] == eff.get("span_khz"),
          f"effective rate {eff} must match its caps entry by code")
    check(win.fft_combo.currentText() == str(eff.get("fft_size")),
          "combo starts on the effective fft")
    check(win.sr_combo.currentText()
          == f"{int(eff.get('span_khz', 0)) // 1000} MS/s",
          "rate combo starts on the effective span")
    check(win.rbw_lbl.text()
          == f"{eff['span_khz'] / eff['fft_size']:.1f} kHz",
          "RBW derives from effective span/fft")
    check(win.spec_card.cap_lbl.text() == "POWER · dBFS",
          "RF units must be dBFS")
    check(win.spec_plot.getAxis("left").labelText.strip() == "dBFS",
          "axis label must be dBFS")
    check(header_text(win, 3) == "Peak dBFS",
          "sidebar header must say Peak dBFS")
    check(win.db_max.value() == 0,
          "RF source switch must default the axis ymax to 0 dBFS")
    if win.monitor_state.utilization_available:
        check("UNAVAILABLE" not in win.util_card.cap_lbl.text(),
              "proven utilization must not read UNAVAILABLE")
    else:
        check("UNAVAILABLE" in win.util_card.cap_lbl.text(),
              "utilization must stay unavailable until proven")
    check(win.peak_chk.isEnabled() and win.wf_chk.isEnabled(),
          "display controls stay enabled in RF mode")
    print(f"[ok] RF capability acked ({len(sizes)} fft sizes, "
          f"RBW={win.rbw_lbl.text()})")
    shot(win, shots / "02-rf-ready.png") if shots else None


def waterfall_rows(win: MainWindow) -> int:
    return int(sum(1 for row in win.wf if np.isfinite(row).any()))


def warmup_waterfall(win: MainWindow, rows: int = 30) -> None:
    """Wait for >=`rows` NEW waterfall rows with Qt pumping, prove the
    history shifted down and that serial bytes drove the rows, then
    render so the screenshot shows real history (never synthetic fill)."""
    arrived: list[int] = []
    if win.source is not None:
        win.source.stats.connect(lambda rx, errors: arrived.append(rx))
    wait(lambda: np.isfinite(win.wf[0]).any(), 8.0, "first waterfall row")
    snapshot = win.wf[0].copy()
    base = waterfall_rows(win)
    bytes_before = len(arrived)
    target = min(base + rows, len(win.wf))
    wait(lambda: waterfall_rows(win) >= target, 25.0,
         f"{rows} new waterfall rows")
    check(len(arrived) > bytes_before,
          "warmup rows must be driven by serial bytes")
    check(any(np.array_equal(row, snapshot, equal_nan=True)
              for row in win.wf[1:]),
          "waterfall history must shift down as rows arrive")
    arrived_now = np.isfinite(win.cur).any()
    check(arrived_now, "frames must arrive before render")
    if arrived_now:                         # render only on proven arrival
        win._render()
    print(f"[ok] waterfall warmup: {waterfall_rows(win)} rows "
          f"(>= {rows} new), shifted, serial-driven")


def step_frame_renders(win: MainWindow, shots) -> None:
    wait(lambda: np.isfinite(win.cur).any(), 8.0, "0x04 frames")
    arrived = np.isfinite(win.cur).any()
    check(arrived, "0x04 frames must arrive before render")  # wait raises
    if arrived:
        win._render()
    check(finite_frac(win.cur) > 0.05, "cur must cover >5% of the band")
    check(np.isfinite(win.peak).any(), "peak hold must accumulate")
    check(np.isfinite(win.wf[0]).any(), "waterfall must receive a row")
    peak_cells = [cell_text(win, r, 3)
                  for r in range(win.ch_table.rowCount())]
    check(any(c not in ("—", "-", "") for c in peak_cells),
          "sidebar peak column must show measured dBFS values")
    if not win.monitor_state.utilization_available:
        check(all(cell_text(win, r, 2) == "—"
                  for r in range(win.ch_table.rowCount())),
              "utilization column stays '—' without a proven source")
    print(f"[ok] frames rendered (cur {finite_frac(win.cur):.0%} finite)")
    warmup_waterfall(win, 30)               # >=30 real rows before the shot
    check_util_column(win, SYNTHETIC_RUN)
    shot(win, shots / "03-rf-live.png") if shots else None


def step_display_controls(win: MainWindow, shots) -> None:
    win.peak_chk.setChecked(False)
    check(not win.peak_curve.isVisible(), "peak toggle must hide curve")
    win.peak_chk.setChecked(True)
    check(win.peak_curve.isVisible(), "peak toggle must restore curve")
    win.wf_chk.setChecked(False)
    check(not win.wf_card.isVisible(), "waterfall toggle must hide card")
    win.wf_chk.setChecked(True)
    win.db_min.setValue(-90)
    check("-90 dBFS" in win.db_min_lbl.text(), "dB range label follows unit")
    win._zoom_to_channel(5, 0)
    lo, hi = win.spec_plot.getViewBox().viewRange()[0]
    check(hi - lo < 150, "channel zoom must narrow the span")
    win._reset_x()
    win.ch_chk.setChecked(False)
    check(not win.util_card.isVisible(), "channel markers toggle")
    win.ch_chk.setChecked(True)
    win._reset_peak()
    check(np.isnan(win.peak).all(),
          "reset peak on the real path must clear to gaps")
    win._render()
    print("[ok] peak/waterfall/range/zoom/markers/reset all respond")
    shot(win, shots / "04-controls.png") if shots else None
    # new frames rebuild peak after reset
    wait(lambda: np.isfinite(win.peak).any(), 6.0, "peak rebuild")


def step_fft_rate_requests(win: MainWindow, shots, writes) -> None:
    caps = win.monitor_state.spectrum_caps or {}
    sizes = caps.get("fft_sizes", [])          # ints, the authority
    size_strs = [str(s) for s in sizes]        # combo texts
    rates = caps.get("rate_codes", [])
    check(sizes and rates, "capabilities required before requesting")
    # request only values the RECEIVED caps advertise
    current_fft = win.fft_combo.currentText()
    target_fft = next((s for s in size_strs if s != current_fft), None)
    if target_fft is None:
        print("[note] caps offer a single FFT size; request step skipped")
    else:
        base = len(writes)
        win.fft_combo.setCurrentText(target_fft)
        wait(lambda: len(writes) > base, 5.0, "CONFIG after FFT change")
        last = config_from_frame(writes[-1])
        check(last["fft_size"] == int(target_fft),
              f"CONFIG carries fft {target_fft}, got {last}")
    rate_items = [f"{int(r['span_khz']) // 1000} MS/s" for r in rates]
    current_rate = win.sr_combo.currentText()
    target_rate = next((r for r in rate_items if r != current_rate), None)
    if target_rate is None:
        print("[note] caps offer a single rate; request step skipped")
    else:
        base = len(writes)
        win.sr_combo.setCurrentText(target_rate)
        wait(lambda: len(writes) > base, 5.0, "CONFIG after rate change")
        last = config_from_frame(writes[-1])
        check(last["sample_rate_khz"] == int(target_rate.split()[0]) * 1000,
              f"CONFIG carries span for {target_rate}, got {last}")
    # the effective ack must match BOTH the explicit request and the caps
    want_fft = int(target_fft or current_fft)
    want_span = int((target_rate or current_rate).split()[0]) * 1000

    def effective_matches() -> bool:
        e = win.monitor_state.spectrum_effective
        if not e:
            return False
        entries = [r for r in (win.monitor_state.spectrum_caps or {})
                   .get("rate_codes", [])
                   if r.get("code") == e.get("rate_code")]
        return (e.get("fft_size") == want_fft
                and e.get("span_khz") == want_span
                and len(entries) == 1
                and entries[0]["span_khz"] == want_span
                and e.get("fft_size") in sizes)

    wait(effective_matches, 8.0,
         "effective ack matching request and caps entry")
    eff = win.monitor_state.spectrum_effective or {}
    check(win.rbw_lbl.text()
          == f"{eff['span_khz'] / eff['fft_size']:.1f} kHz",
          "RBW follows the effective ack, not the request")
    print(f"[ok] requests honored: fft {want_fft} span {want_span} "
          f"(RBW {win.rbw_lbl.text()})")


def step_sweep_cycle_and_count(win: MainWindow, shots) -> None:
    epoch_before = win.monitor_state.epoch
    win.sweep_count.setValue(2)
    win.mode_tabs.setCurrentIndex(1)              # Band Sweep
    wait(lambda: win.monitor_state.ready, 8.0, "sweep re-ack")
    check(epoch_before is None or win.monitor_state.epoch != epoch_before,
          "an applied CONFIG change must start a new epoch")
    check(win.sweep_count.isEnabled(), "count control enabled in sweep")
    wait(lambda: win.sweeps_done >= 1, 8.0, "first sweep cycle")
    wait(lambda: np.isfinite(win.cur).any(), 8.0, "sweep flush renders")
    win._render()
    print("[ok] sweep renders only after STATUS cycle marker")
    wait(lambda: win.sweeps_done >= 2, 10.0, "second sweep cycle")
    wait(lambda: not win.play_btn.isChecked(), 3.0,
         "finite sweep count must stop play")
    check(win.sweep_lbl.text().startswith("2 /"),
          f"sweep label must read 2 / 2, got {win.sweep_lbl.text()}")
    print("[ok] finite sweep count stops play at 2 / 2")
    check_util_column(win, SYNTHETIC_RUN)   # sweep util flush at marker
    shot(win, shots / "05-sweep.png") if shots else None


def step_band5g(win: MainWindow, shots) -> None:
    win.play_btn.setChecked(True)
    win.mode_tabs.setCurrentIndex(0)              # back to Live for speed
    win.band_seg.setCurrentIndex(1)               # 5 GHz
    wait(lambda: win.monitor_state.ready and win.monitor_state.band == 1,
         8.0, "5 GHz re-ack")
    wait(lambda: np.isfinite(win.cur).any(), 8.0, "5 GHz frames")
    finite = win.freqs[np.isfinite(win.cur)]
    check(finite.size, "5 GHz must render measured points")
    check(bool(finite.min() >= BANDS[1].f_start - 1),
          "5 GHz data must live in the 5 GHz range")
    check(win.band == 1 and win.ch_table.rowCount()
          == len(BANDS[1].channels), "5 GHz sidebar rebuilt")
    print(f"[ok] 5 GHz renders ({finite.size} grid points)")
    warmup_waterfall(win, 30)               # >=30 real rows before the shot
    check_util_column(win, SYNTHETIC_RUN)
    shot(win, shots / "06-rf-5g.png") if shots else None


def step_pause_resume(win: MainWindow) -> None:
    arrived: list[int] = []
    if win.source is not None:
        win.source.stats.connect(lambda rx, errors: arrived.append(rx))
    win.play_btn.setChecked(False)
    # prove bytes keep ARRIVING while paused - a silent sleep would hide
    # a render bug; we pump real Qt events throughout the window
    wait(lambda: len(arrived) >= 3, 8.0,
         "device bytes arriving while paused")
    frozen = win.cur.copy()
    at_freeze = len(arrived)
    end = time.monotonic() + 0.5
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.01)
    win._render()
    check(len(arrived) > at_freeze,
          "frames must keep ARRIVING during the paused window")
    check(np.array_equal(frozen, win.cur, equal_nan=True),
          "pause must freeze the display despite arriving frames")
    win.play_btn.setChecked(True)
    wait(lambda: not np.array_equal(frozen, win.cur, equal_nan=True),
         8.0, "resume must accept frames again")
    print("[ok] pause froze display while frames arrived; resume accepts")


def step_reconnect(win: MainWindow, shots) -> None:
    epoch_before = win.monitor_state.epoch
    win.connect_btn.setChecked(False)
    wait(lambda: np.isnan(win.cur).all(), 5.0, "disconnect clears plots")
    check(win.dev_lbl.text() == DISCONNECTED_TEXT,
          "disconnect status line")
    check(not win.monitor_state.ready, "monitor state reset on disconnect")
    shot(win, shots / "07-disconnected.png") if shots else None
    win.connect_btn.setChecked(True)
    # A NEW matching ack must arrive after the state clear: rf_ready is
    # only set by accepting a 5-field echo, and disconnect reset the
    # state (checked above). Identical settings legitimately keep the
    # epoch (docs/wifi-monitor.md:42); an epoch is required to change
    # only when the CONFIG itself changed (covered by the sweep/band
    # steps), and it must never regress here.
    wait(lambda: win.monitor_state.rf_ready, 10.0, "reconnect re-ack")
    epoch_after = win.monitor_state.epoch
    if epoch_after is None:
        raise HarnessError("fresh matching CONFIG ack after reopen")
    if epoch_before is not None and epoch_after != epoch_before:
        check(epoch_after > epoch_before,
              f"epoch must never regress "
              f"({epoch_before} -> {epoch_after})")
    wait(lambda: np.isfinite(win.cur).any(), 8.0, "reconnect renders")
    check(win.db_max.value() == 0,
          "ymax default must survive the reconnect source switch")
    check(win.db_min.value() == -90,
          "manually chosen range must survive source re-switching")
    print(f"[ok] disconnect clears, reconnect re-renders "
          f"(epoch {epoch_before} -> {epoch_after}; "
          f"identical CONFIG may keep the epoch)")


def step_demo_restore(win: MainWindow, shots) -> None:
    win.connect_btn.setChecked(False)
    wait(lambda: np.isnan(win.cur).all(), 5.0, "second disconnect")
    win.demo_btn.setChecked(True)
    wait(lambda: np.isfinite(win.cur).any(), 8.0, "demo frames again")
    check(not np.isnan(win.cur).any(),
          "demo restore must leave no NaN (stale RF) behind")
    check(bool(np.isfinite(win.peak).all()),
          "demo peak must be finite floor-based values")
    check(win.spec_card.cap_lbl.text() == "POWER · dBm",
          "units must return to dBm")
    check(header_text(win, 3) == "Peak",
          "sidebar header returns to Peak")
    check(win.fft_combo.isEnabled(), "demo acquisition controls on")
    print("[ok] demo restore: dBm labels, floor background, no NaN")
    shot(win, shots / "08-demo-restore.png") if shots else None
    win.demo_btn.setChecked(False)


# ----------------------------------------------------------------- main
def run_harness(port: str, baud: int, shots_dir: str | None,
                ready_timeout: float) -> int:
    from pathlib import Path

    shots = Path(shots_dir) if shots_dir else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    win = MainWindow()
    win.show()
    APP.processEvents()
    # observe CONFIG writes without touching the port seam
    writes: list[bytes] = []
    port_combo = win.port_combo
    port_combo.setCurrentText(port)
    win.baud_combo.setCurrentText(str(baud))

    # wrap SerialReader writes after connect: patch the class method once
    from wifi_spectrum.serial_link import SerialReader
    original_write = SerialReader.write

    def observing_write(self, data: bytes) -> None:
        if self.port == port:
            writes.append(data)
        original_write(self, data)

    SerialReader.write = observing_write  # type: ignore[method-assign]

    steps = [
        ("demo baseline", lambda: step_demo_baseline(win, shots)),
        ("connect", lambda: step_connect(win)),
        ("rf ready", lambda: step_rf_ready(win, shots, ready_timeout)),
        ("frame rendering", lambda: step_frame_renders(win, shots)),
        ("display controls", lambda: step_display_controls(win, shots)),
        ("fft/rate controls",
         lambda: step_fft_rate_requests(win, shots, writes)),
        ("sweep + count", lambda: step_sweep_cycle_and_count(win, shots)),
        ("5 GHz band", lambda: step_band5g(win, shots)),
        ("pause/resume", lambda: step_pause_resume(win)),
        ("reconnect", lambda: step_reconnect(win, shots)),
        ("demo restore", lambda: step_demo_restore(win, shots)),
    ]
    failed = None
    try:
        for name, fn in steps:
            print(f"[step] {name}")
            fn()
    except Blocked as exc:
        failed = f"BLOCKED: {exc}"
        code = 2
    except HarnessError as exc:
        failed = f"FAIL: {exc}"
        code = 1
    else:
        code = 0
    finally:
        SerialReader.write = original_write  # type: ignore[method-assign]
        with contextlib.suppress(Exception):
            win.close()
        APP.processEvents()
    if failed:
        print(f"[{failed}]")
        print("NOTE: synthetic/physical run ended with unresolved step; "
              "parent's physical acceptance is still required."
              if code == 2 else "")
        return code
    print("[ok] all harness steps passed "
          f"({'SYNTHETIC PTY device' if port.startswith('/dev/pts') or 'pty' in port else 'physical device'})")
    print("[ok] harness closed MainWindow cleanly")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test", action="store_true",
                      help="PTY + SYNTHETIC device (no hardware)")
    mode.add_argument("--port", help="physical serial device (FW worker)")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--shots", help="directory for PNG screenshots")
    ap.add_argument("--ready-timeout", type=float, default=10.0,
                    help="seconds to wait for the RF capability ack")
    args = ap.parse_args(argv)
    global SYNTHETIC_RUN
    SYNTHETIC_RUN = bool(args.self_test)   # fixture checks: PTY only

    slave = None
    device = None
    master = None
    try:
        if args.self_test:
            if os.name != "posix":
                print("--self-test needs a POSIX PTY", file=sys.stderr)
                return 2
            import pty
            import tty

            master, slave_fd = pty.openpty()
            tty.setraw(slave_fd)
            slave = os.ttyname(slave_fd)
            os.close(slave_fd)
            device = SyntheticDevice(master)
            device.start()
            print("[run] SYNTHETIC device on PTY - fixtures, NOT measured "
                  "RF; screenshots are synthetic evidence")
            return run_harness(slave, args.baud, args.shots, 8.0)
        print(f"[run] physical device {args.port} - parent's physical "
              "acceptance remains required")
        return run_harness(args.port, args.baud, args.shots,
                           args.ready_timeout)
    finally:
        if device:
            device.stop()
        if master is not None:
            with contextlib.suppress(OSError):
                os.close(master)


if __name__ == "__main__":
    sys.exit(main())
