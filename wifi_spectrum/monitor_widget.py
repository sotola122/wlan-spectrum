"""Real-device monitor view: measured RSSI, received-packet rates, APs.

Renders only what the firmware observes (see ``wifi_spectrum.monitor_data``):
per-channel peak RSSI of received frames, received packets per second of
observation time, and bounded AP sightings. No interpolated curves, no FFT/
RBW values, no utilization percentages, no waterfall - those belong to the
Demo plots and are never produced for real measurements.
"""

from __future__ import annotations

import time

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .bands import BAND_24, BANDS, BandInfo, channel_freq
from .monitor_data import MonitorState
from .theme import C

WAITING_TEXT = "Waiting for device capabilities"
DISCONNECTED_TEXT = "Disconnected — no device data"
CAPTION = "Sequential passive observations; not RF spectrum or channel occupancy"

CHANNEL_COLUMNS = ("CH", "MHz", "RSSI dBm", "packets/s", "observed ms")
AP_COLUMNS = ("SSID", "BSSID", "obs CH", "adv CH", "RSSI dBm", "age")
DASH = "—"


def _band_info(band: int | None) -> BandInfo:
    if isinstance(band, int):
        info = BANDS.get(band)
        if info is not None:
            return info
    return BANDS[BAND_24]


def _card(title: str, caption: str) -> tuple[QFrame, pg.PlotItem]:
    """White card with a header row and a plot, matching the Demo cards."""
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(16, 12, 16, 10)
    lay.setSpacing(6)
    head = QHBoxLayout()
    head.setSpacing(12)
    t = QLabel(title)
    t.setObjectName("cardTitle")
    c = QLabel(caption)
    c.setObjectName("caption")
    head.addWidget(t)
    head.addWidget(c)
    head.addStretch(1)
    lay.addLayout(head)
    plot = pg.PlotItem()
    view = pg.PlotWidget(plotItem=plot)
    view.setFrameShape(QFrame.Shape.NoFrame)
    theme.style_plot(plot)
    lay.addWidget(view, 1)
    return frame, plot


