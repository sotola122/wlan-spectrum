"""Main window: top nav, three linked plot cards, right settings panel.

Visual language follows DESIGN.md (Cursor): cream canvas, white hairline
cards, warm ink text, Cursor Orange only on primary CTAs, mono numerics.
"""

from __future__ import annotations

import json
import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QMainWindow, QPushButton, QScrollArea, QSlider,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import theme, tlv
from .bands import BAND_24, BAND_5, BANDS, channel_freq
from .mock import MODE_LIVE, MODE_SWEEP, MockSource
from .serial_link import SerialReader, available_ports
from .theme import C

theme.configure_pyqtgraph()

WATERFALL_ROWS = 200

# Backwards-compatible name used by __main__ in earlier versions
apply_dark_theme = theme.apply_theme


# ------------------------------------------------------------------ small widgets
class Segmented(QFrame):
    """Quiet hairline segmented control (exclusive), API similar to QTabBar."""

    currentChanged = Signal(int)

    def __init__(self, labels: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("segment")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)
        self._group = QButtonGroup(self)
        for i, text in enumerate(labels):
            b = QPushButton(text)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            self._group.addButton(b, i)
            lay.addWidget(b)
        self._group.button(0).setChecked(True)
        self._idx = 0
        self._group.idClicked.connect(self.setCurrentIndex)
        self.setFixedHeight(34)

    def currentIndex(self) -> int:
        return self._idx

    def button(self, i: int) -> QPushButton:
        return self._group.button(i)

    def setCurrentIndex(self, i: int) -> None:
        self._group.button(i).setChecked(True)
        if i != self._idx:
            self._idx = i
            self.currentChanged.emit(i)


def _vsep() -> QFrame:
    f = QFrame()
    f.setObjectName("vsep")
    f.setFixedHeight(24)
    return f


def _labeled(text: str, w: QWidget) -> QWidget:
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    lab = QLabel(text)
    lab.setObjectName("dim")
    lay.addWidget(lab)
    lay.addWidget(w)
    return box


def _value(text: str = "-") -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("value")
    lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lab


def _legend_chip(color: str, text: str) -> QLabel:
    lab = QLabel(f'<span style="color:{color}; font-size:11pt;">━</span>&nbsp;{text}')
    lab.setObjectName("legend")
    return lab


class PlotCard(QFrame):
    """White card with a header row (title, caption, extras) and a plot."""

    def __init__(self, title: str, caption: str) -> None:
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 10)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(12)
        t = QLabel(title)
        t.setObjectName("cardTitle")
        cap = QLabel(caption)
        cap.setObjectName("caption")
        head.addWidget(t)
        head.addWidget(cap)
        head.addStretch(1)
        self.head = head
        lay.addLayout(head)
        self.view = pg.PlotWidget()
        self.view.setFrameShape(QFrame.NoFrame)
        self.plot: pg.PlotItem = self.view.getPlotItem()
        theme.style_plot(self.plot)
        lay.addWidget(self.view, 1)

    def add_header_widget(self, w: QWidget) -> None:
        self.head.addWidget(w)


