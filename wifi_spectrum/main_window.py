"""Main window: top nav, three linked plot cards, right settings panel.

Visual language follows DESIGN.md (Cursor): cream canvas, white hairline
cards, warm ink text, Cursor Orange only on primary CTAs, mono numerics.
"""

from __future__ import annotations

import contextlib
import json
import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme, tlv
from .bands import BAND_5, BAND_24, BANDS, channel_freq
from .mock import MODE_LIVE, MODE_SWEEP, MockSource
from .monitor_data import SCHEMA as MONITOR_SCHEMA
from .monitor_data import UTIL_CONFIDENCE, UTIL_SOURCE, MonitorState
from .serial_link import SerialReader, available_ports
from .theme import C

theme.configure_pyqtgraph()

WATERFALL_ROWS = 200

# CONFIG resend while no matching ack has arrived: bounded, idempotent
# (latest tuple only), cancelled by the fresh matching ack, detach, demo,
# legacy fallback or source error. The post-ctrl gate log only showed a
# missing ack on run1 and a recovery on the identical rerun - cause not
# proven; the PTY test covers a device that loses its first CONFIG.
CFG_RETRY_MS = 500
CFG_MAX_RETRIES = 10

# Source-visible status lines (moved from the removed monitor screen)
WAITING_TEXT = "Waiting for configuration"
DISCONNECTED_TEXT = "Disconnected — no device data"