def _table(columns: tuple[str, ...]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(list(columns))
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(26)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.setShowGrid(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    table.setFont(theme.mono_font(9))
    return table


def _cell(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    return item


class MonitorWidget(QWidget):
    """Live rendering of :class:`MonitorState`.

    ``refresh`` re-derives everything from the state; when the state is not
    acknowledged yet the widget shows ``_waiting_msg`` and empty tables, so
    stale measurements never survive a reconnect or band change.
    """

    def __init__(self, state: MonitorState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self._waiting_msg = WAITING_TEXT
        self._plot_band: int | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        status = QHBoxLayout()
        status.setSpacing(14)
        self.ack_lbl = QLabel(WAITING_TEXT)
        self.ack_lbl.setObjectName("value")
        self.ack_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.diag_lbl = QLabel("")
        self.diag_lbl.setObjectName("dim")
        status.addWidget(self.ack_lbl)
        status.addWidget(self.diag_lbl)
        status.addStretch(1)
        root.addLayout(status)

        self.caption_lbl = QLabel(CAPTION)
        self.caption_lbl.setObjectName("legend")
        root.addWidget(self.caption_lbl)

        rssi_card, self.rssi_plot = _card("Received-frame RSSI",
                                          "PEAK OF RECEIVED FRAMES · dBm")
        theme.axis_label(self.rssi_plot, "left", "dBm")
        # X positions are channel-center frequencies, but the tick strings
        # are channel numbers - label the axis for what the ticks show.
        theme.axis_label(self.rssi_plot, "bottom", "Channel")
        self.rssi_plot.getViewBox().setLimits(yMin=-115, yMax=0)
        self.rssi_plot.getViewBox().setYRange(-100, -20, padding=0)
        self.rssi_scatter = pg.ScatterPlotItem(pen=pg.mkPen(None),
                                               brush=pg.mkBrush(C["primary"]),
                                               size=7)
        self.rssi_plot.addItem(self.rssi_scatter)
        root.addWidget(rssi_card, 3)

        rate_card, self.rate_plot = _card("Received packets/s",
                                          "PER OBSERVATION TIME")
        theme.axis_label(self.rate_plot, "left", "packets/s")
        theme.axis_label(self.rate_plot, "bottom", "Channel")
        self.rate_plot.getViewBox().setYRange(0, 10, padding=0)
        self.rate_bars = pg.BarGraphItem(x=[], height=[], width=1,
                                         brush=pg.mkBrush(theme.SPECTRUM_LINE),
                                         pen=pg.mkPen(None))
        self.rate_plot.addItem(self.rate_bars)
        root.addWidget(rate_card, 3)

        tables = QHBoxLayout()
        tables.setSpacing(12)
        tables.addWidget(self._build_channel_card(), 1)
        tables.addWidget(self._build_ap_card(), 2)
        root.addLayout(tables, 2)

        self._age_timer = QTimer(self)        # AP age column / expiry pruning
        self._age_timer.setInterval(1000)
        self._age_timer.timeout.connect(self.refresh)
        self._age_timer.start()

    # ------------------------------------------------------------ cards
    def _build_channel_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 12, 16, 10)
        head = QHBoxLayout()
        title = QLabel("Channels")
        title.setObjectName("cardTitle")
        cap = QLabel("MEASURED")
        cap.setObjectName("caption")
        head.addWidget(title)
        head.addWidget(cap)
        head.addStretch(1)
        lay.addLayout(head)
        self.ch_table = _table(CHANNEL_COLUMNS)
        self.ch_table.setMinimumHeight(150)
        lay.addWidget(self.ch_table)
        return card

    def _build_ap_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 12, 16, 10)
        head = QHBoxLayout()
        title = QLabel("Access points")
        title.setObjectName("cardTitle")
        cap = QLabel("SEEN IN LAST 30 s")
        cap.setObjectName("caption")
        head.addWidget(title)
        head.addWidget(cap)
        head.addStretch(1)
        lay.addLayout(head)
        self.ap_table = _table(AP_COLUMNS)
        self.ap_table.setMinimumHeight(150)
        lay.addWidget(self.ap_table)
        return card

    # ------------------------------------------------------------ control
    def set_waiting(self, text: str = WAITING_TEXT) -> None:
        self._waiting_msg = text
        self.ack_lbl.setText(text)
        self.diag_lbl.setText("")
        self._clear_outputs()

    def refresh(self) -> None:
        state = self.state
        state.prune_aps()
        if not state.ready:
            self.ack_lbl.setText(self._waiting_msg)
            self._clear_outputs()
            return
        mode = "sweep" if state.mode == 1 else "live"
        parts = [f"epoch {state.epoch}", mode, _band_info(state.band).name]
        parts.append(f"dwell {state.dwell_ms} ms"
                     if state.dwell_ms is not None else "dwell —")
        parts.append(f"cycles {state.cycles}")
        if state.elapsed_ms is not None:
            parts.append(f"cycle {state.elapsed_ms} ms")
        if state.uptime_ms is not None:
            parts.append(f"uptime {state.uptime_ms} ms")
        self.ack_lbl.setText("  ·  ".join(parts))
        diag = [f"tx dropped {state.tx_dropped}",
                f"AP dropped {state.ap_dropped}"]
        if state.last_error:
            diag.append(f"last error: {state.last_error}")
        self.diag_lbl.setText("  ·  ".join(diag))
        self._fill_channel_table()
        self._fill_ap_table()
        self._fill_plots()

    def _clear_outputs(self) -> None:
        self.ch_table.setRowCount(0)
        self.ap_table.setRowCount(0)
        self.rssi_scatter.setData([], [])
        self.rate_bars.setOpts(x=[], height=[], width=1)
        self._plot_band = None

    # ------------------------------------------------------------ tables
    def _fill_channel_table(self) -> None:
        state = self.state
        info = _band_info(state.band)
        channels = state.channels
        self.ch_table.setRowCount(len(channels))
        for row, ch in enumerate(channels):
            obs = state.displayed.get(ch)
            if obs is None or ch in state.unavailable:
                cells = (str(ch), f"{channel_freq(info.band_id, ch):.0f}",
                         DASH, DASH, DASH)
            else:
                rssi = DASH if obs.peak_rssi_dbm is None else str(obs.peak_rssi_dbm)
                cells = (str(ch), f"{channel_freq(info.band_id, ch):.0f}", rssi,
                         f"{obs.packets_per_second:.1f}", str(obs.observed_ms))
            for col, text in enumerate(cells):
                self.ch_table.setItem(row, col, _cell(text))

    def _fill_ap_table(self) -> None:
        now = time.monotonic()
        sightings = sorted(self.state.aps.values(),
                           key=lambda v: v["last_seen"], reverse=True)
        self.ap_table.setRowCount(len(sightings))
        for row, ap in enumerate(sightings):
            advertised = ap.get("advertised_channel")
            rssi = ap.get("rssi_dbm")
            cells = (
                ap["ssid"] or "(hidden)",
                ap["bssid"],
                str(ap["rx_channel"]),
                DASH if advertised is None else str(advertised),
                DASH if rssi is None else str(rssi),
                f"{max(0.0, now - ap['last_seen']):.0f} s",
            )
            for col, text in enumerate(cells):
                self.ap_table.setItem(row, col, _cell(text))

    # ------------------------------------------------------------ plots
    def _fill_plots(self) -> None:
        state = self.state
        info = _band_info(state.band)
        band = info.band_id
        if band != self._plot_band:
            self._plot_band = band
            for plot in (self.rssi_plot, self.rate_plot):
                vb = plot.getViewBox()
                vb.setLimits(xMin=info.f_start, xMax=info.f_stop)
                vb.setXRange(info.f_start, info.f_stop, padding=0)
        rssi_x: list[float] = []
        rssi_y: list[float] = []
        rate_x: list[float] = []
        rate_y: list[float] = []
        ticks: list[tuple[float, str]] = []
        for ch in state.channels:
            obs = state.displayed.get(ch)
            if obs is None or ch in state.unavailable:
                continue
            fc = channel_freq(band, ch)
            ticks.append((fc, str(ch)))
            if obs.peak_rssi_dbm is not None:
                rssi_x.append(fc)
                rssi_y.append(float(obs.peak_rssi_dbm))
            rate_x.append(fc)
            rate_y.append(obs.packets_per_second)
        self.rssi_scatter.setData(rssi_x, rssi_y)
        width = 4.0 if band == BAND_24 else 15.0
        self.rate_bars.setOpts(x=rate_x, height=rate_y, width=width)
        for plot in (self.rssi_plot, self.rate_plot):
            plot.getAxis("bottom").setTicks([ticks, []])
        if rssi_y:
            lo = min(-100.0, min(rssi_y) - 5.0)
            hi = max(-20.0, max(rssi_y) + 5.0)
            self.rssi_plot.getViewBox().setYRange(lo, hi, padding=0)
        # packets/s is a rate, not a percentage: never clamp it at 100
        top = max(10.0, (max(rate_y) if rate_y else 0.0) * 1.2)
        self.rate_plot.getViewBox().setYRange(0, top, padding=0)
