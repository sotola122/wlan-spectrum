"""Real serial-open behavior: CONFIG is transmitted only after the port opens.

Linux PTY integration tests exercise the actual MainWindow -> SerialReader
wiring (the original bug: the first CONFIG was written before the port
existed and silently dropped). The failed-open and detach checks are
platform-independent.
"""

from __future__ import annotations

import contextlib
import os
import time
import unittest

from PySide6.QtWidgets import QApplication

from wifi_spectrum import tlv
from wifi_spectrum.main_window import MainWindow
from wifi_spectrum.serial_link import SerialReader

APP = QApplication.instance() or QApplication([])

CONFIG_TIMEOUT_S = 2.0


def spin_until(condition, timeout_s: float = CONFIG_TIMEOUT_S) -> bool:
    """Process Qt events until condition() or the fixed deadline."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        APP.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return bool(condition())


class ConfigWatch:
    """Keeps one TlvParser alive across reads so partial frames survive."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.parser = tlv.TlvParser()
        self.configs: list[dict] = []

    def pump(self) -> bool:
        try:
            os.set_blocking(self.fd, False)
            chunk = os.read(self.fd, 4096)
        except (BlockingIOError, OSError):
            chunk = b""
        for msg in self.parser.feed(chunk):
            if isinstance(msg, dict):
                self.configs.append(msg)
        return bool(self.configs)


@unittest.skipUnless(os.name == "posix", "PTY integration requires POSIX")
class SerialOpenConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        import pty
        import tty

        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.slave_path = os.ttyname(slave)
        os.close(slave)

    def tearDown(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.master)

    def _drain_master(self) -> None:
        import select

        while select.select([self.master], [], [], 0)[0]:
            try:
                os.read(self.master, 4096)
            except OSError:
                break               # no slave attached yet (EIO on PTYs)

    def test_nondefault_config_sent_once_after_open(self) -> None:
        win = MainWindow()
        win.port_combo.setCurrentText(self.slave_path)
        win.mode_tabs.setCurrentIndex(1)          # Sweep
        win.band_seg.setCurrentIndex(1)           # 5 GHz
        win.sweep_time.setValue(3000)
        win.fft_combo.setCurrentText("1024")
        win.sr_combo.setCurrentText("40 MS/s")
        watch = ConfigWatch(self.master)
        win.connect_btn.setChecked(True)
        try:
            self.assertTrue(spin_until(watch.pump),
                            "no CONFIG arrived after serial open")
            self.assertEqual(watch.configs[0],
                             {"mode": 1, "band": 1, "sweep_ms": 3000,
                              "fft_size": 1024, "sample_rate_khz": 40000})
            # exactly one CONFIG: no duplicate from a pre-open attempt and
            # none from an immediate re-send
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:
                APP.processEvents()
                watch.pump()
                time.sleep(0.01)
            self.assertEqual(watch.configs, [watch.configs[0]],
                             "unexpected extra CONFIG frames")
        finally:
            win.connect_btn.setChecked(False)
            win._detach()
            win.close()

    def test_reconnect_sends_fresh_config(self) -> None:
        win = MainWindow()
        win.port_combo.setCurrentText(self.slave_path)
        win.connect_btn.setChecked(True)
        try:
            watch = ConfigWatch(self.master)
            self.assertTrue(spin_until(watch.pump), "no CONFIG on connect")
            win.connect_btn.setChecked(False)
            self._drain_master()
            win.connect_btn.setChecked(True)
            watch2 = ConfigWatch(self.master)
            self.assertTrue(spin_until(watch2.pump),
                            "reconnect did not send a new CONFIG")
        finally:
            win.connect_btn.setChecked(False)
            win._detach()
            win.close()

    def test_detach_disconnects_opened_callback(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.frames: list[bytes] = []

            def write(self, data: bytes) -> None:
                self.frames.append(data)

        win = MainWindow()
        reader = SerialReader(self.slave_path, 921600)
        recorder = Recorder()
        reader.write = recorder.write      # observe without a real port
        win._attach(reader)
        # positive control: the wiring exists and fires
        reader.opened.emit()
        self.assertEqual(len(recorder.frames), 1,
                         "opened must be wired to _send_config")
        win._detach()                      # must drop that wiring
        win.source = reader                # make a retained call observable
        reader.opened.emit()
        self.assertEqual(len(recorder.frames), 1,
                         "old source's opened callback must not survive detach")
        win.source = None
        win.close()

    def test_mock_source_still_configures_immediately(self) -> None:
        # Demo path must keep its immediate CONFIG (no open event on mocks).
        from wifi_spectrum.mock import MockSource

        win = MainWindow()
        src = MockSource(win)
        writes: list[bytes] = []
        original = src.write
        src.write = lambda data: (writes.append(data), original(data))[1]
        win._attach(src)
        self.assertTrue(writes, "MockSource got no immediate CONFIG")
        win._detach()


class FailedOpenTests(unittest.TestCase):
    def test_failed_open_emits_error_not_opened(self) -> None:
        reader = SerialReader("/nonexistent/wifi-monitor-port", 921600)
        opened = []
        errors = []
        reader.opened.connect(lambda: opened.append(True))
        reader.error.connect(errors.append)
        reader.start()
        self.assertTrue(spin_until(lambda: bool(errors)),
                        "no error emitted for failed open")
        reader.stop()
        self.assertEqual(opened, [])
        self.assertIn("Cannot open port", errors[0])


if __name__ == "__main__":
    unittest.main()