# Demo/legacy acquisition options; the RF path replaces these with the
# runtime spectrum_caps lists.
DEMO_FFT_SIZES = ["64", "128", "256", "512", "1024"]
DEMO_RATES = ["20 MS/s", "40 MS/s"]

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
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self._group.addButton(b, i)
            lay.addWidget(b)
        self._group.button(0).setChecked(True)
        self._idx = 0
        self._group.idClicked.connect(self.setCurrentIndex)
        self.setFixedHeight(34)

    def currentIndex(self) -> int:
        return self._idx

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
    lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
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
        self.title_lbl = t
        self.cap_lbl = cap
        head.addWidget(t)
        head.addWidget(cap)
        head.addStretch(1)
        self.head = head
        lay.addLayout(head)
        self.plot = pg.PlotItem()
        self.view = pg.PlotWidget(plotItem=self.plot)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
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
        # Explicit source/capability state (replaces the old stack-index
        # "is monitor view" check): none = no source, demo = MockSource,
        # serial = real port. RF mode = serial with a valid spectrum ack.
        self._source_kind = "none"
        self._legacy = False            # serial device streaming legacy 0x01
        self._rf_active = False         # current ack carries valid RF caps
        self._rf_covered = np.zeros(0, dtype=bool)
        self._rf_cycle: int | None = None
        self.monitor_state = MonitorState()
        self.mode = MODE_LIVE
        self.band = BAND_24
        self.playing = False
        self.sweeps_done = 0
        self._dirty = False
        self._frames, self._fps, self._fps_t = 0, 0.0, time.monotonic()
        self._util: dict[int, float] = {}   # ch -> percent (int in demo)
        # One bounded CONFIG-retry chain, owned by a single-shot QTimer:
        # re-armed with the latest requested tuple on every change, stopped
        # on ack/detach/demo/legacy/error (see _send_config/_retry_config).
        self._cfg_timer = QTimer(self)
        self._cfg_timer.setSingleShot(True)
        self._cfg_timer.timeout.connect(self._retry_config)
        self._cfg_attempts = 0
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
        self._update_acquisition_controls()
        self._update_power_units()

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
        self.port_combo.setEditable(True)        # COM3, /dev/ttyUSB0, or a Linux pty path
        self.port_combo.setPlaceholderText("COM port")
        self.port_combo.setToolTip("Serial port (Windows: COM3; Linux: /dev/ttyUSB0 or /dev/ttyACM0)")
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
        sp.getViewBox().setMouseEnabled(x=True, y=False)
        self.peak_curve = sp.plot(pen=pg.mkPen(theme.PEAK_LINE, width=1.2))
        self.cur_curve = sp.plot(pen=pg.mkPen(theme.SPECTRUM_LINE, width=1.6),
                                 fillLevel=-200, brush=pg.mkBrush(*theme.SPECTRUM_FILL))
        self.vline = pg.InfiniteLine(angle=90, pen=pg.mkPen(C["muted_soft"], style=Qt.PenStyle.DashLine))
        sp.addItem(self.vline, ignoreBounds=True)
        sp.scene().sigMouseMoved.connect(self._on_mouse_moved)
        lay.addWidget(self.spec_card, 4)

        # 2) waterfall
        self.wf_card = PlotCard("Waterfall", "TIME × FREQUENCY")
        wp = self.wf_plot = self.wf_card.plot
        theme.axis_label(wp, "left", "Frames")
        wp.getViewBox().invertY(True)               # newest row at the top
        wp.getViewBox().setMouseEnabled(x=True, y=False)
        wp.showGrid(x=False, y=False)
        wp.getViewBox().setXLink(sp.getViewBox())
        self.wf_img = pg.ImageItem()
        lut = theme.waterfall_cmap().getLookupTable(nPts=256)
        if not isinstance(lut, np.ndarray):
            raise RuntimeError("waterfall LUT did not interpolate to an array")
        self.wf_img.setLookupTable(lut)
        wp.addItem(self.wf_img)
        lay.addWidget(self.wf_card, 4)

        # 3) channel utilisation bars
        self.util_card = PlotCard("Channel Utilization", "UTILIZATION · %")
        for col, text in ((C["mint"], "&lt; 40"), (C["gold"], "40–70"), (C["error"], "≥ 70")):
            self.util_card.add_header_widget(_legend_chip(col, text))
        up = self.util_plot = self.util_card.plot
        theme.axis_label(up, "left", "%")
        theme.axis_label(up, "bottom", "Channel")
        up.getViewBox().setYRange(0, 105, padding=0)
        up.getViewBox().setMouseEnabled(x=True, y=False)
        up.showGrid(x=False, y=True, alpha=0.12)
        up.getViewBox().setXLink(sp.getViewBox())
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
        self.channel_group = g
        gl = QVBoxLayout(g)
        gl.setSpacing(10)
        self.ch_table = QTableWidget(0, 4)
        self.ch_table.setHorizontalHeaderLabels(["CH", "MHz", "Util", "Peak"])
        self.ch_table.verticalHeader().setVisible(False)
        self.ch_table.verticalHeader().setDefaultSectionSize(26)
        self.ch_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.ch_table.setShowGrid(False)
        self.ch_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ch_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.ch_table.setAlternatingRowColors(True)
        self.ch_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        self.peak_reset_btn = reset
        vl.addWidget(reset)
        lay.addWidget(g)

        # dB range
        g = QGroupBox("dB RANGE")
        fl = QFormLayout(g)
        fl.setVerticalSpacing(12)
        self.db_max = QSlider(Qt.Orientation.Horizontal)
        self.db_max.setRange(-70, 0)
        self.db_max.setValue(-20)
        self.db_min = QSlider(Qt.Orientation.Horizontal)
        self.db_min.setRange(-130, -60)
        self.db_min.setValue(-105)
        self.db_max_lbl, self.db_min_lbl = _value(), _value()
        for s, lab, name in ((self.db_max, self.db_max_lbl, "Max"), (self.db_min, self.db_min_lbl, "Min")):
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(s, 1)
            lab.setMinimumWidth(72)
            lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
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
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setFixedWidth(330)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return scroll

    def _set_conn(self, text: str, state: str = "") -> None:
        self.conn_lbl.setText(text)
        self.conn_lbl.setProperty("state", state)
        self.conn_lbl.style().unpolish(self.conn_lbl)
        self.conn_lbl.style().polish(self.conn_lbl)

    # ============================================================ band / mode
    def _init_display_arrays(self) -> None:
        """(Re)initialise cur/peak/waterfall/coverage for the current
        source and band. Real path: NaN everywhere until measured - gaps
        stay gaps. Demo/legacy: the original floor background. Also
        clears coverage, RF cycle tracking and utilization so no state
        crosses a source, band, epoch or capability transition."""
        info = BANDS[self.band]
        floor = float(self.db_min.value())
        real = self._real_path()
        fill = np.nan if real else floor
        self.cur = np.full(info.n_points, fill, dtype=np.float32)
        self.peak = np.full(info.n_points, np.nan if real else -200.0,
                            dtype=np.float32)
        self.wf = np.full((WATERFALL_ROWS, info.n_points), fill,
                          dtype=np.float32)
        self._rf_covered = np.zeros(info.n_points, dtype=bool)
        self._rf_cycle = None
        self._util = {}
        self._update_util_bars()
        self._dirty = True

    def _set_band(self, band: int) -> None:
        self.band = band
        info = BANDS[band]
        self.freqs = np.linspace(info.f_start, info.f_stop, info.n_points)
        floor = float(self.db_min.value())
        self._init_display_arrays()
        self._wf_rect = QRectF(info.f_start, 0, info.f_stop - info.f_start, WATERFALL_ROWS)
        self.wf_img.setImage(self.wf, autoLevels=False, levels=(floor, self.db_max.value()))
        self.wf_img.setRect(self._wf_rect)
        self.sweeps_done = 0
        self._update_sweep_label()

        for p in (self.spec_plot, self.wf_plot, self.util_plot):
            p.getViewBox().setLimits(xMin=info.f_start, xMax=info.f_stop)
        self.wf_plot.getViewBox().setYRange(0, WATERFALL_ROWS, padding=0)
        self._reset_x()

        # channel markers on the spectrum + channel ticks on the bar axis
        for ln in self._ch_lines:
            self.spec_plot.removeItem(ln)
        self._ch_lines = []
        ticks = []
        for ch in info.channels:
            f = channel_freq(band, ch)
            ln = pg.InfiniteLine(
                f, angle=90, pen=pg.mkPen(C["hairline"], style=Qt.PenStyle.DotLine), label=str(ch),
                labelOpts={"position": 0.97, "color": C["muted"], "anchors": [(0.5, 0), (0.5, 0)]})
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
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.ch_table.setItem(r, c, it)
        self._update_util_bars()
        if self._monitor_view():
            self._reset_monitor(WAITING_TEXT)
        self._update_power_units()
        self._send_config()
        self._dirty = True

    def _on_mode_changed(self, idx: int) -> None:
        self.mode = MODE_SWEEP if idx == 1 else MODE_LIVE
        self.sweeps_done = 0
        self._update_sweep_label()
        self._update_timing_controls()
        if self._monitor_view():
            self._reset_monitor(WAITING_TEXT)
        self._send_config()

    def _reset_x(self) -> None:
        info = BANDS[self.band]
        self.spec_plot.getViewBox().setXRange(info.f_start, info.f_stop, padding=0)

    def _zoom_to_channel(self, row: int, _col: int) -> None:
        ch = BANDS[self.band].channels[row]
        f = channel_freq(self.band, ch)
        half = 30 if self.band == BAND_24 else 60
        self.spec_plot.getViewBox().setXRange(f - half, f + half, padding=0)

    # ============================================================ sources
    def _is_serial(self) -> bool:
        return self._source_kind == "serial"

    def _monitor_view(self) -> bool:
        """Serial source speaking the wifi-monitor/1 contract (not a legacy
        0x01 device). Replaces the old stack-index check: config echoes,
        dwell timing and capability sync all bind through this state."""
        return self._source_kind == "serial" and not self._legacy

    def _real_path(self) -> bool:
        """The real path renders measurements only: buffers stay NaN where
        nothing has been measured (no fake floor, no zero-fill)."""
        return self._source_kind == "serial" and not self._legacy

    def _power_unit(self) -> str:
        return "dBFS" if self._rf_active else "dBm"

    def _legacy_fallback(self) -> None:
        """Old serial firmware emitting demo-style 0x01/0x02 frames: keep
        the shared plots, render like Demo, never enter RF mode."""
        self._cfg_timer.stop()          # no monitor ack is coming now
        if self._legacy:
            return
        self._legacy = True
        self._rf_active = False
        self._init_display_arrays()          # demo-style floor background
        self._update_acquisition_controls()
        self._update_power_units()
        self.statusBar().showMessage(
            "Device streams legacy spectrum frames; showing standard plots")

    def _set_combo_items(self, combo, items: list[str],
                         select: str | None) -> None:
        """Rebuild a combo from capability/demo options without emitting
        spurious CONFIG frames (selection follows the device's effective
        values, not a new user request)."""
        if ([combo.itemText(i) for i in range(combo.count())] == items):
            if select and combo.currentText() != select and select in items:
                combo.blockSignals(True)
                combo.setCurrentText(select)
                combo.blockSignals(False)
            return
        current = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(items)
        want = select if select in items else (
            current if current in items else (items[0] if items else ""))
        if want:
            combo.setCurrentText(want)
        combo.blockSignals(False)

    def _update_acquisition_controls(self) -> None:
        """FFT size / sample rate are ACQUISITION controls: bound to the
        runtime spectrum_caps lists when RF is proven, Demo-like for the
        mock and legacy paths, and honestly disabled for baseline
        monitor-only firmware that has no RF at all. Display controls
        (peak/waterfall/markers/range/zoom) are never gated."""
        if self._rf_active:
            caps = self.monitor_state.spectrum_caps or {}
            eff = self.monitor_state.spectrum_effective or {}
            sizes = [str(s) for s in caps.get("fft_sizes", [])]
            rates = [f"{int(r['span_khz']) // 1000} MS/s"
                     for r in caps.get("rate_codes", [])]
            self._set_combo_items(self.fft_combo, sizes,
                                  str(eff.get("fft_size", "")))
            self._set_combo_items(
                self.sr_combo, rates,
                f"{int(eff.get('span_khz', 0)) // 1000} MS/s")
            enabled = True
        elif self._monitor_view():
            enabled = False               # monitor-only FW: no FFT support
        else:                              # demo, legacy, or no source
            self._set_combo_items(self.fft_combo, list(DEMO_FFT_SIZES), None)
            self._set_combo_items(self.sr_combo, list(DEMO_RATES), None)
            enabled = True
        self.fft_combo.setEnabled(enabled)
        self.sr_combo.setEnabled(enabled)
        self._update_timing_controls()
        self._update_rbw()

    def _update_timing_controls(self) -> None:
        # Monitor/RF mode: sweep time is the hopping interval in Live too.
        self.sweep_time.setEnabled(self._monitor_view()
                                   or self.mode == MODE_SWEEP)
        self.sweep_count.setEnabled(self.mode == MODE_SWEEP)

    def _reset_monitor(self, message: str = WAITING_TEXT) -> None:
        self.monitor_state.reset()
        self._rf_active = False
        self.dev_lbl.setText(message)
        self._update_acquisition_controls()
        self._update_power_units()

    def _refresh_ports(self) -> None:
        cur = self.port_combo.currentText()
        self.port_combo.clear()
        self.port_combo.addItems(available_ports())
        if cur:
            self.port_combo.setCurrentText(cur)

    def _attach(self, src) -> None:
        src.spectrum.connect(self._on_spectrum)
        src.spectrum_rf.connect(self._on_spectrum_rf)
        src.ch_util.connect(self._on_util)
        src.status.connect(self._on_status)
        src.error.connect(self._on_source_error)
        src.stats.connect(self._on_stats)
        self.source = src
        if isinstance(src, SerialReader):
            # The initial CONFIG must wait until the port is actually open;
            # a write before open is silently dropped (no timer workarounds).
            src.opened.connect(self._send_config)
            self._source_kind = "serial"
            self._legacy = False
            self._rf_active = False
            self._reset_monitor(WAITING_TEXT)
            self._init_display_arrays()      # NaN: no fake floor, no mock data
        else:
            self._source_kind = "demo"
            self._legacy = False
            self._rf_active = False
            self.monitor_state.reset()
            self._init_display_arrays()      # clears any real-path traces
            self._update_acquisition_controls()
            self._update_power_units()
            self._send_config()            # MockSource writes immediately

    def _detach(self) -> None:
        self._cfg_timer.stop()          # no retry may outlive this source
        self._cfg_attempts = 0
        was_serial = self._source_kind == "serial"
        if self.source is not None:
            src, self.source = self.source, None
            src.stop()
            for sig, slot in ((src.spectrum, self._on_spectrum),
                              (src.spectrum_rf, self._on_spectrum_rf),
                              (src.ch_util, self._on_util),
                              (src.status, self._on_status),
                              (src.error, self._on_source_error),
                              (src.stats, self._on_stats)):
                with contextlib.suppress(RuntimeError, TypeError):
                    sig.disconnect(slot)
            if isinstance(src, SerialReader):
                with contextlib.suppress(RuntimeError, TypeError):
                    src.opened.disconnect(self._send_config)
            src.deleteLater()
        self._set_conn("Disconnected")
        if was_serial:
            self._init_display_arrays()     # blank the real path fully
        self._reset_monitor(DISCONNECTED_TEXT)
        self._source_kind = "none"
        self._legacy = False
        self._rf_active = False

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
            self._cfg_timer.stop()      # dead source: cancel the chain
            self.connect_btn.setChecked(False)
            self._set_conn("Error", "err")

    def _send_config(self, *_):
        """Push current settings to the device as a CONFIG TLV (0x10)."""
        if not hasattr(self, "sr_combo"):
            return
        self._update_rbw()
        if self.source is None:
            return
        fields = (self.mode, self.band, self.sweep_time.value(),
                  int(self.fft_combo.currentText()),
                  int(self.sr_combo.currentText().split()[0]) * 1000)
        if self._monitor_view():
            # an echo of these five fields is the device's acknowledgement
            self.monitor_state.request(*fields)
        self.source.write(tlv.encode_config(*fields))
        if self._monitor_view() and isinstance(self.source, SerialReader):
            # one bounded resend chain: start() re-arms with the LATEST
            # tuple; stop() on ack/detach/demo/legacy/error cancels
            self._cfg_attempts = 0
            self._cfg_timer.start(CFG_RETRY_MS)
        else:
            self._cfg_timer.stop()

    def _retry_config(self) -> None:
        """Bounded resend of the latest CONFIG until a fresh matching ack.
        A single owned QTimer is re-armed with the latest requested tuple
        (stop/re-arm on every change or detach), and the chain ends itself
        after CFG_MAX_RETRIES with a visible failure."""
        if not (isinstance(self.source, SerialReader)
                and self._monitor_view()
                and not self.monitor_state.ready):
            return                      # stale chain: never resends
        if self._cfg_attempts >= CFG_MAX_RETRIES:
            self.statusBar().showMessage(
                f"No configuration acknowledgement after "
                f"{CFG_MAX_RETRIES} retries - device not responding?")
            return                      # single-shot: the chain ends here
        fields = self.monitor_state.requested
        if fields is None:
            return
        self._cfg_attempts += 1
        self.source.write(tlv.encode_config(*fields))   # idempotent tuple
        self._cfg_timer.start(CFG_RETRY_MS)

    def _on_play_toggled(self, on: bool) -> None:
        self.playing = on
        self.play_btn.setText("Pause" if on else "▶  Start")
        if on and self.mode == MODE_SWEEP and self.sweep_count.value() and \
                self.sweeps_done >= self.sweep_count.value():
            self.sweeps_done = 0                 # restart a finished sweep run
            self._update_sweep_label()

    # ============================================================ data in
    def _on_spectrum(self, msg: tlv.Spectrum) -> None:
        if self._rf_active:
            return                   # RF mode never falls back on 0x01
        if self._is_serial():
            self._legacy_fallback()
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

    def _on_spectrum_rf(self, msg: tlv.SpectrumRf) -> None:
        """0x04 SPECTRUM_RF: live mode renders each frame immediately;
        sweep mode stages in MonitorState until the STATUS cycle marker
        (handled in _on_monitor_status)."""
        if not self._rf_active or not self.playing:
            return
        if not self.monitor_state.accept_rf(msg):
            return                   # stale epoch/band or a closed cycle
        self._rf_apply_frames([msg])
        self._push_row()             # live: one waterfall row per frame

    def _sync_util_from_state(self) -> None:
        """Rebuild the display map from MonitorState's published util set
        (live per channel, sweep flushed at the marker, gaps on missing/
        failed/capability change). Float percent keeps the raw fraction for
        bars/data; labels round only at formatting time."""
        self._util = {ch: 100.0 * s["busy"] / s["total"]
                      for ch, s in self.monitor_state.util_samples.items()}
        self._update_util_bars()

    def _on_util(self, msg: tlv.ChannelUtil) -> None:
        # RF mode takes utilization ONLY from the epoch'd STATUS channel
        # envelope (contract: Sampled PHY CCA). A 0x02 carries no epoch and
        # can never be this device's actual source - reject ALL of them so
        # an unproven frame cannot overwrite measured util. Demo and an
        # established legacy 0x01 stream keep the original 0x02 behavior.
        if self._rf_active:
            return
        if self._source_kind == "serial":
            render = (self._legacy and self.playing
                      and msg.band == self.band)
        else:                              # demo: original behavior
            render = self.playing and msg.band == self.band
        if not render:
            return
        self._util = dict(msg.util)
        self._update_util_bars()

    def _on_status(self, msg: tlv.Status) -> None:
        data = msg.data
        if isinstance(data, dict) and data.get("schema") == MONITOR_SCHEMA:
            self._on_monitor_status(data)
            return
        self.dev_lbl.setText(json.dumps(data, ensure_ascii=False)[:200])

    def _on_monitor_status(self, data: dict) -> None:
        event = data.get("event")
        if event in ("channel", "cycle") and not self.playing:
            return          # paused: measurements are gated before acceptance;
                            # config/error status still processes while paused
        old_epoch = self.monitor_state.epoch
        try:
            changed = self.monitor_state.accept(data)
        except ValueError as exc:
            self.statusBar().showMessage(f"Rejected monitor payload: {exc}")
            return
        if not changed and event != "channel_error":
            # a repeated channel error still invalidates published util
            return
        if event == "config":
            self._cfg_timer.stop()   # fresh matching ack cancels the chain
            self._sync_rf_state(old_epoch)
            self._update_dev_label(data)
        elif event == "cycle" and self.mode == MODE_SWEEP:
            self.sweeps_done += 1
            self._update_sweep_label()
            limit = self.sweep_count.value()
            if limit and self.sweeps_done >= limit:
                self.play_btn.setChecked(False)
                self.statusBar().showMessage(f"Completed {limit} sweeps")
        elif event in ("error", "channel_error"):
            # device-reported faults stay visible without the old widget
            self.statusBar().showMessage(
                f"Device error: {self.monitor_state.last_error}")
        if event in ("config", "channel", "cycle", "channel_error"):
            # the display is rebuilt from MonitorState's published util set:
            # live per channel, sweep flushed at the marker, gaps on
            # missing/failed/capability change - never a stale percent
            self._sync_util_from_state()
        if event == "cycle" and self._rf_active:
            # The STATUS cycle marker is the ONLY cycle authority for RF
            # data (never a frequency-position heuristic): staged sweep
            # frames flush here, then unclosed points become gaps.
            flushed = self.monitor_state.rf_flushed
            if flushed:
                self._rf_apply_frames(flushed)
            # Close/mask the cycle BEFORE copying into waterfall history,
            # so a row never contains another cycle's leftover values.
            self._rf_close_cycle()
            if flushed or self.mode == MODE_SWEEP:
                # exactly one history row per completed sweep cycle - an
                # all-NaN row honestly records a cycle with no captures
                self._push_row()

    def _sync_rf_state(self, old_epoch: int | None) -> None:
        """Re-derive RF mode from the current ack and clear every buffer
        that could carry data across an epoch or capability boundary."""
        was_active = self._rf_active
        self._rf_active = self.monitor_state.rf_ready
        if self._rf_active != was_active:
            if self._rf_active:
                self._legacy = False   # capability ack supersedes legacy claim
            self._init_display_arrays()
        if old_epoch is not None and self.monitor_state.epoch != old_epoch:
            self._init_display_arrays()   # new epoch: no stale traces
        self._update_acquisition_controls()
        self._update_power_units()

    def _update_dev_label(self, data: dict) -> None:
        st = self.monitor_state
        parts = [str(data.get("fw", "?")), str(data.get("chip", "?")),
                 f"epoch {st.epoch}", f"dwell {st.dwell_ms} ms"]
        if st.rf_ready:
            eff = st.spectrum_effective or {}
            span = int(eff.get("span_khz", 0))
            parts.append(
                f"RF FFT {eff.get('fft_size')} @ {span / 1000:.0f} MS/s")
        else:
            parts.append("spectrum n/a")
        parts.append("util available" if st.utilization_available
                     else "util n/a")
        if st.tx_dropped:
            parts.append(f"tx drop {st.tx_dropped}")
        self.dev_lbl.setText(" · ".join(parts))

    def _rf_begin_cycle(self, cycle: int) -> None:
        # If a STATUS cycle marker was lost, the first frame of the next
        # cycle closes the previous coverage instead of extending it.
        if self._rf_cycle is None:
            self._rf_cycle = cycle
        elif cycle != self._rf_cycle:
            self._rf_close_cycle()
            self._rf_cycle = cycle

    def _rf_close_cycle(self) -> None:
        # Channels without a frame in the closing cycle become gaps:
        # no zero-fill, no interpolation, no synthetic floor.
        self.cur[~self._rf_covered] = np.nan
        self._rf_covered[:] = False
        self._rf_cycle = None
        self._dirty = True

    def _rf_apply_frames(self, frames: list[tlv.SpectrumRf]) -> None:
        for f in frames:
            f_mhz = f.freqs
            mask = (self.freqs >= f_mhz[0]) & (self.freqs <= f_mhz[-1])
            if not mask.any():
                continue               # frame outside the display range
            self._rf_begin_cycle(f.cycle)
            self.cur[mask] = np.interp(self.freqs[mask], f_mhz,
                                       f.power_dbfs)
            np.fmax(self.peak, self.cur, out=self.peak)  # NaN-safe max-hold
            self._rf_covered |= mask
            self._frames += 1
        self._dirty = True

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
            for x, h, ch in zip(xs, hs, info.channels, strict=True):
                if ch not in self._util:
                    continue        # gap: no label that claims a value
                t = pg.TextItem(f"{h:.0f}", color=C["body"], anchor=(0.5, 1))
                t.setFont(font)
                t.setPos(x, h)
                self.util_plot.addItem(t)
                self.util_texts.append(t)
        for r, ch in enumerate(info.channels):
            it = self.ch_table.item(r, 2)
            if it is None:
                continue
            if ch in self._util:
                it.setText(f"{self._util[ch]:.0f} %")
                sample = self.monitor_state.util_samples.get(ch)
                it.setToolTip(
                    f"Sampled PHY CCA (experimental): busy {sample['busy']} / "
                    f"total {sample['total']} ticks · window_us_upper "
                    f"{sample['window_us_upper']} µs" if sample else "")
            else:
                it.setText("—")
                it.setToolTip(
                    "Sampled PHY CCA (experimental): no valid sample (gap)"
                    if self._rf_active else "")

    def _update_peak_column(self) -> None:
        info = BANDS[self.band]
        for r, ch in enumerate(info.channels):
            fc = channel_freq(self.band, ch)
            m = np.abs(self.freqs - fc) <= 10
            vals = self.peak[m] if m.any() else self.peak[:0]
            vals = vals[np.isfinite(vals)]
            it = self.ch_table.item(r, 3)
            if it is not None:
                # always write: NaN / sentinel (-200) shows the gap "—"
                it.setText(f"{vals.max():.0f}"
                           if vals.size and vals.max() > -199 else "—")

    # ============================================================ UI helpers
    def _apply_db_range(self) -> None:
        lo, hi = self.db_min.value(), self.db_max.value()
        unit = self._power_unit()
        self.db_min_lbl.setText(f"{lo} {unit}")
        self.db_max_lbl.setText(f"{hi} {unit}")
        self.spec_plot.getViewBox().setYRange(lo, hi, padding=0)
        self.wf_img.setLevels((lo, hi))
        self._dirty = True

    def _update_rbw(self) -> None:
        if self._rf_active:
            # RBW = effective span / effective bins (device-reported, never
            # what the GUI asked for)
            eff = self.monitor_state.spectrum_effective or {}
            span = int(eff.get("span_khz", 0))
            fft = int(eff.get("fft_size", 0))
            self.rbw_lbl.setText(f"{span / fft:.1f} kHz"
                                 if span and fft else "—")
        elif self._monitor_view():
            self.rbw_lbl.setText("N/A — no RF FFT")   # baseline monitor FW
        else:
            sr = int(self.sr_combo.currentText().split()[0])
            fft = int(self.fft_combo.currentText())
            self.rbw_lbl.setText(f"{sr * 1000 / fft:.1f} kHz")

    def _update_power_units(self) -> None:
        """dBFS vs dBm labelling across axis, captions, table header and
        dB-range readouts. RF and Demo never share a unit string."""
        unit = self._power_unit()
        theme.axis_label(self.spec_plot, "left", unit)
        self.spec_card.cap_lbl.setText(f"POWER · {unit}")
        self.ch_table.setHorizontalHeaderLabels(
            ["CH", "MHz", "Util",
             "Peak dBFS" if self._rf_active else "Peak"])
        self.util_card.cap_lbl.setText(
            "UTILIZATION · % · UNAVAILABLE"
            if self._rf_active and not self.monitor_state.utilization_available
            else "UTILIZATION · %")
        if self._rf_active and self.monitor_state.utilization_available:
            self.util_card.setToolTip(
                f"Sampled PHY CCA (experimental) · source={UTIL_SOURCE} · "
                f"confidence={UTIL_CONFIDENCE} · cell tooltip: raw "
                "busy/total ticks + window_us_upper µs")
        else:
            self.util_card.setToolTip("")
        self._apply_db_range()

    def _update_sweep_label(self, *_) -> None:
        limit = self.sweep_count.value()
        self.sweep_lbl.setText(f"{self.sweeps_done} / {limit if limit else '∞'}")

    def _reset_peak(self) -> None:
        self.peak[:] = np.nan if self._real_path() else -200
        self._dirty = True

    def _toggle_waterfall(self, on: bool) -> None:
        self.wf_card.setVisible(on)

    def _toggle_channels(self, on: bool) -> None:
        for ln in self._ch_lines:
            ln.setVisible(on)
        self.util_card.setVisible(on)

    def _on_mouse_moved(self, pos) -> None:
        vb = self.spec_plot.getViewBox()
        if not vb.sceneBoundingRect().contains(pos):
            self.readout.setText("")
            return
        pt = vb.mapSceneToView(pos)
        i = int(np.clip(np.searchsorted(self.freqs, pt.x()), 0, len(self.freqs) - 1))
        self.vline.setPos(self.freqs[i])
        unit = self._power_unit()
        cur = self.cur[i]
        pk = self.peak[i]
        cur_s = f"{cur:.1f} {unit}" if np.isfinite(cur) else "—"
        pk_s = f"{pk:.1f} {unit}" if np.isfinite(pk) else "—"
        self.readout.setText(
            f"{self.freqs[i]:.1f} MHz  ·  {cur_s}  ·  peak {pk_s}")

    def closeEvent(self, ev) -> None:
        self._detach()
        super().closeEvent(ev)
