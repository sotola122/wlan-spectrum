"""Shared-plot RF rendering vs Demo, driven through actual dispatch paths.

Every measurement enters ``MainWindow`` the same way hardware bytes do:
encoded with the real CRC framing, parsed by ``TlvParser``, dispatched by
``SerialReader._dispatch`` into the shared Demo layout (spectrum card,
waterfall, utilization chart, sidebar). RF frames are 0x04 SPECTRUM_RF with
dBFS units; Demo keeps dBm. The PTY test exercises the real serial read
thread end to end (POSIX only). Synthetic fixtures, never device captures.
"""

from __future__ import annotations

import contextlib
import os
import unittest
from unittest import mock

import numpy as np
from PySide6.QtWidgets import QApplication

from wifi_spectrum import main_window, tlv
from wifi_spectrum.bands import BANDS
from wifi_spectrum.main_window import (
    DISCONNECTED_TEXT,
    WAITING_TEXT,
    MainWindow,
)
from wifi_spectrum.mock import MockSource
from wifi_spectrum.monitor_data import UTIL_CONFIDENCE, UTIL_SOURCE
from wifi_spectrum.serial_link import SerialReader

APP = QApplication.instance() or QApplication([])

CFG_DEFAULTS = {"mode": 0, "band": 0, "sweep_ms": 1000, "fft_size": 64,
                "sample_rate_khz": 20000}
SCHEMA = "wifi-monitor/1"

RF_CAPS = {"source": "c5_snapshot_iq_fft", "fft_sizes": [64, 128, 256],
           "rate_codes": [{"code": 0, "span_khz": 20000},
                          {"code": 1, "span_khz": 40000}],
           "bin_unit": "centi_dbfs"}
RF_EFF = {"fft_size": 128, "rate_code": 0, "span_khz": 20000}


