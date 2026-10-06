"""Real monitor view vs Demo stack, driven through actual dispatch paths.

Every measurement enters ``MainWindow`` the same way hardware bytes do:
encoded with the real CRC framing, parsed by ``TlvParser``, dispatched by
``SerialReader._dispatch``, routed by ``_on_status`` into ``MonitorState``
and rendered by ``MonitorWidget``. The PTY test additionally exercises the
real serial read thread end to end (POSIX only).
"""

from __future__ import annotations

import contextlib
import os
import unittest

from PySide6.QtWidgets import QApplication, QLabel

from wifi_spectrum import tlv
from wifi_spectrum.main_window import MainWindow
from wifi_spectrum.mock import MockSource
from wifi_spectrum.monitor_widget import CAPTION, DISCONNECTED_TEXT, WAITING_TEXT
from wifi_spectrum.serial_link import SerialReader

APP = QApplication.instance() or QApplication([])

CFG_DEFAULTS = {"mode": 0, "band": 0, "sweep_ms": 1000, "fft_size": 64,
                "sample_rate_khz": 20000}
AP = {"bssid": "001122334455", "ssid_hex": "74657374",
      "primary_ch": 6, "rssi_dbm": -48}


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
    d = {"schema": "wifi-monitor/1", "event": "config", "epoch": epoch,
         "dwell_ms": 120, "channels": list(range(1, 14)), "tx_dropped": 0,
         "spectrum": False, "cca": False, "fft_supported": False,
         **CFG_DEFAULTS}
    d.update(over)
    return d


def channel_event(epoch: int = 1, cycle: int = 1, ch: int = 6,
                  packets: int = 24, peak: int | None = -48,
                  observed_ms: int = 120, aps: list | None = None,
                  band: int = 0, **over) -> dict:
    d = {"schema": "wifi-monitor/1", "event": "channel", "epoch": epoch,
         "cycle": cycle, "band": band, "ch": ch, "observed_ms": observed_ms,
         "packets": packets, "peak_rssi_dbm": peak,
         "aps": aps if aps is not None else [], "ap_dropped": 0}
    d.update(over)
    return d


def cycle_event(epoch: int = 1, cycle: int = 1, band: int = 0, **over) -> dict:
    d = {"schema": "wifi-monitor/1", "event": "cycle", "epoch": epoch,
         "cycle": cycle, "band": band, "elapsed_ms": 1600,
         "uptime_ms": 1600 * cycle}
    d.update(over)
    return d


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


class RealViewSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.win = MainWindow()
        self.win.show()
        APP.processEvents()

    def tearDown(self) -> None:
        self.win.close()
        APP.processEvents()

    def test_serial_attach_shows_monitor_view_and_waiting(self) -> None:
        fx = FixtureReader()
        self.win._attach(fx)
        self.assertEqual(self.win.plot_stack.currentIndex(), 1)
        self.assertEqual(self.win.monitor_widget.ack_lbl.text(), WAITING_TEXT)
        self.assertFalse(self.win.fft_combo.isEnabled())
        self.assertFalse(self.win.sr_combo.isEnabled())
        self.assertFalse(self.win.peak_chk.isEnabled())
        self.assertFalse(self.win.wf_chk.isEnabled())
        self.assertFalse(self.win.peak_reset_btn.isEnabled())
        self.assertFalse(self.win.channel_group.isVisible())
        self.assertEqual(self.win.rbw_lbl.text(), "N/A — no RF FFT")
        # real Live still allows editing the hopping interval
        self.assertTrue(self.win.sweep_time.isEnabled())
        fx.opened.emit()                     # port-open signal sends CONFIG
        self.assertEqual(len(fx.writes), 1)
        self.assertEqual(config_from_frame(fx.writes[0]), CFG_DEFAULTS)

    def test_demo_attach_restores_original_view(self) -> None:
        self.win._attach(FixtureReader())
        self.assertEqual(self.win.plot_stack.currentIndex(), 1)
        src = MockSource(self.win)
        # detach the serial fixture first, like the real toggle handlers do
        self.win._detach()
        self.win._attach(src)
        self.assertEqual(self.win.plot_stack.currentIndex(), 0)
        self.assertTrue(self.win.fft_combo.isEnabled())
        self.assertTrue(self.win.sr_combo.isEnabled())
        self.assertTrue(self.win.peak_chk.isEnabled())
        self.assertTrue(self.win.wf_chk.isEnabled())
        self.assertTrue(self.win.channel_group.isVisible())
        self.assertNotEqual(self.win.rbw_lbl.text(), "N/A — no RF FFT")
        self.assertFalse(self.win.sweep_time.isEnabled())  # demo Live
        self.win._detach()

    def test_wifi_monitor_status_keeps_monitor_view(self) -> None:
        fx = FixtureReader()
        self.win._attach(fx)
        self.win.play_btn.setChecked(True)
        fx.opened.emit()
        fx.deliver(tlv.encode_status_json(config_event(**CFG_DEFAULTS)))
        self.assertTrue(self.win.monitor_state.ready)
        self.assertEqual(self.win.plot_stack.currentIndex(), 1)

    def test_legacy_spectrum_falls_back_to_plots(self) -> None:
        fx = FixtureReader()
        self.win._attach(fx)
        self.win.play_btn.setChecked(True)
        fx.deliver(tlv.encode_spectrum(2400.0, 0.5, [-50.0, -60.0]))
        self.assertEqual(self.win.plot_stack.currentIndex(), 0)
        self.assertTrue(self.win.fft_combo.isEnabled())
        self.assertNotEqual(self.win.rbw_lbl.text(), "N/A — no RF FFT")


class MonitorWidgetRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.win = MainWindow()
        self.win.show()
        APP.processEvents()
        self.fx = FixtureReader()
        self.win._attach(self.fx)
        self.win.play_btn.setChecked(True)
        self.fx.opened.emit()
        self.widget = self.win.monitor_widget

    def tearDown(self) -> None:
        self.win.close()
        APP.processEvents()

    def ack(self, **over) -> dict:
        fields = config_from_frame(self.fx.writes[-1])
        event = config_event(**fields, **over)
        self.fx.deliver(tlv.encode_status_json(event))
        self.assertTrue(self.win.monitor_state.ready, "config not acknowledged")
        return fields

    def deliver(self, *events: dict) -> None:
        self.fx.deliver(*(tlv.encode_status_json(e) for e in events))

    def table_row(self, channel: int) -> list[str]:
        row = self.win.monitor_state.channels.index(channel)
        return [cell_text(self.widget.ch_table, row, col)
                for col in range(self.widget.ch_table.columnCount())]

    def test_labels_and_caption_make_no_fft_or_utilization_claims(self) -> None:
        self.ack()
        texts = [label.text()
                 for label in self.widget.findChildren(QLabel)]
        self.assertIn("Received-frame RSSI", texts)
        self.assertIn("Received packets/s", texts)
        self.assertIn(CAPTION, texts)
        # X tick strings are channel numbers on BOTH plots (channel centers
        # are positioned by frequency), so the axis labels must say so.
        self.assertEqual(
            self.widget.rssi_plot.getAxis("bottom").labelText.strip(),
            "Channel")
        self.assertEqual(
            self.widget.rate_plot.getAxis("bottom").labelText.strip(),
            "Channel")
        for text in texts:
            self.assertNotIn("%", text, text)
            self.assertNotIn("FFT", text, text)
            self.assertNotIn("RBW", text, text)
        self.assertEqual(self.win.rbw_lbl.text(), "N/A — no RF FFT")

    def test_rssi_scatter_and_channel_table_at_real_channel_center(self) -> None:
        self.ack()
        self.deliver(channel_event(ch=6, aps=[AP]))
        self.assertEqual(self.table_row(6),
                         ["6", "2437", "-48", "200.0", "120"])
        xs, ys = self.widget.rssi_scatter.getData()
        self.assertEqual(list(xs), [2437.0])   # channel center, no interpolation
        self.assertEqual(list(ys), [-48.0])
        self.assertIn("dwell 120 ms", self.widget.ack_lbl.text())
        self.assertIn("epoch 1", self.widget.ack_lbl.text())

    def test_ap_table_shows_ssid_and_bssid(self) -> None:
        self.ack()
        self.deliver(channel_event(ch=6, aps=[AP]))
        self.assertEqual(self.widget.ap_table.rowCount(), 1)
        cells = [cell_text(self.widget.ap_table, 0, col)
                 for col in range(self.widget.ap_table.columnCount())]
        self.assertEqual(cells[:5], ["test", "001122334455", "6", "6", "-48"])

    def test_unknown_rssi_and_unobserved_channels_stay_unavailable(self) -> None:
        self.ack()
        self.deliver(channel_event(ch=1, packets=0, peak=None))
        self.assertEqual(self.table_row(1), ["1", "2412", "—", "0.0", "120"])
        self.assertEqual(self.table_row(6), ["6", "2437", "—", "—", "—"])
        xs, _ = self.widget.rssi_scatter.getData()
        self.assertEqual(len(xs), 0)           # no invented RSSI point

    def test_two_hundred_packets_per_second_is_not_clamped(self) -> None:
        self.ack()
        self.deliver(channel_event(ch=6))      # 24 packets / 120 ms
        self.assertEqual(self.table_row(6)[3], "200.0")
        heights = list(self.widget.rate_bars.opts.get("height") or [])
        self.assertIn(200.0, heights)
        top = self.widget.rate_plot.getViewBox().viewRange()[1][1]
        self.assertGreater(top, 100)

    def test_sweep_stages_until_cycle_completes(self) -> None:
        self.win.mode_tabs.setCurrentIndex(1)          # Band Sweep
        self.ack()
        self.deliver(channel_event(ch=6))
        self.assertEqual(self.table_row(6)[2], "—")    # staged, not visible
        self.deliver(cycle_event(cycle=1))
        self.assertEqual(self.table_row(6)[2], "-48")
        self.assertEqual(self.win.sweeps_done, 1)

    def test_pause_gates_measurements_but_not_config_or_error(self) -> None:
        self.win.play_btn.setChecked(False)            # paused
        self.ack()
        self.assertTrue(self.win.monitor_state.ready)
        self.deliver(channel_event(ch=6))
        self.assertEqual(self.win.monitor_state.displayed, {})
        self.deliver({"schema": "wifi-monitor/1", "event": "error",
                      "code": "invalid_config"})
        self.assertIn("invalid_config", self.widget.diag_lbl.text())
        self.win.play_btn.setChecked(True)
        self.deliver(channel_event(ch=6))
        self.assertIn(6, self.win.monitor_state.displayed)

    def test_unique_cycles_count_once_and_limit_stops_play(self) -> None:
        self.win.mode_tabs.setCurrentIndex(1)
        self.win.sweep_count.setValue(2)
        self.ack()
        self.deliver(channel_event(ch=6, cycle=1), cycle_event(cycle=1))
        self.assertEqual(self.win.sweeps_done, 1)
        self.deliver(cycle_event(cycle=1))             # duplicate: ignored
        self.assertEqual(self.win.sweeps_done, 1)
        self.deliver(channel_event(ch=6, cycle=2), cycle_event(cycle=2))
        self.assertEqual(self.win.sweeps_done, 2)
        self.assertFalse(self.win.play_btn.isChecked())  # limit reached
        self.deliver(channel_event(ch=6, cycle=3), cycle_event(cycle=3))
        self.assertEqual(self.win.sweeps_done, 2)        # paused: no count

    def test_band_change_resets_display_and_reconfigures(self) -> None:
        self.ack()
        self.deliver(channel_event(ch=6))
        self.assertIn(6, self.win.monitor_state.displayed)
        self.win.band_seg.setCurrentIndex(1)            # 5 GHz
        self.assertFalse(self.win.monitor_state.ready)
        self.assertEqual(self.win.monitor_state.displayed, {})
        self.assertEqual(self.widget.ack_lbl.text(),
                         "Waiting for configuration")
        self.assertEqual(config_from_frame(self.fx.writes[-1])["band"], 1)
        fields = config_from_frame(self.fx.writes[-1])
        self.deliver(config_event(**fields, channels=[36, 40]))
        self.assertTrue(self.win.monitor_state.ready)
        self.deliver(channel_event(ch=36, band=1))
        self.assertEqual(self.widget.ch_table.rowCount(), 2)

    def test_disconnect_clears_state_and_view(self) -> None:
        self.ack()
        self.deliver(channel_event(ch=6))
        self.assertEqual(self.widget.ch_table.rowCount(), 13)
        self.win._detach()
        self.assertFalse(self.win.monitor_state.ready)
        self.assertEqual(self.widget.ack_lbl.text(), DISCONNECTED_TEXT)
        self.assertEqual(self.widget.ch_table.rowCount(), 0)
        self.assertEqual(self.widget.ap_table.rowCount(), 0)

    def test_malformed_observation_is_reported_visibly(self) -> None:
        self.ack()
        self.deliver(channel_event(ch=6, observed_ms=-5))
        self.assertEqual(self.win.monitor_state.displayed, {})
        self.assertIn("Rejected monitor payload",
                      self.win.statusBar().currentMessage())

    def test_cycle_missed_channel_hides_stale_value_then_recovers(self) -> None:
        self.ack()
        self.deliver(channel_event(ch=6), channel_event(ch=7),
                     cycle_event(cycle=1))
        self.assertEqual(self.table_row(7)[2], "-48")
        # cycle 2 loses channel 7 in transit: the stale value must vanish
        self.deliver(channel_event(ch=6, cycle=2), cycle_event(cycle=2))
        self.assertEqual(self.table_row(7)[2], "—")
        # a later valid observation recovers the row
        self.deliver(channel_event(ch=7, cycle=3))
        self.assertEqual(self.table_row(7)[2], "-48")


