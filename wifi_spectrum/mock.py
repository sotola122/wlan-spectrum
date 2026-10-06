"""Mock ESP32-C5 device: generates fake Wi-Fi spectra as real TLV bytes.

Used two ways:
  * In-process demo mode (``MockSource``). Works on Windows, Linux, and macOS::

        uv run wifi-spectrum --demo

  * Fake serial device on a POSIX pseudo-terminal (Linux and macOS)::

        python -m wifi_spectrum.mock --pty

    then connect the GUI to the printed /dev/pts/N path. This exercises the
    full SerialReader + TLV path end to end. On Windows ``--pty`` exits with
    a message; use demo mode there.
"""

from __future__ import annotations

import contextlib
import math
import random
import sys
import time

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from . import tlv
from .bands import BAND_5, BAND_24, BANDS, channel_freq

MODE_LIVE, MODE_SWEEP = 0, 1

# (center MHz, bandwidth MHz, peak dBm, duty cycle 0..1)
_APS = {
    BAND_24: [(2412, 20, -62, 0.45), (2437, 20, -54, 0.70), (2462, 20, -70, 0.30),
              (2422, 20, -82, 0.15), (2447, 20, -78, 0.10)],
    BAND_5: [(5210, 80, -60, 0.55), (5260, 20, -76, 0.25), (5510, 40, -67, 0.40),
             (5600, 20, -84, 0.10), (5775, 80, -57, 0.65), (5825, 20, -80, 0.20)],
}


def _mask_db(df: np.ndarray, bw: float) -> np.ndarray:
    """Rough OFDM spectral mask (dB relative to in-band level) vs offset from center."""
    h = bw / 2
    x = np.abs(df)
    return np.interp(x, [0, h - 1, h + 1, h + 10, bw, bw * 1.5],
                     [0, 0, -20, -28, -40, -60])


class MockDevice:
    """Hardware-free signal generator. Call ``tick(dt)`` to get TLV bytes."""

    def __init__(self) -> None:
        self.mode, self.band = MODE_LIVE, BAND_24
        self.sweep_ms, self.fft_size, self.sample_rate_khz = 1000, 64, 20000
        self.sweep_count = 0
        self._seg_idx, self._seg_acc = 0, 0.0
        self._util_acc, self._status_acc = 0.0, 0.0
        self._t0 = time.monotonic()
        self._rx = tlv.TlvParser()
        self._microwave = 0.0

    # -- control ------------------------------------------------------
    def configure(self, mode=None, band=None, sweep_ms=None, fft_size=None, sample_rate_khz=None):
        if band is not None and band != self.band:
            self._seg_idx = 0
        for k, v in {"mode": mode, "band": band, "sweep_ms": sweep_ms,
                     "fft_size": fft_size, "sample_rate_khz": sample_rate_khz}.items():
            if v is not None:
                setattr(self, k, v)

    def handle_rx(self, data: bytes) -> None:
        """Accept CONFIG frames sent by the PC."""
        for msg in self._rx.feed(data):
            if isinstance(msg, dict):
                self.configure(**msg)

    # -- signal model --------------------------------------------------
    def _render(self, freqs: np.ndarray) -> np.ndarray:
        mw = 10 ** ((-96 + np.random.normal(0, 1.6, freqs.shape)) / 10)
        for fc, bw, p, duty in _APS[self.band]:
            if random.random() < duty:
                lvl = p + random.uniform(-3, 2)
                mw += 10 ** ((lvl + _mask_db(freqs - fc, bw)
                              + np.random.normal(0, 1.0, freqs.shape)) / 10)
        if self.band == BAND_24:
            # Bluetooth-ish narrow hops + an occasional microwave oven hump
            for _ in range(2):
                fc = random.uniform(2402, 2480)
                mw += 10 ** ((-75 + _mask_db(freqs - fc, 2)) / 10)
            self._microwave = (self._microwave + 0.01) % 1.0
            if self._microwave > 0.7 and random.random() < 0.5:
                mw += 10 ** ((-72 + _mask_db(freqs - 2455, 16)) / 10)
        return (10 * np.log10(mw)).astype(np.float32)

    def _util(self) -> dict[int, int]:
        out = {}
        for ch in BANDS[self.band].channels:
            fc = channel_freq(self.band, ch)
            u = 0.0
            for apc, bw, p, duty in _APS[self.band]:
                overlap = max(0.0, min(fc + 10, apc + bw / 2) - max(fc - 10, apc - bw / 2)) / 20
                u += duty * overlap * 100 * (0.6 if p < -75 else 1.0)
            out[ch] = int(max(0, min(100, u + random.gauss(3, 2))))
        return out

    # -- frame generation ---------------------------------------------
    def tick(self, dt: float) -> bytes:
        info = BANDS[self.band]
        out = bytearray()
        if self.mode == MODE_LIVE:
            step = 0.3125 if self.band == BAND_24 else 1.0
            freqs = np.arange(info.f_start, info.f_stop + step / 2, step)
            out += tlv.encode_spectrum(info.f_start, step, self._render(freqs))
        else:
            # Sweep: the band is split into segments of one sample-rate width,
            # each one FFT (fft_size bins). Segments are paced over sweep_ms.
            seg_bw = self.sample_rate_khz / 1000.0
            step = seg_bw / self.fft_size
            nseg = max(1, math.ceil((info.f_stop - info.f_start) / seg_bw))
            self._seg_acc += dt * nseg / (self.sweep_ms / 1000.0)
            while self._seg_acc >= 1.0:
                self._seg_acc -= 1.0
                f0 = info.f_start + self._seg_idx * seg_bw
                freqs = f0 + step * np.arange(self.fft_size)
                out += tlv.encode_spectrum(f0, step, self._render(freqs))
                self._seg_idx += 1
                if self._seg_idx >= nseg:
                    self._seg_idx = 0
                    self.sweep_count += 1
        self._util_acc += dt
        if self._util_acc >= 0.5:
            self._util_acc = 0.0
            out += tlv.encode_ch_util(self.band, self._util())
        self._status_acc += dt
        if self._status_acc >= 1.0:
            self._status_acc = 0.0
            out += tlv.encode_status_json({
                "fw": "mock-0.1", "chip": "ESP32-C5", "band": self.band, "mode": self.mode,
                "sweep_count": self.sweep_count, "fft": self.fft_size,
                "uptime_ms": int((time.monotonic() - self._t0) * 1000),
            })
        return bytes(out)