def spin_until(condition, timeout_s: float = 3.0) -> bool:
    """Process Qt events until condition() or the fixed deadline."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        APP.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return bool(condition())


def config_event(epoch: int = 1, **over) -> dict:
    d = {"schema": SCHEMA, "event": "config", "epoch": epoch,
         "dwell_ms": 120, "channels": list(range(1, 14)), "tx_dropped": 0,
         "spectrum": False, "cca": False, "fft_supported": False,
         **CFG_DEFAULTS}
    d.update(over)
    return d


def rf_config_event(epoch: int = 1, **over) -> dict:
    base = {"spectrum": True, "spectrum_caps": dict(RF_CAPS),
            "spectrum_effective": dict(RF_EFF),
            "utilization": {"available": False,
                            "blocker": "cca_semantics_unproven"}}
    base.update(over)
    return config_event(epoch=epoch, **base)


def channel_event(epoch: int = 1, cycle: int = 1, ch: int = 6,
                  packets: int = 24, peak: int | None = -48,
                  observed_ms: int = 120, band: int = 0, **over) -> dict:
    d = {"schema": SCHEMA, "event": "channel", "epoch": epoch,
         "cycle": cycle, "band": band, "ch": ch, "observed_ms": observed_ms,
         "packets": packets, "peak_rssi_dbm": peak, "aps": [],
         "ap_dropped": 0}
    d.update(over)
    return d


def cycle_event(epoch: int = 1, cycle: int = 1, band: int = 0, **over) -> dict:
    d = {"schema": SCHEMA, "event": "cycle", "epoch": epoch, "cycle": cycle,
         "band": band, "elapsed_ms": 1600, "uptime_ms": 1600 * cycle}
    d.update(over)
    return d


def rf_blob(epoch: int = 1, cycle: int = 1, band: int = 0, ch: int = 6,
            mode: int = 0, center_khz: int = 2437000, span_khz: int = 20000,
            power: list | None = None) -> bytes:
    """One synthetic 0x04 frame (SYNTHETIC fixture, not a device capture),
    coherent with RF_EFF: rate_code 0, span 20000, effective bin count."""
    n = int(RF_EFF["fft_size"])
    if power is None:
        values = [-30.0] * n
    elif len(power) == n:
        values = [float(v) for v in power]
    else:                       # flat fixture level -> effective bins
        values = [float(power[0])] * n
    return tlv.encode_spectrum_rf(epoch, cycle, band, ch, mode, 0, 0,
                                  center_khz, span_khz, values)


def config_from_frame(blob: bytes) -> dict:
    for msg in tlv.TlvParser().feed(blob):
        if isinstance(msg, dict):
            return msg
    raise AssertionError("no CONFIG frame in write")


def cell_text(table, row: int, col: int) -> str:
    item = table.item(row, col)
    if item is None:
        raise AssertionError(f"missing cell ({row}, {col})")
    return item.text()


def header_text(table, col: int) -> str:
    item = table.horizontalHeaderItem(col)
    if item is None:
        raise AssertionError(f"missing header ({col})")
    return item.text()


def row_of(win: MainWindow, channel: int) -> int:
    return BANDS[win.band].channels.index(channel)


class FixtureReader(SerialReader):
    """Test double at the port seam only: writes are captured and recorded
    device bytes are pushed through the real parser + ``_dispatch``."""

    def __init__(self) -> None:
        super().__init__("<fixture>", 921600)
        self.writes: list[bytes] = []
        self._parser = tlv.TlvParser()

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def stop(self) -> None:
        self._stop.set()

    def deliver(self, *blobs: bytes) -> None:
        for blob in blobs:
            for msg in self._parser.feed(blob):
                self._dispatch(msg)


class SharedLayoutSourceTests(unittest.TestCase):
    """One plot layout for every source; capability state replaces the
    old stack-index view switch."""

    def setUp(self) -> None:
        self.win = MainWindow()
        self.win.show()
        APP.processEvents()

    def tearDown(self) -> None:
        self.win.close()
        APP.processEvents()

    def test_serial_attach_waits_with_acquisition_disabled(self) -> None:
        fx = FixtureReader()
        self.win._attach(fx)
        self.assertEqual(self.win._source_kind, "serial")
        self.assertTrue(self.win._monitor_view())
        self.assertEqual(self.win.dev_lbl.text(), WAITING_TEXT)
        # acquisition controls honestly off for monitor-only firmware
        self.assertFalse(self.win.fft_combo.isEnabled())
        self.assertFalse(self.win.sr_combo.isEnabled())
        self.assertEqual(self.win.rbw_lbl.text(), "N/A — no RF FFT")
        # display controls work ALWAYS, in every source state
        for w in (self.win.peak_chk, self.win.wf_chk,
                  self.win.peak_reset_btn, self.win.channel_group):
            self.assertTrue(w.isEnabled())
        self.assertTrue(self.win.channel_group.isVisible())
        # real Live still allows editing the hopping interval
        self.assertTrue(self.win.sweep_time.isEnabled())
        fx.opened.emit()
        self.assertEqual(len(fx.writes), 1)
        self.assertEqual(config_from_frame(fx.writes[0]), CFG_DEFAULTS)
        # real path carries no fake floor before any measurement
        self.assertTrue(np.isnan(self.win.cur).all())

    def test_demo_attach_restores_original_demo_state(self) -> None:
        fx = FixtureReader()
        self.win._attach(fx)
        self.win._detach()
        src = MockSource(self.win)
        self.win._attach(src)               # no start(): deterministic
        self.assertEqual(self.win._source_kind, "demo")
        self.assertTrue(self.win.fft_combo.isEnabled())
        self.assertEqual([self.win.fft_combo.itemText(i)
                          for i in range(self.win.fft_combo.count())],
                         ["64", "128", "256", "512", "1024"])
        self.assertNotEqual(self.win.rbw_lbl.text(), "N/A — no RF FFT")
        self.assertFalse(self.win.sweep_time.isEnabled())  # demo Live
        self.assertEqual(self.win.spec_card.cap_lbl.text(), "POWER · dBm")
        self.assertEqual(header_text(self.win.ch_table, 3), "Peak")
        self.assertFalse(np.isnan(self.win.cur).any())     # floor, no NaN
        self.assertTrue((self.win.peak == -200.0).all())
        self.win._detach()

    def test_baseline_monitor_status_keeps_acquisition_off(self) -> None:
        fx = FixtureReader()
        self.win._attach(fx)
        self.win.play_btn.setChecked(True)
        fx.opened.emit()
        fx.deliver(tlv.encode_status_json(config_event(**CFG_DEFAULTS)))
        self.assertTrue(self.win.monitor_state.ready)
        self.assertFalse(self.win._rf_active)
        self.assertFalse(self.win.fft_combo.isEnabled())
        self.assertIn("spectrum n/a", self.win.dev_lbl.text())

    def test_legacy_spectrum_falls_back_without_new_screen(self) -> None:
        fx = FixtureReader()
        self.win._attach(fx)
        self.win.play_btn.setChecked(True)
        fx.deliver(tlv.encode_spectrum(2400.0, 0.5, [-50.0, -60.0]))
        self.assertTrue(self.win._legacy)
        self.assertFalse(self.win._rf_active)
        self.assertTrue(self.win.fft_combo.isEnabled())   # demo-like again
        self.assertNotEqual(self.win.rbw_lbl.text(), "N/A — no RF FFT")
        self.assertIn("legacy spectrum frames",
                      self.win.statusBar().currentMessage())
        self.assertFalse(np.isnan(self.win.cur).any())

    def test_unsolicited_ch_util_never_causes_legacy_fallback(self) -> None:
        fx = FixtureReader()
        self.win._attach(fx)
        self.win.play_btn.setChecked(True)
        fx.opened.emit()
        fx.deliver(tlv.encode_status_json(config_event(**CFG_DEFAULTS)))
        self.assertTrue(self.win.monitor_state.ready)
        # a monitor-only status device emitting 0x02 proves nothing:
        # no fallback, no percent, acquisition stays honestly off
        fx.deliver(tlv.encode_ch_util(0, {6: 40}))
        self.assertFalse(self.win._legacy)
        self.assertTrue(self.win._monitor_view())
        self.assertFalse(self.win.fft_combo.isEnabled())
        self.assertEqual(self.win.rbw_lbl.text(), "N/A — no RF FFT")
        self.assertEqual(cell_text(self.win.ch_table,
                                   row_of(self.win, 6), 2), "—")


class RfRenderingTests(unittest.TestCase):
    """Real 0x04 frames drive the shared Demo widgets with dBFS honesty."""

    def setUp(self) -> None:
        self.win = MainWindow()
        self.win.show()
        APP.processEvents()
        self.fx = FixtureReader()
        self.win._attach(self.fx)
        self.win.play_btn.setChecked(True)
        self.fx.opened.emit()

    def tearDown(self) -> None:
        self.win.close()
        APP.processEvents()

    def ack(self, **over) -> dict:
        fields = config_from_frame(self.fx.writes[-1])
        event = rf_config_event(**fields, **over)
        self.fx.deliver(tlv.encode_status_json(event))
        self.assertTrue(self.win.monitor_state.rf_ready,
                        "RF capability ack not accepted")
        return fields

    def deliver(self, *events: dict) -> None:
        self.fx.deliver(*(tlv.encode_status_json(e) for e in events))

    def val_at(self, freq_mhz: float, arr=None) -> float:
        a = self.win.cur if arr is None else arr
        idx = int(np.argmin(np.abs(self.win.freqs - freq_mhz)))
        return float(a[idx])

    # ------------------------------------------------------------ wiring
    def test_rf_ack_binds_caps_units_and_controls(self) -> None:
        self.ack()
        self.assertTrue(self.win._rf_active)
        self.assertEqual(self.win.spec_card.cap_lbl.text(), "POWER · dBFS")
        self.assertEqual(
            self.win.spec_plot.getAxis("left").labelText.strip(), "dBFS")
        self.assertEqual(header_text(self.win.ch_table, 3), "Peak dBFS")
        self.assertTrue(self.win.db_min_lbl.text().endswith("dBFS"))
        # acquisition controls come from runtime caps, effective selection
        self.assertTrue(self.win.fft_combo.isEnabled())
        self.assertEqual([self.win.fft_combo.itemText(i)
                          for i in range(self.win.fft_combo.count())],
                         ["64", "128", "256"])
        self.assertEqual(self.win.fft_combo.currentText(), "128")
        self.assertEqual([self.win.sr_combo.itemText(i)
                          for i in range(self.win.sr_combo.count())],
                         ["20 MS/s", "40 MS/s"])
        self.assertEqual(self.win.rbw_lbl.text(), "156.2 kHz")  # 20000/128
        # utilization stays honestly unavailable
        self.assertIn("UNAVAILABLE", self.win.util_card.cap_lbl.text())
        self.assertIn("util n/a", self.win.dev_lbl.text())
        self.assertIn("RF FFT 128", self.win.dev_lbl.text())
        for row in range(self.win.ch_table.rowCount()):
            self.assertEqual(cell_text(self.win.ch_table, row, 2), "—")
        # display controls still work in RF mode
        for w in (self.win.peak_chk, self.win.wf_chk,
                  self.win.peak_reset_btn):
            self.assertTrue(w.isEnabled())

    def test_frame_renders_cur_peak_waterfall_and_sidebar(self) -> None:
        self.ack()
        blob = rf_blob(ch=6, power=[-30.0] * 8)
        self.fx.deliver(blob)
        frame = next(m for m in tlv.TlvParser().feed(blob)
                     if isinstance(m, tlv.SpectrumRf))
        lo, hi = float(frame.freqs[0]), float(frame.freqs[-1])
        # covered grid = the frame's own bin range: measured values,
        # exact dBFS; every other grid point stays a gap - no fake floor,
        # no zero-fill
        covered = np.array([lo <= f <= hi for f in self.win.freqs])
        self.assertTrue(np.isfinite(self.win.cur[covered]).all())
        self.assertTrue(np.isnan(self.win.cur[~covered]).all())
        self.assertAlmostEqual(self.val_at(2427.0), -30.0, places=3)
        self.assertAlmostEqual(self.val_at(2437.0), -30.0, places=3)
        self.assertTrue(np.isnan(self.win.cur[np.abs(
            self.win.freqs - 2470.0) < 0.1]).all())
        # peak: NaN-safe max-hold over observed points only
        self.assertAlmostEqual(self.val_at(2437.0, self.win.peak), -30.0,
                               places=3)
        self.assertTrue(np.isnan(self.win.peak[np.abs(
            self.win.freqs - 2470.0) < 0.1]).all())
        # waterfall: one live row, gaps transparent in the row too
        self.assertAlmostEqual(
            self.val_at(2437.0, self.win.wf[0]), -30.0, places=3)
        self.assertTrue(np.isnan(self.win.wf[0])[
            int(np.argmin(np.abs(self.win.freqs - 2470.0)))])
        self.assertTrue(np.isnan(self.win.wf[1]).all())  # only one row
        # sidebar peak column: dBFS value at observed channel, gap elsewhere
        self.win._render()
        self.assertEqual(cell_text(self.win.ch_table,
                                   row_of(self.win, 6), 3), "-30")
        self.assertEqual(cell_text(self.win.ch_table,
                                   row_of(self.win, 13), 3), "—")
        self.assertEqual(cell_text(self.win.ch_table,
                                   row_of(self.win, 6), 2), "—")

    def test_stale_epoch_band_and_closed_cycle_frames_dropped(self) -> None:
        self.ack()
        self.fx.deliver(rf_blob(cycle=1, power=[-30.0] * 8))
        before = self.win.cur.copy()
        self.fx.deliver(
            rf_blob(epoch=2, cycle=1, power=[-80.0] * 8),   # stale epoch
            rf_blob(band=1, cycle=1, power=[-80.0] * 8))    # wrong band
        np.testing.assert_equal(self.win.cur, before)
        self.deliver(cycle_event(cycle=1))                  # cycle closed
        self.fx.deliver(rf_blob(cycle=1, power=[-80.0] * 8))  # replay
        np.testing.assert_equal(self.win.cur, before)
        # a genuinely new cycle renders again
        self.fx.deliver(rf_blob(cycle=2, power=[-50.0] * 8))
        self.assertAlmostEqual(self.val_at(2437.0), -50.0, places=3)

    def test_cycle_close_turns_unobserved_regions_into_gaps(self) -> None:
        self.ack()
        self.fx.deliver(rf_blob(cycle=1, center_khz=2437000))  # 2427..2444.5
        self.assertAlmostEqual(self.val_at(2437.0), -30.0, places=3)
        self.deliver(cycle_event(cycle=1))
        # after the marker, only cycle-1 coverage may show
        self.assertAlmostEqual(self.val_at(2437.0), -30.0, places=3)
        self.assertTrue(np.isnan(self.win.cur[np.abs(
            self.win.freqs - 2412.0) < 0.1]).all())
        # cycle 2 observes a different channel only
        self.fx.deliver(rf_blob(cycle=2, center_khz=2412000))  # 2402..2419.5
        self.assertAlmostEqual(self.val_at(2412.0), -30.0, places=3)
        self.deliver(cycle_event(cycle=2))
        # cycle-1 region became a gap; cycle-2 region stays measured
        self.assertTrue(np.isnan(self.win.cur[np.abs(
            self.win.freqs - 2437.0) < 0.1]).all())
        self.assertAlmostEqual(self.val_at(2412.0), -30.0, places=3)

    def test_channel_error_without_frame_is_a_gap_not_a_guess(self) -> None:
        self.ack()
        self.deliver(
            {"schema": SCHEMA, "event": "channel_error", "epoch": 1,
             "cycle": 1, "band": 0, "ch": 6,
             "code": "spectrum_capture"},
            cycle_event(cycle=1))                 # capture failed: NO 0x04
        self.assertTrue(np.isnan(self.win.cur).all())
        self.assertIn(6, self.win.monitor_state.unavailable)
        self.assertIn("ch 6: spectrum_capture",
                      self.win.statusBar().currentMessage())

    def test_sweep_stages_frames_until_cycle_marker(self) -> None:
        self.win.mode_tabs.setCurrentIndex(1)          # Band Sweep
        self.ack()                                      # mode echo = 1
        self.fx.deliver(rf_blob(mode=1, power=[-30.0] * 8))
        self.assertTrue(np.isnan(self.win.cur).all())  # staged, not visible
        self.assertEqual(self.win.sweeps_done, 0)
        self.deliver(cycle_event(cycle=1))
        self.assertAlmostEqual(self.val_at(2437.0), -30.0, places=3)
        self.assertEqual(self.win.sweeps_done, 1)
        # exactly ONE waterfall row per completed sweep cycle
        self.assertAlmostEqual(self.val_at(2437.0, self.win.wf[0]), -30.0,
                               places=3)
        self.assertTrue(np.isnan(self.win.wf[1]).all())

    def test_pause_gates_rf_frames_but_not_config_or_error(self) -> None:
        self.ack()
        self.win.play_btn.setChecked(False)            # paused
        self.fx.deliver(rf_blob(power=[-30.0] * 8))
        self.assertTrue(np.isnan(self.win.cur).all())  # gated
        self.deliver(rf_config_event(**CFG_DEFAULTS))  # ack heartbeat
        self.assertTrue(self.win.monitor_state.ready)
        self.deliver({"schema": SCHEMA, "event": "error",
                      "code": "invalid_config"})
        self.assertIn("invalid_config", self.win.statusBar().currentMessage())
        self.win.play_btn.setChecked(True)
        self.fx.deliver(rf_blob(power=[-30.0] * 8))
        self.assertAlmostEqual(self.val_at(2437.0), -30.0, places=3)

    def test_finite_sweep_count_stops_play_via_status_cycles(self) -> None:
        self.win.mode_tabs.setCurrentIndex(1)
        self.win.sweep_count.setValue(2)
        self.ack()
        self.fx.deliver(rf_blob(mode=1, cycle=1))
        self.deliver(cycle_event(cycle=1))
        self.assertEqual(self.win.sweeps_done, 1)
        self.assertTrue(self.win.play_btn.isChecked())
        self.deliver(cycle_event(cycle=1))             # duplicate: ignored
        self.assertEqual(self.win.sweeps_done, 1)
        self.fx.deliver(rf_blob(mode=1, cycle=2))
        self.deliver(cycle_event(cycle=2))
        self.assertEqual(self.win.sweeps_done, 2)
        self.assertFalse(self.win.play_btn.isChecked())  # finite count hit
        self.deliver(cycle_event(cycle=3))             # paused: no count
        self.assertEqual(self.win.sweeps_done, 2)

    def test_band_switch_resets_clears_and_reconfigures(self) -> None:
        self.ack()
        self.fx.deliver(rf_blob(power=[-30.0] * 8))
        self.assertAlmostEqual(self.val_at(2437.0), -30.0, places=3)
        self.win.band_seg.setCurrentIndex(1)           # 5 GHz
        self.assertEqual(config_from_frame(self.fx.writes[-1])["band"], 1)
        self.assertFalse(self.win.monitor_state.ready)
        self.assertFalse(self.win._rf_active)
        self.assertTrue(np.isnan(self.win.cur).all())  # old-band data gone
        self.assertEqual(self.win.dev_lbl.text(), WAITING_TEXT)
        self.assertEqual(self.win.spec_card.cap_lbl.text(), "POWER · dBm")
        fields = config_from_frame(self.fx.writes[-1])
        self.fx.deliver(tlv.encode_status_json(
            rf_config_event(**fields, channels=[36, 40])))
        self.assertTrue(self.win._rf_active)
        self.assertEqual(self.win.spec_card.cap_lbl.text(), "POWER · dBFS")
        self.fx.deliver(rf_blob(band=1, ch=36, center_khz=5180000,
                                power=[-45.0] * 8))
        self.assertAlmostEqual(self.val_at(5175.0), -45.0, places=3)
        self.assertTrue(np.isnan(self.win.cur[np.abs(
            self.win.freqs - 2437.0) < 0.1]).all())   # 2.4G grid untouched
        self.win._render()
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 36),
                                   3), "-45")

    def test_unsolicited_ch_util_never_fabricates_percent(self) -> None:
        self.ack()                       # utilization.available = false
        # RSSI-style channel events must not become a percentage either
        self.deliver(channel_event(ch=6), cycle_event(cycle=1))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "—")
        # unsolicited 0x02 while the capability is unavailable: ignored,
        # and RF mode is never left behind (no legacy fallback)
        self.fx.deliver(tlv.encode_ch_util(0, {6: 40}))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "—")
        self.assertTrue(self.win._rf_active)
        self.assertFalse(self.win._legacy)
        self.assertEqual(self.win.spec_card.cap_lbl.text(), "POWER · dBFS")

    def test_proven_utilization_renders_percent_in_rf_mode(self) -> None:
        self.ack(utilization={"available": True,
                              "source": UTIL_SOURCE,
                              "confidence": UTIL_CONFIDENCE})
        self.assertEqual(self.win.util_card.cap_lbl.text(),
                         "UTILIZATION · %")
        self.assertIn("Sampled PHY CCA (experimental)",
                      self.win.util_card.toolTip())
        # epoch-less 0x02 is REJECTED while RF-active: it can never be
        # this device's actual source and must not overwrite measured util
        self.fx.deliver(tlv.encode_ch_util(0, {6: 40}))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "—")
        # actual util rides the epoch'd STATUS channel envelope
        self.deliver(channel_event(ch=6, util={"source": UTIL_SOURCE,
                                               "confidence": UTIL_CONFIDENCE,
                                               "busy": 4000, "total": 10000,
                                               "samples": 3, "attempted": 4, "window_us_upper": 819}))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "40 %")
        self.assertTrue(self.win._rf_active)          # still RF mode
        self.assertEqual(self.win.spec_card.cap_lbl.text(), "POWER · dBFS")

    def test_channel_util_renders_zero_gaps_and_raw_tooltips(self) -> None:
        self.ack(utilization={"available": True,
                              "source": UTIL_SOURCE,
                              "confidence": UTIL_CONFIDENCE})
        util = {"source": UTIL_SOURCE, "confidence": UTIL_CONFIDENCE,
                "busy": 0, "total": 65536, "samples": 3, "attempted": 4, "window_us_upper": 819}
        # measured zero renders as 0 % - never a gap, never fake coverage
        self.deliver(channel_event(ch=6, util=util))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "0 %")
        item = self.win.ch_table.item(row_of(self.win, 6), 2)
        if item is None:
            self.fail("util cell missing")
        self.assertIn("busy 0", item.toolTip() or "")
        self.assertIn("window_us_upper 819", item.toolTip() or "")
        self.assertIn(6, self.win._util)
        # a later event WITHOUT util = gap: the stale value is dropped
        self.deliver(channel_event(ch=6, cycle=2))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "—")
        self.assertNotIn(6, self.win._util)
        # an invalid sample is a gap too (B = A+1, never clamped)
        self.deliver(channel_event(ch=6, cycle=3,
                                   util={**util, "busy": 65537}))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "—")
        self.assertNotIn(6, self.win._util)
        # a valid non-zero sample renders the derived percent
        self.deliver(channel_event(ch=1, cycle=4,
                                   util={**util, "busy": 6554}))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 1),
                                   2), "10 %")
        # other cells stay gaps - a fully valid subset is not required
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 11),
                                   2), "—")

    def test_sweep_util_publishes_only_at_cycle_marker(self) -> None:
        self.win.mode_tabs.setCurrentIndex(1)
        self.ack(utilization={"available": True,
                              "source": UTIL_SOURCE,
                              "confidence": UTIL_CONFIDENCE})
        util = {"source": UTIL_SOURCE, "confidence": UTIL_CONFIDENCE,
                "busy": 0, "total": 65536, "samples": 3, "attempted": 4, "window_us_upper": 819}
        # sweep stages but does NOT publish before the marker
        self.deliver(channel_event(ch=6, util=util))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "—")
        self.assertNotIn(6, self.win._util)
        # the matching marker flushes the staged sample
        self.deliver(cycle_event(cycle=1))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "0 %")
        self.assertIn(6, self.win._util)

    def test_missing_cycles_and_lost_marker_never_reuse_percent(self) -> None:
        util = {"source": UTIL_SOURCE, "confidence": UTIL_CONFIDENCE,
                "busy": 0, "total": 65536, "samples": 3, "attempted": 4, "window_us_upper": 819}
        self.ack(utilization={"available": True,
                              "source": UTIL_SOURCE,
                              "confidence": UTIL_CONFIDENCE})
        self.deliver(channel_event(ch=6, util=util))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "0 %")
        self.deliver(cycle_event(cycle=1))
        # a marker for a cycle with no channel signal: every value gaps
        self.deliver(cycle_event(cycle=2))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "—")
        self.assertNotIn(6, self.win._util)
        # observed WITHOUT util in a later cycle: gap, not stale
        self.deliver(channel_event(ch=6, cycle=3, util=util))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "0 %")
        self.deliver(channel_event(ch=6, cycle=4))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "—")
        # lost marker (5 never arrives): the first cycle6 signal starts a
        # fresh scope - cycle3's percent can never be reused
        self.deliver(channel_event(ch=1, cycle=6,
                                   util={**util, "busy": 1000}))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "—")
        self.assertNotIn(6, self.win._util)
        self.assertIn(1, self.win._util)

    def test_channel_error_clears_util_before_cycle(self) -> None:
        util = {"source": UTIL_SOURCE, "confidence": UTIL_CONFIDENCE,
                "busy": 0, "total": 65536, "samples": 3, "attempted": 4, "window_us_upper": 819}
        self.ack(utilization={"available": True,
                              "source": UTIL_SOURCE,
                              "confidence": UTIL_CONFIDENCE})
        self.deliver(channel_event(ch=6, util=util))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "0 %")
        # failed dwell: the published percent dies before any marker
        self.deliver({"schema": SCHEMA, "event": "channel_error",
                      "epoch": 1, "cycle": 1, "band": 0, "ch": 6,
                      "code": "spectrum_capture"})
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "—")
        self.assertNotIn(6, self.win._util)

    def test_capability_loss_same_epoch_clears_display(self) -> None:
        util = {"source": UTIL_SOURCE, "confidence": UTIL_CONFIDENCE,
                "busy": 0, "total": 65536, "samples": 3, "attempted": 4, "window_us_upper": 819}
        self.ack(utilization={"available": True,
                              "source": UTIL_SOURCE,
                              "confidence": UTIL_CONFIDENCE})
        self.deliver(channel_event(ch=6, util=util))
        self.assertIn(6, self.win._util)
        # SAME epoch: capability revoked -> display gaps immediately
        self.ack(utilization={"available": False,
                              "blocker": "unproven"})
        self.assertFalse(self.win.monitor_state.utilization_available)
        self.assertEqual(self.win._util, {})
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "—")

    def test_sub_one_percent_keeps_fraction_for_data(self) -> None:
        self.ack(utilization={"available": True,
                              "source": UTIL_SOURCE,
                              "confidence": UTIL_CONFIDENCE})
        self.deliver(channel_event(ch=6, util={"source": UTIL_SOURCE,
                                               "confidence": UTIL_CONFIDENCE,
                                               "busy": 1, "total": 65536,
                                               "samples": 3, "attempted": 4, "window_us_upper": 819}))
        # the raw fraction survives in the data/bars; only the LABEL
        # rounds (display rounding, never data truncation)
        self.assertIn(6, self.win._util)
        self.assertAlmostEqual(self.win._util[6], 100.0 / 65536, places=9)
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "0 %")

    def test_sweep_lost_marker_never_publishes_stale_stage(self) -> None:
        """Exact MainWindow repro: staged cycle1, marker1 LOST, ZERO
        channels of cycle2, marker2 arrives -> stale cycle1 must not
        publish as cycle2 (flush requires exact cycle equality)."""
        cap = {"available": True, "source": UTIL_SOURCE,
               "confidence": UTIL_CONFIDENCE}
        util = {"source": UTIL_SOURCE, "confidence": UTIL_CONFIDENCE,
                "busy": 0, "total": 65536, "samples": 3, "attempted": 4, "window_us_upper": 819}
        self.win.mode_tabs.setCurrentIndex(1)
        self.ack(utilization=cap)
        self.deliver(channel_event(ch=6, cycle=1, util=util))  # staged
        self.assertNotIn(6, self.win._util)
        # marker1 lost: ZERO channels of cycle2, marker2 arrives
        self.deliver(cycle_event(cycle=2))
        self.assertNotIn(6, self.win._util, "stale stage published as c2")
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "—")
        self.assertEqual(self.win.monitor_state.util_samples, {})
        # a later cycle publishes only its OWN exact staged subset
        self.deliver(channel_event(ch=6, cycle=3,
                                   util={**util, "busy": 6554}))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "—")    # sweep: still pre-marker
        self.deliver(cycle_event(cycle=3))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "10 %")

    def test_sweep_atomic_until_marker_and_heartbeat_keeps_snapshot(self) -> None:
        """Sweep published snapshot is atomic: valid cycle0 publishes,
        partial cycle1 and a same-epoch config heartbeat leave it UNTOUCHED
        (no blank/partial leak), marker1 then replaces it with the exact
        staged subset."""
        cap = {"available": True, "source": UTIL_SOURCE,
               "confidence": UTIL_CONFIDENCE}
        base = {"source": UTIL_SOURCE, "confidence": UTIL_CONFIDENCE,
                "busy": 0, "total": 65536, "samples": 3, "attempted": 4, "window_us_upper": 819}
        self.win.mode_tabs.setCurrentIndex(1)
        self.ack(utilization=cap)
        # valid cycle0: two channels + marker0 -> published exact
        self.deliver(channel_event(ch=6, cycle=0, util=base))
        self.deliver(channel_event(ch=1, cycle=0,
                                   util={**base, "busy": 6554}))
        self.deliver(cycle_event(cycle=0))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "0 %")
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 1),
                                   2), "10 %")
        # partial cycle1: only ch6 staged (a DIFFERENT value, pre-marker)
        self.deliver(channel_event(ch=6, cycle=1,
                                   util={**base, "busy": 13107}))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "0 %")     # old snapshot, not 20%
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 1),
                                   2), "10 %")    # no blank/partial leak
        # same-epoch config heartbeat mid-cycle1 must keep the snapshot
        self.ack(utilization=cap)
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "0 %")
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 1),
                                   2), "10 %")
        # marker1 replaces with the EXACT staged subset (ch6 only)
        self.deliver(cycle_event(cycle=1))
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 6),
                                   2), "20 %")    # 13107/65536 = 20%
        self.assertEqual(cell_text(self.win.ch_table, row_of(self.win, 1),
                                   2), "—")       # ch1 not in cycle1

    def test_pooled_tooltip_identifies_counts_and_window(self) -> None:
        cap = {"available": True, "source": UTIL_SOURCE,
               "confidence": UTIL_CONFIDENCE}
        self.ack(utilization=cap)
        # pooled sample: counts + summed window + pooling wording
        self.deliver(channel_event(ch=6, util={
            "source": UTIL_SOURCE, "confidence": UTIL_CONFIDENCE,
            "busy": 0, "total": 40000, "samples": 3,
            "attempted": 4, "window_us_upper": 3000}))
        item = self.win.ch_table.item(row_of(self.win, 6), 2)
        if item is None:
            self.fail("util cell missing")
        tip = item.toolTip() or ""
        self.assertIn("Sampled PHY CCA (experimental)", tip)
        self.assertIn("3 valid of 4 attempted", tip)
        self.assertIn("window_us_upper 3000", tip)
        self.assertIn("not the dwell span", tip)
        card = self.win.util_card.toolTip() or ""
        self.assertIn("Sampled PHY CCA (experimental)", card)
        self.assertIn("not the dwell span", card)
        # actual counts render for every sample; samples=1 is simply one
        # valid measurement (no versioning anywhere)
        self.deliver(channel_event(ch=1, util={
            "source": UTIL_SOURCE, "confidence": UTIL_CONFIDENCE,
            "busy": 6554, "total": 65536, "samples": 3, "attempted": 4, "window_us_upper": 819}))
        item1 = self.win.ch_table.item(row_of(self.win, 1), 2)
        if item1 is None:
            self.fail("util cell missing")
        tip1 = item1.toolTip() or ""
        self.assertIn("window_us_upper 819", tip1)
        self.assertIn("not the dwell span", tip1)
        self.assertIn("3 valid of 4 attempted", tip1)
        self.assertNotIn("version", tip1)

    def test_rf_source_switch_defaults_ymax_to_zero(self) -> None:
        # untouched default: the Demo-era -20 must become 0 dBFS on the
        # transition INTO the RF source - never on Demo, heartbeats or data
        self.assertEqual(self.win.db_max.value(), -20)
        self.ack(utilization={"available": True, "source": UTIL_SOURCE,
                              "confidence": UTIL_CONFIDENCE})
        self.assertEqual(self.win.db_max.value(), 0)
        self.assertIn("0 dBFS", self.win.db_max_lbl.text())
        # repeated data must never auto-range
        self.fx.deliver(rf_blob())
        self.assertEqual(self.win.db_max.value(), 0)
        # untouched: leaving RF restores the Demo default (-20); a manual
        # choice would survive (covered by the manual-range test)
        self.win.demo_btn.setChecked(True)
        self.assertEqual(self.win.db_max.value(), -20)
        self.win.demo_btn.setChecked(False)
        self.assertEqual(self.win.db_max.value(), -20)

    def test_manual_range_survives_source_switches_and_heartbeats(self) -> None:
        cap = {"available": True, "source": UTIL_SOURCE,
               "confidence": UTIL_CONFIDENCE}
        self.ack(utilization=cap)          # entering RF: default 0
        self.win.db_max.setValue(-5)       # manual user choice
        self.assertEqual(self.win.db_max.value(), -5)
        # same-epoch config heartbeat must not reset the range
        self.ack(utilization=cap)
        self.assertEqual(self.win.db_max.value(), -5)
        # repeated data must not reset it
        self.deliver(channel_event(ch=6, util={
            "source": UTIL_SOURCE, "confidence": UTIL_CONFIDENCE,
            "busy": 0, "total": 65536, "samples": 3, "attempted": 4, "window_us_upper": 819}))
        self.assertEqual(self.win.db_max.value(), -5)
        # Demo -> RF re-switch: the manual choice survives
        self.win.demo_btn.setChecked(True)
        self.win.demo_btn.setChecked(False)
        self.assertEqual(self.win.db_max.value(), -5)
        self.win._attach(self.fx)
        self.fx.opened.emit()
        self.ack(utilization=cap)          # re-enter RF
        self.assertEqual(self.win.db_max.value(), -5)

    def test_untouched_transitions_restore_demo_default_ymax(self) -> None:
        cap = {"available": True, "source": UTIL_SOURCE,
               "confidence": UTIL_CONFIDENCE}
        self.ack(utilization=cap)              # RF enter: untouched -> 0
        self.assertEqual(self.win.db_max.value(), 0)
        # actual demo start path (demo toggle -> _detach -> MockSource)
        self.win.demo_btn.setChecked(True)
        self.assertFalse(self.win._rf_active)
        self.assertEqual(self.win.db_max.value(), -20,
                         "untouched RF preset must not persist into Demo")
        self.win.demo_btn.setChecked(False)    # actual _detach path
        self.assertEqual(self.win.db_max.value(), -20)
        # RF again: untouched adopts 0 once more
        self.win._attach(self.fx)
        self.fx.opened.emit()
        self.ack(utilization=cap)
        self.assertEqual(self.win.db_max.value(), 0)
        # status-driven RF clear (_on_monitor_status -> _sync_rf_state):
        # an ack without spectrum capability also restores the Demo default
        fields = config_from_frame(self.fx.writes[-1])
        self.fx.deliver(tlv.encode_status_json(
            rf_config_event(**fields, spectrum=False)))
        self.assertFalse(self.win._rf_active)
        self.assertEqual(self.win.db_max.value(), -20)

    def test_sweep_row_contains_only_its_own_cycle(self) -> None:
        """One history row per marker; closing/masking happens BEFORE the
        push so a row never carries another cycle's leftover values."""
        self.win.mode_tabs.setCurrentIndex(1)
        self.ack()
        i2437 = int(np.argmin(np.abs(self.win.freqs - 2437.0)))
        i2412 = int(np.argmin(np.abs(self.win.freqs - 2412.0)))
        self.fx.deliver(rf_blob(mode=1, cycle=1, center_khz=2437000))
        self.deliver(cycle_event(cycle=1))
        self.assertAlmostEqual(
            self.val_at(2437.0, self.win.wf[0]), -30.0, places=3)
        # cycle 2 observes only 2412 (staged until its marker)
        self.fx.deliver(rf_blob(mode=1, cycle=2, center_khz=2412000))
        self.assertAlmostEqual(self.val_at(2437.0), -30.0, places=3)
        self.deliver(cycle_event(cycle=2))
        # row0 = cycle2 ONLY: the 2437 leftover must NOT be in history
        self.assertAlmostEqual(
            self.val_at(2412.0, self.win.wf[0]), -30.0, places=3)
        self.assertTrue(np.isnan(self.win.wf[0][i2437]))
        self.assertTrue(np.isnan(self.win.cur[i2437]))  # gaps after close
        # row1 = cycle1's row (one row per marker), covering only 2437
        self.assertAlmostEqual(
            self.val_at(2437.0, self.win.wf[1]), -30.0, places=3)
        self.assertTrue(np.isnan(self.win.wf[1][i2412]))
        # row2 untouched: exactly two markers -> exactly two rows
        self.assertTrue(np.isnan(self.win.wf[2]).all())
        # peak hold KEEPS both cycles' history by design
        self.assertAlmostEqual(self.val_at(2437.0, self.win.peak), -30.0,
                               places=3)
        self.assertAlmostEqual(self.val_at(2412.0, self.win.peak), -30.0,
                               places=3)

    def test_fully_missing_cycle_pushes_all_nan_row(self) -> None:
        self.win.mode_tabs.setCurrentIndex(1)
        self.ack()
        self.deliver(cycle_event(cycle=1))       # marker, zero captures
        self.assertTrue(np.isnan(self.win.cur).all())
        self.assertTrue(np.isnan(self.win.wf[0]).all())  # honest empty row
        self.assertEqual(self.win.sweeps_done, 1)

    def test_lost_marker_live_closes_old_coverage_next_cycle(self) -> None:
        self.ack()                                # live, marker1 LOST
        self.fx.deliver(rf_blob(cycle=1, center_khz=2437000))
        i2470 = int(np.argmin(np.abs(self.win.freqs - 2470.0)))
        self.assertTrue(np.isnan(self.win.cur[i2470]))   # gap stays gap
        self.fx.deliver(rf_blob(cycle=2, center_khz=2412000))
        # first frame of cycle2 closes the lost cycle1: its coverage
        # persists as last-complete, regions cycle1 never saw are NaN
        self.assertAlmostEqual(self.val_at(2437.0), -30.0, places=3)
        self.assertAlmostEqual(self.val_at(2412.0), -30.0, places=3)
        self.deliver(cycle_event(cycle=2))
        # marker2 closes cycle2: 2437 (cycle1 only) becomes a gap
        self.assertTrue(np.isnan(self.win.cur[i2470]))
        self.assertTrue(np.isnan(
            self.win.cur[int(np.argmin(np.abs(self.win.freqs - 2437.0)))]))
        self.assertAlmostEqual(self.val_at(2412.0), -30.0, places=3)

    def test_missing_capture_spans_never_bridged(self) -> None:
        self.ack()
        self.fx.deliver(rf_blob(cycle=1, center_khz=2412000),   # 2402..2419.5
                       rf_blob(cycle=1, center_khz=2472000))    # 2462..2479.5
        self.assertAlmostEqual(self.val_at(2410.0), -30.0, places=3)
        self.assertAlmostEqual(self.val_at(2470.0), -30.0, places=3)
        # the span between the two captures stays NaN: no interpolation
        # ever bridges genuinely missing captures
        self.assertTrue(np.isnan(self.win.cur[np.abs(
            self.win.freqs - 2440.0) < 0.1]).all())
        self.deliver(cycle_event(cycle=1))
        self.assertTrue(np.isnan(self.win.cur[np.abs(
            self.win.freqs - 2440.0) < 0.1]).all())

    def test_disconnect_then_demo_leaves_no_stale_rf_or_mock_data(self) -> None:
        self.ack()
        self.fx.deliver(rf_blob(power=[-30.0] * 8))
        self.win._detach()
        self.assertTrue(np.isnan(self.win.cur).all())
        self.assertEqual(self.win.dev_lbl.text(), DISCONNECTED_TEXT)
        self.assertFalse(self.win.monitor_state.ready)
        self.assertFalse(self.win._rf_active)
        # demo attach: floor background restored, every RF trace gone
        src = MockSource(self.win)
        self.win._attach(src)
        self.assertFalse(np.isnan(self.win.cur).any())
        self.assertTrue((self.win.peak == -200.0).all())
        self.assertEqual(self.win.spec_card.cap_lbl.text(), "POWER · dBm")
        self.assertEqual(header_text(self.win.ch_table, 3), "Peak")
        self.assertEqual(self.win.util_card.cap_lbl.text(),
                         "UTILIZATION · %")
        self.win._detach()

    def test_display_controls_toggle_and_zoom_in_rf_mode(self) -> None:
        self.ack()
        self.fx.deliver(rf_blob(power=[-30.0] * 8))
        self.win.peak_chk.setChecked(False)
        self.assertFalse(self.win.peak_curve.isVisible())
        self.win.peak_chk.setChecked(True)
        self.assertTrue(self.win.peak_curve.isVisible())
        self.win.wf_chk.setChecked(False)
        self.assertFalse(self.win.wf_card.isVisible())
        self.win.wf_chk.setChecked(True)
        self.assertTrue(self.win.wf_card.isVisible())
        self.win.db_min.setValue(-90)
        self.assertIn("-90 dBFS", self.win.db_min_lbl.text())
        self.win._zoom_to_channel(5, 0)
        lo, hi = self.win.spec_plot.getViewBox().viewRange()[0]
        self.assertLess(hi - lo, 150)
        self.win._reset_x()
        self.win.ch_chk.setChecked(False)
        self.assertFalse(self.win.util_card.isVisible())
        self.win.ch_chk.setChecked(True)
        self.assertTrue(self.win.util_card.isVisible())

    def test_fft_and_rate_changes_send_config_requests(self) -> None:
        self.ack()
        writes = len(self.fx.writes)
        self.win.fft_combo.setCurrentText("256")
        self.win.sr_combo.setCurrentText("40 MS/s")
        sent = [config_from_frame(w) for w in self.fx.writes[writes:]]
        self.assertEqual([s["fft_size"] for s in sent], [256, 256])
        self.assertEqual([s["sample_rate_khz"] for s in sent],
                         [20000, 40000])
        self.assertEqual(self.win.rbw_lbl.text(), "156.2 kHz")  # unchanged:
        # RBW follows the device's EFFECTIVE values, not the new request