@unittest.skipUnless(os.name == "posix", "PTY integration requires POSIX")
class HardwareStreamPtyTests(unittest.TestCase):
    """Real SerialReader thread over a PTY, full GUI wiring."""

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

        parser = tlv.TlvParser()
        deadline_configs: list[dict] = []
        import time

        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            APP.processEvents()
            if select.select([self.master], [], [], 0.05)[0]:
                try:
                    chunk = os.read(self.master, 4096)
                except OSError:
                    break
                for msg in parser.feed(chunk):
                    if isinstance(msg, dict):
                        deadline_configs.append(msg)
            if deadline_configs:
                return deadline_configs[0]
        raise AssertionError("no CONFIG arrived after serial open")

    def test_stream_updates_monitor_view_end_to_end(self) -> None:
        win = self.win
        win.port_combo.setCurrentText(self.slave_path)
        win.connect_btn.setChecked(True)
        try:
            fields = self._pump_config()      # proves opened -> CONFIG
            self.assertEqual(fields, CFG_DEFAULTS)
            os.write(self.master, tlv.encode_status_json(
                config_event(**fields)))
            self.assertTrue(spin_until(lambda: win.monitor_state.ready),
                            "config echo did not reach MonitorState")
            os.write(self.master, tlv.encode_status_json(
                channel_event(ch=6, aps=[AP])))

            def row_ready() -> bool:
                widget = win.monitor_widget
                if widget.ch_table.rowCount() < 6:
                    return False
                item = widget.ch_table.item(5, 2)
                return item is not None and item.text() == "-48"

            self.assertTrue(spin_until(row_ready),
                            "channel observation did not render")
            widget = win.monitor_widget
            self.assertEqual(cell_text(widget.ch_table, 5, 3), "200.0")
            self.assertEqual(widget.ap_table.rowCount(), 1)
            self.assertEqual(cell_text(widget.ap_table, 0, 1),
                             "001122334455")
        finally:
            win.connect_btn.setChecked(False)
            win._detach()
        self.assertFalse(win.monitor_state.ready)
        self.assertEqual(win.monitor_widget.ack_lbl.text(), DISCONNECTED_TEXT)


if __name__ == "__main__":
    unittest.main()