class MockSource(QObject):
    """In-process demo source with the same signals as ``SerialReader``."""

    spectrum = Signal(object)
    ch_util = Signal(object)
    status = Signal(object)
    error = Signal(str)
    stats = Signal(int, int)

    def __init__(self, parent=None, interval_ms: int = 50) -> None:
        super().__init__(parent)
        self.device = MockDevice()
        self._parser = tlv.TlvParser()
        self._rx = 0
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._on_tick)
        self._last = time.monotonic()

    def start(self) -> None:
        self._last = time.monotonic()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def write(self, data: bytes) -> None:
        self.device.handle_rx(data)

    def _on_tick(self) -> None:
        now = time.monotonic()
        data = self.device.tick(min(now - self._last, 0.25))
        self._last = now
        self._rx += len(data)
        for msg in self._parser.feed(data):     # round-trip through the real codec
            if isinstance(msg, tlv.Spectrum):
                self.spectrum.emit(msg)
            elif isinstance(msg, tlv.ChannelUtil):
                self.ch_util.emit(msg)
            elif isinstance(msg, tlv.Status):
                self.status.emit(msg)
        self.stats.emit(self._rx, self._parser.errors)


_PTY_UNSUPPORTED = """\
--pty uses a POSIX pseudo-terminal and runs on Linux and macOS.
On Windows, start in-process demo mode (no virtual COM driver):
    uv run wifi-spectrum --demo
"""


def _run_pty() -> None:
    import os
    import select

    if sys.platform == "win32":
        print(_PTY_UNSUPPORTED, file=sys.stderr, end="")
        raise SystemExit(2)
    try:
        import pty
        import tty
    except ImportError:
        print(_PTY_UNSUPPORTED, file=sys.stderr, end="")
        raise SystemExit(2) from None

    master, slave = pty.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    print(f"Fake ESP32-C5 on {os.ttyname(slave)}  (Ctrl+C to stop)", flush=True)
    dev, last = MockDevice(), time.monotonic()
    try:
        while True:
            r, _, _ = select.select([master], [], [], 0.05)
            if r:
                dev.handle_rx(os.read(master, 4096))
            now = time.monotonic()
            data = dev.tick(now - last)
            last = now
            with contextlib.suppress(BlockingIOError):
                os.write(master, data)                     # nobody reading - drop frames
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if "--pty" in sys.argv:
        _run_pty()
    else:
        print(__doc__)