@unittest.skipUnless(os.name == "posix", "PTY integration requires POSIX")
class RfHardwareStreamPtyTests(unittest.TestCase):
    """Real SerialReader thread over a PTY, full GUI wiring (SYNTHETIC
    device bytes; no hardware attached)."""

    def setUp(self) -> None:
        import pty
        import tty

        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.slave_path = os.ttyname(slave)
        os.close(slave)
        self.win = MainWindow()
        self.win.show()

    def tearDown(self) -> None:
        self.win.close()
        APP.processEvents()
        with contextlib.suppress(OSError):
            os.close(self.master)

    def _pump_config(self) -> dict:
        import select
        import time

        parser = tlv.TlvParser()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            APP.processEvents()
            if select.select([self.master], [], [], 0.05)[0]:
                try:
                    chunk = os.read(self.master, 4096)
                except OSError:
                    break
                for msg in parser.feed(chunk):
                    if isinstance(msg, dict):
                        return msg
        raise AssertionError("no CONFIG arrived after serial open")

    def test_rf_stream_renders_end_to_end(self) -> None:
        win = self.win
        win.port_combo.setCurrentText(self.slave_path)
        win.connect_btn.setChecked(True)
        try:
            fields = self._pump_config()      # proves opened -> CONFIG
            self.assertEqual(fields, CFG_DEFAULTS)
            os.write(self.master, tlv.encode_status_json(
                rf_config_event(**fields)))
            self.assertTrue(spin_until(lambda: win.monitor_state.rf_ready),
                            "RF capability echo did not reach MonitorState")
            self.assertTrue(spin_until(
                lambda: not win.fft_combo.isEnabled() or
                win.fft_combo.isEnabled()), "controls synced")
            self.assertTrue(win.fft_combo.isEnabled())
            os.write(self.master, rf_blob(power=[-33.0] * 8))
            idx = int(np.argmin(np.abs(win.freqs - 2437.0)))

            def rendered() -> bool:
                APP.processEvents()
                return (not np.isnan(win.cur[idx])
                        and abs(float(win.cur[idx]) + 33.0) < 1e-3)

            self.assertTrue(spin_until(rendered),
                            "0x04 frame did not render through the "
                            "real serial thread")
            win._render()
            self.assertEqual(
                cell_text(win.ch_table, row_of(win, 6), 3), "-33")
            self.assertEqual(win.spec_card.cap_lbl.text(), "POWER · dBFS")
        finally:
            win.connect_btn.setChecked(False)
            win._detach()
        self.assertTrue(np.isnan(win.cur).all())
        self.assertEqual(win.dev_lbl.text(), DISCONNECTED_TEXT)


