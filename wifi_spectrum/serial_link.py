"""USB-UART link: background reader thread that parses TLV frames."""

from __future__ import annotations

import re
import threading

import serial
from PySide6.QtCore import QThread, Signal
from serial.tools import list_ports

from .tlv import ChannelUtil, Spectrum, SpectrumRf, Status, TlvParser


def _port_sort_key(device: str) -> list:
    """COM2 before COM10, and ttyUSB2 before ttyUSB10."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", device)]


def available_ports() -> list[str]:
    """Serial port device names.

    Windows: ``COM3``, ``COM10``, … (pass that name to ``serial.Serial``;
    pyserial adds the ``\\\\.\\`` prefix for ports above COM9).
    Linux: ``/dev/ttyUSB*`` and ``/dev/ttyACM*``.
    """
    names = [p.device for p in list_ports.comports() if p.device]
    return sorted(names, key=_port_sort_key)


class SerialReader(QThread):
    """Reads the serial port in its own thread and emits decoded messages.

    Signals are delivered to the GUI thread through Qt's queued connections.
    ``opened`` fires exactly once, after the port is successfully open and
    before any read, so callers can transmit their first frame safely.
    """

    spectrum = Signal(object)       # tlv.Spectrum
    spectrum_rf = Signal(object)    # tlv.SpectrumRf (0x04 real RF frames)
    ch_util = Signal(object)        # tlv.ChannelUtil
    status = Signal(object)         # tlv.Status
    error = Signal(str)
    stats = Signal(int, int)        # bytes received, parse errors
    opened = Signal()               # port is open; first write is safe

    def __init__(self, port: str, baud: int = 921600, parent=None) -> None:
        super().__init__(parent)
        self.port, self.baud = port, baud
        self._stop = threading.Event()
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()

    def run(self) -> None:
        parser, rx = TlvParser(), 0
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.05)
        except (serial.SerialException, OSError) as e:
            self.error.emit(f"Cannot open port: {e}")
            return
        self.opened.emit()                  # before any read/write attempt
        try:
            while not self._stop.is_set():
                chunk = self._ser.read(self._ser.in_waiting or 1)
                if not chunk:
                    continue
                rx += len(chunk)
                for msg in parser.feed(chunk):
                    self._dispatch(msg)
                self.stats.emit(rx, parser.errors)
        except (serial.SerialException, OSError) as e:
            self.error.emit(f"Serial error: {e}")
        finally:
            with self._lock:
                self._ser.close()
                self._ser = None

    def _dispatch(self, msg) -> None:
        if isinstance(msg, Spectrum):
            self.spectrum.emit(msg)
        elif isinstance(msg, SpectrumRf):
            self.spectrum_rf.emit(msg)
        elif isinstance(msg, ChannelUtil):
            self.ch_util.emit(msg)
        elif isinstance(msg, Status):
            self.status.emit(msg)

    def write(self, data: bytes) -> None:
        """Send bytes to the device (e.g. a CONFIG frame). Safe from GUI thread."""
        with self._lock:
            if self._ser is not None and self._ser.is_open:
                try:
                    self._ser.write(data)
                except (serial.SerialException, OSError) as e:
                    self.error.emit(f"Send error: {e}")

    def stop(self) -> None:
        self._stop.set()
        self.wait(1000)