# ------------------------------------------------------------------ window
class MainWindow(QMainWindow):
    def __init__(self, start_demo: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("ESP32-C5 Wi-Fi Spectrum Analyzer")
        self.resize(1480, 940)

        self.source = None              # SerialReader | MockSource | None
        self.mode = MODE_LIVE
        self.band = BAND_24
        self.playing = False
        self.sweeps_done = 0
        self._dirty = False
        self._frames, self._fps, self._fps_t = 0, 0.0, time.monotonic()
        self._util: dict[int, int] = {}
        self._ch_lines: list[pg.InfiniteLine] = []

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_topbar())
        body = QHBoxLayout()
        body.setContentsMargins(20, 20, 20, 16)
        body.setSpacing(20)
        body.addWidget(self._build_plots(), 1)
        body.addWidget(self._build_right_panel())
        root.addLayout(body, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Disconnected — click Demo for mock data, or choose a COM port and Connect")

        self._set_band(BAND_24)
        self._on_mode_changed(0)

        self._render_timer = QTimer(self)        # throttle redraws to ~30 fps
        self._render_timer.timeout.connect(self._render)
        self._render_timer.start(33)

        if start_demo:
            self.demo_btn.setChecked(True)

    # ============================================================ top nav
    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(64)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(8)

        dot = QLabel("●")
        dot.setObjectName("brandDot")
        word = QLabel("Spectrum")
        word.setObjectName("wordmark")
        word.setToolTip("ESP32-C5 Wi-Fi Spectrum Analyzer")
        lay.addWidget(dot)
        lay.addWidget(word)
        lay.addSpacing(8)

        self.mode_tabs = Segmented(["Live", "Band Sweep"])
        self.mode_tabs.currentChanged.connect(self._on_mode_changed)
        lay.addWidget(self.mode_tabs)

        self.band_seg = Segmented([BANDS[BAND_24].name, BANDS[BAND_5].name])
        self.band_seg.currentChanged.connect(self._set_band)
        lay.addWidget(self.band_seg)
        lay.addWidget(_vsep())

        self.play_btn = QPushButton("▶  Start")
        self.play_btn.setObjectName("primary")
        self.play_btn.setCheckable(True)
        self.play_btn.setMinimumWidth(92)
        self.play_btn.toggled.connect(self._on_play_toggled)
        lay.addWidget(self.play_btn)

        self.sweep_time = QSpinBox()
        self.sweep_time.setRange(100, 10000)
        self.sweep_time.setSingleStep(100)
        self.sweep_time.setValue(1000)
        self.sweep_time.setSuffix(" ms")
        self.sweep_time.setFixedWidth(104)
        self.sweep_time.valueChanged.connect(self._send_config)
        lay.addWidget(_labeled("Sweep", self.sweep_time))

        self.sweep_count = QSpinBox()
        self.sweep_count.setRange(0, 9999)
        self.sweep_count.setSpecialValueText("∞")
        self.sweep_count.setFixedWidth(76)
        self.sweep_count.valueChanged.connect(self._update_sweep_label)
        lay.addWidget(_labeled("Count", self.sweep_count))
        self.sweep_lbl = QLabel("0 / ∞")
        self.sweep_lbl.setObjectName("badge")
        self.sweep_lbl.setFixedHeight(20)
        lay.addWidget(self.sweep_lbl)

        lay.addStretch(1)

        self.demo_btn = QPushButton("Demo")
        self.demo_btn.setCheckable(True)
        self.demo_btn.setToolTip("Generate a mock spectrum without hardware")
        self.demo_btn.toggled.connect(self._on_demo_toggled)
        lay.addWidget(self.demo_btn)
        lay.addWidget(_vsep())

        self.port_combo = QComboBox()
        self.port_combo.setFixedWidth(124)
        self.port_combo.setEditable(True)        # allow typing e.g. /dev/pts/3
        self.port_combo.lineEdit().setPlaceholderText("COM port")
        self.port_combo.setToolTip("COM port (e.g. COM3, /dev/ttyUSB0)")
        lay.addWidget(self.port_combo)
        refresh = QPushButton("⟳")
        refresh.setObjectName("icon")
        refresh.setToolTip("Refresh port list")
        refresh.clicked.connect(self._refresh_ports)
        lay.addWidget(refresh)
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["115200", "460800", "921600", "2000000"])
        self.baud_combo.setCurrentText("921600")
        self.baud_combo.setFixedWidth(100)
        lay.addWidget(self.baud_combo)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setCheckable(True)
        self.connect_btn.setMinimumWidth(80)
        self.connect_btn.toggled.connect(self._on_connect_toggled)
        lay.addWidget(self.connect_btn)
        self._refresh_ports()
        return bar

    # ============================================================ plots
    def _build_plots(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        # 1) line spectrum
        self.spec_card = PlotCard("Spectrum", "POWER · dBm")
        sp = self.spec_plot = self.spec_card.plot
        self.readout = QLabel("")
        self.readout.setObjectName("legend")
        self.spec_card.add_header_widget(self.readout)
        self.spec_card.add_header_widget(_legend_chip(theme.SPECTRUM_LINE, "Current"))
        self.peak_legend = _legend_chip(theme.PEAK_LINE, "Peak Hold")
        self.spec_card.add_header_widget(self.peak_legend)
        theme.axis_label(sp, "left", "dBm")
        sp.setMouseEnabled(x=True, y=False)
        self.peak_curve = sp.plot(pen=pg.mkPen(theme.PEAK_LINE, width=1.2))
        self.cur_curve = sp.plot(pen=pg.mkPen(theme.SPECTRUM_LINE, width=1.6),
                                 fillLevel=-200, brush=pg.mkBrush(*theme.SPECTRUM_FILL))
        self.vline = pg.InfiniteLine(angle=90, pen=pg.mkPen(C["muted_soft"], style=Qt.DashLine))
        sp.addItem(self.vline, ignoreBounds=True)
        sp.scene().sigMouseMoved.connect(self._on_mouse_moved)
        lay.addWidget(self.spec_card, 4)

        # 2) waterfall
        self.wf_card = PlotCard("Waterfall", "TIME × FREQUENCY")
        wp = self.wf_plot = self.wf_card.plot
        theme.axis_label(wp, "left", "Frames")
        wp.invertY(True)                         # newest row at the top
        wp.setMouseEnabled(x=True, y=False)
        wp.showGrid(x=False, y=False)
        wp.setXLink(sp)
        self.wf_img = pg.ImageItem()
        self.wf_img.setLookupTable(theme.waterfall_cmap().getLookupTable(nPts=256))
        wp.addItem(self.wf_img)
        lay.addWidget(self.wf_card, 4)

        # 3) channel utilisation bars
        self.util_card = PlotCard("Channel Utilization", "UTILIZATION · %")
        for col, text in ((C["mint"], "&lt; 40"), (C["gold"], "40–70"), (C["error"], "≥ 70")):
            self.util_card.add_header_widget(_legend_chip(col, text))
        up = self.util_plot = self.util_card.plot
        theme.axis_label(up, "left", "%")
        theme.axis_label(up, "bottom", "Channel")
        up.setYRange(0, 105, padding=0)
        up.setMouseEnabled(x=True, y=False)
        up.showGrid(x=False, y=True, alpha=0.12)
        up.setXLink(sp)
        self.util_bars = pg.BarGraphItem(x=[], height=[], width=1)
        up.addItem(self.util_bars)
        self.util_texts: list[pg.TextItem] = []
        lay.addWidget(self.util_card, 3)
        return box

    # ============================================================ right panel
    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("panel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(4)

        # channel list
        g = QGroupBox("CHANNELS")
        gl = QVBoxLayout(g)
        gl.setSpacing(10)
        self.ch_table = QTableWidget(0, 4)
        self.ch_table.setHorizontalHeaderLabels(["CH", "MHz", "Util", "Peak"])
        self.ch_table.verticalHeader().setVisible(False)
        self.ch_table.verticalHeader().setDefaultSectionSize(26)
        self.ch_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.ch_table.setShowGrid(False)
        self.ch_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.ch_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ch_table.setAlternatingRowColors(True)
        self.ch_table.setFocusPolicy(Qt.NoFocus)
        self.ch_table.setMinimumHeight(200)
        self.ch_table.setFont(theme.mono_font(9))
        self.ch_table.cellClicked.connect(self._zoom_to_channel)
        gl.addWidget(self.ch_table)
        full = QPushButton("Show Full Band")
        full.clicked.connect(self._reset_x)
        gl.addWidget(full)
        lay.addWidget(g, 1)

        # acquisition (placeholders, sent to the device as CONFIG)
        g = QGroupBox("ACQUISITION  ·  PROVISIONAL")
        fl = QFormLayout(g)
        fl.setVerticalSpacing(10)
        self.fft_combo = QComboBox()
        self.fft_combo.addItems(["64", "128", "256", "512", "1024"])
        self.fft_combo.currentTextChanged.connect(self._send_config)
        fl.addRow("FFT size", self.fft_combo)
        self.sr_combo = QComboBox()
        self.sr_combo.addItems(["20 MS/s", "40 MS/s"])
        self.sr_combo.currentTextChanged.connect(self._send_config)
        fl.addRow("Sample rate", self.sr_combo)
        self.rbw_lbl = _value()
        fl.addRow("RBW", self.rbw_lbl)
        lay.addWidget(g)

        # display toggles
        g = QGroupBox("DISPLAY")
        vl = QVBoxLayout(g)
        vl.setSpacing(6)
        self.peak_chk = QCheckBox("Peak Hold")
        self.wf_chk = QCheckBox("Waterfall")
        self.ch_chk = QCheckBox("Channel Display")
        for c in (self.peak_chk, self.wf_chk, self.ch_chk):
            c.setChecked(True)
            vl.addWidget(c)
        self.peak_chk.toggled.connect(self.peak_curve.setVisible)
        self.peak_chk.toggled.connect(self.peak_legend.setVisible)
        self.wf_chk.toggled.connect(self._toggle_waterfall)
        self.ch_chk.toggled.connect(self._toggle_channels)
        vl.addSpacing(4)
        reset = QPushButton("Reset Peak")
        reset.clicked.connect(self._reset_peak)
        vl.addWidget(reset)
        lay.addWidget(g)

        # dB range
        g = QGroupBox("dB RANGE")
        fl = QFormLayout(g)
        fl.setVerticalSpacing(12)
        self.db_max = QSlider(Qt.Horizontal)
        self.db_max.setRange(-70, 0)
        self.db_max.setValue(-20)
        self.db_min = QSlider(Qt.Horizontal)
        self.db_min.setRange(-130, -60)
        self.db_min.setValue(-105)
        self.db_max_lbl, self.db_min_lbl = _value(), _value()
        for s, lab, name in ((self.db_max, self.db_max_lbl, "Max"), (self.db_min, self.db_min_lbl, "Min")):
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(s, 1)
            lab.setMinimumWidth(72)
            lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(lab)
            fl.addRow(name, row)
            s.valueChanged.connect(self._apply_db_range)
        lay.addWidget(g)

        # status
        g = QGroupBox("STATUS")
        fl = QFormLayout(g)
        fl.setVerticalSpacing(8)
        self.conn_lbl = QLabel()
        self.conn_lbl.setObjectName("badge")
        self.conn_lbl.setFixedHeight(20)
        self._set_conn("Disconnected")
        self.rx_lbl = _value("0 B")
        self.fps_lbl = _value("0.0 fps")
        self.err_lbl = _value("0")
        fl.addRow("Link", self.conn_lbl)
        fl.addRow("Received", self.rx_lbl)
        fl.addRow("Update", self.fps_lbl)
        fl.addRow("TLV errors", self.err_lbl)
        self.dev_lbl = QLabel("—")
        self.dev_lbl.setObjectName("device")
        self.dev_lbl.setWordWrap(True)
        fl.addRow(self.dev_lbl)
        lay.addWidget(g)

        self._apply_db_range()
        self._update_rbw()
        scroll = QScrollArea()                   # keeps the panel usable on small screens
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setFixedWidth(330)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return scroll

    def _set_conn(self, text: str, state: str = "") -> None:
        self.conn_lbl.setText(text)
        self.conn_lbl.setProperty("state", state)
        self.conn_lbl.style().unpolish(self.conn_lbl)
        self.conn_lbl.style().polish(self.conn_lbl)

    # ============================================================ band / mode
    def _set_band(self, band: int) -> None:
        self.band = band
        info = BANDS[band]
        self.freqs = np.linspace(info.f_start, info.f_stop, info.n_points)
        floor = float(self.db_min.value())
        self.cur = np.full(info.n_points, floor, dtype=np.float32)
        self.peak = np.full(info.n_points, -200.0, dtype=np.float32)
        self.wf = np.full((WATERFALL_ROWS, info.n_points), floor, dtype=np.float32)
        self._wf_rect = QRectF(info.f_start, 0, info.f_stop - info.f_start, WATERFALL_ROWS)
        self.wf_img.setImage(self.wf, autoLevels=False, levels=(floor, self.db_max.value()))
        self.wf_img.setRect(self._wf_rect)
        self._util = {}
        self.sweeps_done = 0
        self._update_sweep_label()

        for p in (self.spec_plot, self.wf_plot, self.util_plot):
            p.setLimits(xMin=info.f_start, xMax=info.f_stop)
        self.wf_plot.setYRange(0, WATERFALL_ROWS, padding=0)
        self._reset_x()

        # channel markers on the spectrum + channel ticks on the bar axis
        for ln in self._ch_lines:
            self.spec_plot.removeItem(ln)
        self._ch_lines = []
        ticks = []
        for ch in info.channels:
            f = channel_freq(band, ch)
            ln = pg.InfiniteLine(
                f, angle=90, pen=pg.mkPen(C["hairline"], style=Qt.DotLine), label=str(ch),
                labelOpts=dict(position=0.97, color=C["muted"], anchors=[(0.5, 0), (0.5, 0)]))
            ln.label.setFont(theme.mono_font(7.5))
            ln.setVisible(self.ch_chk.isChecked())
            self.spec_plot.addItem(ln, ignoreBounds=True)
            self._ch_lines.append(ln)
            ticks.append((f, str(ch)))
        step = 20 if band == BAND_24 else 50
        mhz_ticks = [(f, f"{f:.0f}") for f in np.arange(np.ceil(info.f_start / step) * step,
                                                        info.f_stop + 1, step)]
        self.util_plot.getAxis("bottom").setTicks([ticks, []])
        self.spec_plot.getAxis("bottom").setTicks([mhz_ticks, []])
        self.wf_plot.getAxis("bottom").setTicks([mhz_ticks, []])

        self.ch_table.setRowCount(len(info.channels))
        for r, ch in enumerate(info.channels):
            for c, txt in enumerate((str(ch), f"{channel_freq(band, ch):.0f}", "—", "—")):
                it = QTableWidgetItem(txt)
                it.setTextAlignment(Qt.AlignCenter)
                self.ch_table.setItem(r, c, it)
        self._update_util_bars()
        self._send_config()
        self._dirty = True

    def _on_mode_changed(self, idx: int) -> None:
        self.mode = MODE_SWEEP if idx == 1 else MODE_LIVE
        sweep = self.mode == MODE_SWEEP
        self.sweep_time.setEnabled(sweep)
        self.sweep_count.setEnabled(sweep)
        self.sweeps_done = 0
        self._update_sweep_label()
        self._send_config()

    def _reset_x(self) -> None:
        info = BANDS[self.band]
        self.spec_plot.setXRange(info.f_start, info.f_stop, padding=0)

    def _zoom_to_channel(self, row: int, _col: int) -> None:
        ch = BANDS[self.band].channels[row]
        f = channel_freq(self.band, ch)
        half = 30 if self.band == BAND_24 else 60
        self.spec_plot.setXRange(f - half, f + half, padding=0)

    # ============================================================ sources
    def _refresh_ports(self) -> None:
        cur = self.port_combo.currentText()
        self.port_combo.clear()
        self.port_combo.addItems(available_ports())
        if cur:
            self.port_combo.setCurrentText(cur)

    def _attach(self, src) -> None:
        src.spectrum.connect(self._on_spectrum)
        src.ch_util.connect(self._on_util)
        src.status.connect(self._on_status)
        src.error.connect(self._on_source_error)
        src.stats.connect(self._on_stats)
        self.source = src
        self._send_config()

    def _detach(self) -> None:
        if self.source is not None:
            src, self.source = self.source, None
            src.stop()
            for sig, slot in ((src.spectrum, self._on_spectrum), (src.ch_util, self._on_util),
                              (src.status, self._on_status), (src.error, self._on_source_error),
                              (src.stats, self._on_stats)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            src.deleteLater()
        self._set_conn("Disconnected")

    def _on_demo_toggled(self, on: bool) -> None:
        if on:
            if self.connect_btn.isChecked():
                self.connect_btn.setChecked(False)
            self._detach()
            src = MockSource(self)
            self._attach(src)
            src.start()
            self._set_conn("Demo (mock)", "demo")
            self.statusBar().showMessage("Demo mode: generating mock data")
            self.play_btn.setChecked(True)
        elif isinstance(self.source, MockSource):
            self._detach()
            self.play_btn.setChecked(False)
            self.statusBar().showMessage("Demo mode stopped")

    def _on_connect_toggled(self, on: bool) -> None:
        if on:
            port = self.port_combo.currentText().strip()
            if not port:
                self.statusBar().showMessage("Please select a COM port")
                self.connect_btn.setChecked(False)
                return
            if self.demo_btn.isChecked():
                self.demo_btn.setChecked(False)
            self._detach()
            src = SerialReader(port, int(self.baud_combo.currentText()), self)
            self._attach(src)
            src.start()
            self.connect_btn.setText("Disconnect")
            self._set_conn(f"{port} @ {self.baud_combo.currentText()}", "ok")
            self.statusBar().showMessage(f"Connected to {port}")
            self.play_btn.setChecked(True)
        else:
            if isinstance(self.source, SerialReader):
                self._detach()
                self.statusBar().showMessage("Disconnected")
            self.connect_btn.setText("Connect")

    def _on_source_error(self, msg: str) -> None:
        self.statusBar().showMessage(msg)
        if isinstance(self.sender(), SerialReader):
            self.connect_btn.setChecked(False)
            self._set_conn("Error", "err")

    def _send_config(self, *_):
        """Push current settings to the device as a CONFIG TLV (0x10)."""
        if not hasattr(self, "sr_combo"):
            return
        self._update_rbw()
        if self.source is None:
            return
        sr_khz = int(self.sr_combo.currentText().split()[0]) * 1000
        self.source.write(tlv.encode_config(self.mode, self.band, self.sweep_time.value(),
                                            int(self.fft_combo.currentText()), sr_khz))

    def _on_play_toggled(self, on: bool) -> None:
        self.playing = on
        self.play_btn.setText("Pause" if on else "▶  Start")
        if on and self.mode == MODE_SWEEP and self.sweep_count.value() and \
                self.sweeps_done >= self.sweep_count.value():
            self.sweeps_done = 0                 # restart a finished sweep run
            self._update_sweep_label()

    # ============================================================ data in
    def _on_spectrum(self, msg: tlv.Spectrum) -> None:
        if not self.playing or len(msg.dbm) < 2:
            return
        f = msg.freqs
        mask = (self.freqs >= f[0]) & (self.freqs <= f[-1])
        if not mask.any():
            return                               # frame for another band
        self.cur[mask] = np.interp(self.freqs[mask], f, msg.dbm)
        np.maximum(self.peak, self.cur, out=self.peak)
        self._frames += 1

        info = BANDS[self.band]
        if self.mode == MODE_LIVE:
            self._push_row()
        elif f[-1] + 2 * msg.f_step >= info.f_stop or f[-1] >= self.freqs[-1]:
            # last segment of the band arrived -> one sweep complete
            self._push_row()
            self.sweeps_done += 1
            self._update_sweep_label()
            limit = self.sweep_count.value()
            if limit and self.sweeps_done >= limit:
                self.play_btn.setChecked(False)
                self.statusBar().showMessage(f"Completed {limit} sweeps")
        self._dirty = True

    def _push_row(self) -> None:
        self.wf = np.roll(self.wf, 1, axis=0)
        self.wf[0] = self.cur

    def _on_util(self, msg: tlv.ChannelUtil) -> None:
        if msg.band != self.band or not self.playing:
            return
        self._util = dict(msg.util)
        self._update_util_bars()

    def _on_status(self, msg: tlv.Status) -> None:
        self.dev_lbl.setText(json.dumps(msg.data, ensure_ascii=False)[:200])

    def _on_stats(self, rx: int, errors: int) -> None:
        self.rx_lbl.setText(f"{rx / 1024:.1f} KiB" if rx > 1024 else f"{rx} B")
        self.err_lbl.setText(str(errors))

    # ============================================================ rendering
    def _render(self) -> None:
        now = time.monotonic()
        if now - self._fps_t >= 1.0:
            self._fps = self._frames / (now - self._fps_t)
            self._frames, self._fps_t = 0, now
            self.fps_lbl.setText(f"{self._fps:.1f} fps")
        if not self._dirty:
            return
        self._dirty = False
        self.cur_curve.setData(self.freqs, self.cur)
        self.peak_curve.setData(self.freqs, self.peak)
        if self.wf_chk.isChecked():
            self.wf_img.setImage(self.wf, autoLevels=False,
                                 levels=(self.db_min.value(), self.db_max.value()))
            self.wf_img.setRect(self._wf_rect)
        self._update_peak_column()

    def _update_util_bars(self) -> None:
        info = BANDS[self.band]
        xs = [channel_freq(self.band, ch) for ch in info.channels]
        hs = [self._util.get(ch, 0) for ch in info.channels]
        brushes = [pg.mkBrush(theme.util_color(h)) for h in hs]
        width = 3.6 if self.band == BAND_24 else 15
        self.util_bars.setOpts(x=xs, height=hs, width=width, brushes=brushes,
                               pen=pg.mkPen(None))
        # % labels above bars
        for t in self.util_texts:
            self.util_plot.removeItem(t)
        self.util_texts = []
        if self._util:
            font = theme.mono_font(7.5)
            for x, h in zip(xs, hs):
                t = pg.TextItem(f"{h}", color=C["body"], anchor=(0.5, 1))
                t.setFont(font)
                t.setPos(x, h)
                self.util_plot.addItem(t)
                self.util_texts.append(t)
        for r, ch in enumerate(info.channels):
            it = self.ch_table.item(r, 2)
            if it is not None:
                it.setText(f"{self._util[ch]} %" if ch in self._util else "—")

    def _update_peak_column(self) -> None:
        info = BANDS[self.band]
        for r, ch in enumerate(info.channels):
            fc = channel_freq(self.band, ch)
            m = np.abs(self.freqs - fc) <= 10
            it = self.ch_table.item(r, 3)
            if it is not None and m.any() and self.peak[m].max() > -199:
                it.setText(f"{self.peak[m].max():.0f}")

    # ============================================================ UI helpers
    def _apply_db_range(self) -> None:
        lo, hi = self.db_min.value(), self.db_max.value()
        self.db_min_lbl.setText(f"{lo} dBm")
        self.db_max_lbl.setText(f"{hi} dBm")
        self.spec_plot.setYRange(lo, hi, padding=0)
        self.wf_img.setLevels((lo, hi))
        self._dirty = True

    def _update_rbw(self) -> None:
        sr = int(self.sr_combo.currentText().split()[0])
        fft = int(self.fft_combo.currentText())
        self.rbw_lbl.setText(f"{sr * 1000 / fft:.1f} kHz")

    def _update_sweep_label(self, *_) -> None:
        limit = self.sweep_count.value()
        self.sweep_lbl.setText(f"{self.sweeps_done} / {limit if limit else '∞'}")

    def _reset_peak(self) -> None:
        self.peak[:] = -200
        self._dirty = True

    def _toggle_waterfall(self, on: bool) -> None:
        self.wf_card.setVisible(on)

    def _toggle_channels(self, on: bool) -> None:
        for ln in self._ch_lines:
            ln.setVisible(on)
        self.util_card.setVisible(on)

    def _on_mouse_moved(self, pos) -> None:
        vb = self.spec_plot.vb
        if not vb.sceneBoundingRect().contains(pos):
            self.readout.setText("")
            return
        pt = vb.mapSceneToView(pos)
        i = int(np.clip(np.searchsorted(self.freqs, pt.x()), 0, len(self.freqs) - 1))
        self.vline.setPos(self.freqs[i])
        self.readout.setText(f"{self.freqs[i]:.1f} MHz  ·  {self.cur[i]:.1f} dBm  ·  "
                             f"peak {self.peak[i]:.1f} dBm")

    def closeEvent(self, ev) -> None:
        self._detach()
        super().closeEvent(ev)