class ConfigAckRetryTests(unittest.TestCase):
    """Bounded cancellable resend of the latest CONFIG until a fresh
    matching ack. The gate log only showed a missing ack on run1 and a
    recovery on the identical rerun (cause not proven); these tests
    establish the lost-first-CONFIG class plus the cancel/bound rules."""

    def setUp(self) -> None:
        self.win = MainWindow()
        self.win.show()
        APP.processEvents()
        self.fx = FixtureReader()
        self.win._attach(self.fx)
        self.win.play_btn.setChecked(True)
        p = mock.patch.object(main_window, "CFG_RETRY_MS", 10)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self) -> None:
        self.win.close()
        APP.processEvents()

    def _ack_latest(self) -> None:
        fields = config_from_frame(self.fx.writes[-1])
        self.fx.deliver(tlv.encode_status_json(config_event(**fields)))

    def test_boot_loss_recovers_on_retry(self) -> None:
        self.fx.opened.emit()          # first CONFIG (the device drops it)
        self.assertEqual(len(self.fx.writes), 1)
        self.assertFalse(self.win.monitor_state.ready)
        self.assertTrue(
            spin_until(lambda: len(self.fx.writes) >= 2),
            "no bounded resend of the idempotent CONFIG")
        self._ack_latest()
        self.assertTrue(spin_until(lambda: self.win.monitor_state.ready))
        self.assertFalse(
            self.win._cfg_timer.isActive(),
            "retry chain must stop on the fresh matching ack")

    def test_retry_is_bounded_and_failure_visible(self) -> None:
        p = mock.patch.object(main_window, "CFG_MAX_RETRIES", 3)
        p.start()
        self.addCleanup(p.stop)
        import time

        self.fx.opened.emit()
        self.assertTrue(
            spin_until(lambda: len(self.fx.writes) >= 1 + 3),
            "resends did not reach the configured bound")
        time.sleep(0.05)
        APP.processEvents()
        self.assertEqual(len(self.fx.writes), 1 + 3,
                         "retries must stop at CFG_MAX_RETRIES")
        self.assertFalse(self.win._cfg_timer.isActive())
        self.assertIn("No configuration acknowledgement",
                      self.win.statusBar().currentMessage())

    def test_disconnect_cancels_retry_chain(self) -> None:
        import time

        self.fx.opened.emit()
        self.win._detach()
        self.assertFalse(self.win._cfg_timer.isActive())
        time.sleep(0.05)
        APP.processEvents()
        self.assertEqual(len(self.fx.writes), 1, "stale retry after detach")

    def test_change_while_unacked_resends_latest_tuple(self) -> None:
        self.fx.opened.emit()
        self.win.fft_combo.setCurrentText("256")   # new intent, still unacked
        self.assertTrue(spin_until(lambda: len(self.fx.writes) >= 3))
        self.assertEqual(config_from_frame(self.fx.writes[-1])["fft_size"],
                         256, "retry must resend the LATEST tuple")
        self._ack_latest()
        self.assertTrue(spin_until(lambda: self.win.monitor_state.ready))
        self.assertFalse(self.win._cfg_timer.isActive())

    def test_wrong_request_ack_neither_readies_nor_stops_retry(self) -> None:
        self.fx.opened.emit()
        good = config_from_frame(self.fx.writes[-1])
        self.fx.deliver(tlv.encode_status_json(
            config_event(**{**good, "fft_size": 256})))  # wrong echo
        self.assertFalse(self.win.monitor_state.ready)
        self.assertTrue(self.win._cfg_timer.isActive(),
                        "a wrong-request heartbeat must not stop the chain")
        self.assertTrue(spin_until(lambda: len(self.fx.writes) >= 2))
        self._ack_latest()
        self.assertTrue(spin_until(lambda: self.win.monitor_state.ready))

    def test_demo_switch_cancels_serial_retry(self) -> None:
        self.fx.opened.emit()
        self.win.demo_btn.setChecked(True)   # detach + MockSource attach
        self.assertFalse(self.win._cfg_timer.isActive())
        self.win.demo_btn.setChecked(False)

    def test_legacy_fallback_cancels_retry(self) -> None:
        import time

        self.fx.opened.emit()
        self.win._legacy_fallback()          # legacy 0x01 stream detected
        self.assertFalse(self.win._cfg_timer.isActive())
        time.sleep(0.05)
        APP.processEvents()
        self.assertEqual(len(self.fx.writes), 1,
                         "stale retry after legacy fallback")


if __name__ == "__main__":
    unittest.main()
