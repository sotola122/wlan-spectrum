"""Light "Cursor" theme (see DESIGN.md): warm cream canvas, warm ink,
hairline-only depth, Cursor Orange reserved for primary CTAs.
"""

from __future__ import annotations

from pathlib import Path

import pyqtgraph as pg
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

# ---- design tokens (DESIGN.md › colors) --------------------------------
C = dict(
    primary="#f54e00", primary_active="#d04200", on_primary="#ffffff",
    ink="#26251e", body="#5a5852", muted="#807d72", muted_soft="#a09c92",
    hairline="#e6e5e0", hairline_soft="#efeee8", hairline_strong="#cfcdc4",
    canvas="#f7f7f4", canvas_soft="#fafaf7", card="#ffffff", surface_strong="#e6e5e0",
    peach="#dfa88f", mint="#9fc9a2", blue="#9fbbe0", lavender="#c0a8dd", gold="#c08532",
    error="#cf2d56", success="#1f8a65",
)

# Plot accents (pastels are allowed here as data colours)
SPECTRUM_LINE = "#3f6fae"       # deeper "read" blue for contrast on white
SPECTRUM_FILL = (159, 187, 224, 70)
PEAK_LINE = C["primary"]

UI_FAMILIES = ["Inter", "Noto Sans CJK JP", "Hiragino Sans", "Yu Gothic UI",
               "Helvetica Neue", "Helvetica", "Arial", "sans-serif"]
MONO_FAMILIES = ["JetBrains Mono", "Fira Code", "Noto Sans Mono CJK JP", "Menlo",
                 "Consolas", "DejaVu Sans Mono", "monospace"]
# Qt stylesheets take forward slashes. The url() values are quoted so a
# Windows drive prefix (C:) and spaces in the profile path still parse.
_ASSETS = Path(__file__).with_name("assets").resolve().as_posix()


def _css_families(fams: list[str]) -> str:
    return ", ".join(f'"{f}"' if " " in f else f for f in fams)


def ui_font(pt: float = 10, weight: QFont.Weight = QFont.Normal) -> QFont:
    f = QFont()
    f.setFamilies(UI_FAMILIES)
    f.setPointSizeF(pt)
    f.setWeight(weight)
    return f


def mono_font(pt: float = 9) -> QFont:
    f = QFont()
    f.setFamilies(MONO_FAMILIES)
    f.setPointSizeF(pt)
    f.setStyleHint(QFont.Monospace)
    return f


def waterfall_cmap() -> pg.ColorMap:
    """Cool → warm map that starts near the card white (quiet noise floor)."""
    stops = [0.0, 0.18, 0.40, 0.60, 0.80, 1.0]
    cols = ["#fafaf7", C["blue"], C["lavender"], C["peach"], C["gold"], C["primary_active"]]
    return pg.ColorMap(stops, [QColor(c) for c in cols])


def util_color(pct: float) -> str:
    """Mint → gold → error by severity."""
    return C["mint"] if pct < 40 else C["gold"] if pct < 70 else C["error"]


def configure_pyqtgraph() -> None:
    pg.setConfigOptions(antialias=True, background=C["card"], foreground=C["muted"],
                        imageAxisOrder="row-major")


def style_plot(plot: pg.PlotItem) -> None:
    """White card plot: hairline axes, muted mono ticks, faint grid."""
    for name in ("left", "bottom"):
        ax = plot.getAxis(name)
        ax.setPen(pg.mkPen(C["hairline_strong"]))
        ax.setTextPen(pg.mkPen(C["muted"]))
        ax.setTickFont(mono_font(8))
        ax.setStyle(tickLength=-4)
    plot.getAxis("left").setWidth(56)
    plot.showGrid(x=True, y=True, alpha=0.12)
    plot.hideButtons()


def axis_label(plot: pg.PlotItem, side: str, text: str) -> None:
    plot.setLabel(side, text, **{"color": C["muted"], "font-size": "8pt"})


def apply_theme(app: QApplication) -> None:
    configure_pyqtgraph()
    app.setStyle("Fusion")
    QFontDatabase.families()              # warm up font db
    app.setFont(ui_font(10))

    p = QPalette()
    for role, col in [
        (QPalette.Window, C["canvas"]), (QPalette.WindowText, C["ink"]),
        (QPalette.Base, C["card"]), (QPalette.AlternateBase, C["canvas_soft"]),
        (QPalette.Text, C["ink"]), (QPalette.Button, C["card"]), (QPalette.ButtonText, C["ink"]),
        (QPalette.ToolTipBase, C["card"]), (QPalette.ToolTipText, C["ink"]),
        (QPalette.Highlight, C["surface_strong"]), (QPalette.HighlightedText, C["ink"]),
        (QPalette.PlaceholderText, C["muted_soft"]),
    ]:
        p.setColor(role, QColor(col))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, QColor(C["muted_soft"]))
    app.setPalette(p)
    app.setStyleSheet(stylesheet())


def stylesheet() -> str:
    mono = _css_families(MONO_FAMILIES)
    return f"""
    QWidget {{ color: {C['ink']}; }}
    QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget#panel {{ background: {C['canvas']}; }}
    QToolTip {{ background: {C['card']}; color: {C['ink']}; border: 1px solid {C['hairline']};
               padding: 4px 8px; }}

    /* ---------- top nav ---------- */
    QFrame#topbar {{ background: {C['canvas']}; border-bottom: 1px solid {C['hairline']}; }}
    QLabel#wordmark {{ font-size: 13pt; font-weight: 400; color: {C['ink']}; }}
    QLabel#brandDot {{ color: {C['primary']}; font-size: 13pt; }}
    QLabel#caption {{ color: {C['muted']}; font-size: 8pt; font-weight: 600; letter-spacing: 1px; }}
    QLabel#dim {{ color: {C['muted']}; }}
    QFrame#vsep {{ background: {C['hairline']}; max-width: 1px; min-width: 1px; }}

    /* ---------- buttons ---------- */
    QPushButton {{ background: {C['card']}; color: {C['ink']}; border: 1px solid {C['hairline_strong']};
                   border-radius: 8px; padding: 6px 12px; font-weight: 500; min-height: 20px; }}
    QPushButton:hover {{ border-color: {C['muted_soft']}; }}
    QPushButton:pressed {{ background: {C['hairline_soft']}; }}
    QPushButton:checked {{ background: {C['surface_strong']}; border-color: {C['hairline_strong']}; }}
    QPushButton:disabled {{ color: {C['muted_soft']}; border-color: {C['hairline']}; }}
    QPushButton#primary {{ background: {C['primary']}; color: {C['on_primary']}; border: 1px solid {C['primary']}; }}
    QPushButton#primary:hover {{ background: #ff5a0d; }}
    QPushButton#primary:pressed {{ background: {C['primary_active']}; border-color: {C['primary_active']}; }}
    /* checked primary = running / connected -> becomes a quiet secondary */
    QPushButton#primary:checked {{ background: {C['card']}; color: {C['ink']}; border-color: {C['hairline_strong']}; }}
    QPushButton#icon {{ padding: 6px 8px; }}

    /* segmented control: hairline group, ink-on-surface-strong selection */
    QFrame#segment {{ background: {C['card']}; border: 1px solid {C['hairline_strong']}; border-radius: 8px; }}
    QPushButton#seg {{ background: transparent; border: none; border-radius: 6px; padding: 5px 10px;
                       color: {C['body']}; margin: 2px; }}
    QPushButton#seg:hover {{ color: {C['ink']}; }}
    QPushButton#seg:checked {{ background: {C['surface_strong']}; color: {C['ink']}; }}

    /* ---------- inputs ---------- */
    QComboBox, QSpinBox {{ background: {C['card']}; border: 1px solid {C['hairline_strong']}; border-radius: 8px;
                           padding: 5px 10px; min-height: 20px; font-family: {mono}; font-size: 9pt;
                           selection-background-color: {C['surface_strong']}; selection-color: {C['ink']}; }}
    QComboBox:disabled, QSpinBox:disabled {{ color: {C['muted_soft']}; background: {C['canvas_soft']};
                                              border-color: {C['hairline']}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox::down-arrow {{ image: url("{_ASSETS}/chevron-down.svg"); width: 10px; height: 10px; }}
    QComboBox QAbstractItemView {{ background: {C['card']}; border: 1px solid {C['hairline']};
                                   font-family: {mono}; outline: 0; padding: 4px; }}
    QSpinBox {{ padding-right: 22px; }}
    QSpinBox::up-button, QSpinBox::down-button {{ border: none; width: 18px; background: transparent; }}
    QSpinBox::up-arrow {{ image: url("{_ASSETS}/chevron-up.svg"); width: 10px; height: 10px; }}
    QSpinBox::down-arrow {{ image: url("{_ASSETS}/chevron-down.svg"); width: 10px; height: 10px; }}

    QCheckBox {{ spacing: 10px; color: {C['body']}; padding: 3px 0; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px;
                            border: 1px solid {C['hairline_strong']}; background: {C['card']}; }}
    QCheckBox::indicator:checked {{ background: {C['ink']}; border-color: {C['ink']};
                                    image: url("{_ASSETS}/check.svg"); }}

    QSlider::groove:horizontal {{ height: 4px; background: {C['hairline']}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {C['ink']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {C['card']}; border: 1px solid {C['hairline_strong']};
                                  width: 14px; height: 14px; margin: -6px 0; border-radius: 8px; }}

    /* ---------- cards ---------- */
    QFrame#card {{ background: {C['card']}; border: 1px solid {C['hairline']}; border-radius: 12px; }}
    QLabel#cardTitle {{ font-size: 10.5pt; font-weight: 500; color: {C['ink']}; }}
    QGroupBox {{ background: {C['card']}; border: 1px solid {C['hairline']}; border-radius: 12px;
                 margin-top: 22px; padding: 14px 14px 12px 14px; }}
    QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 2px; top: 2px;
                        color: {C['muted']}; font-size: 8pt; font-weight: 600; }}
    QGroupBox QLabel {{ color: {C['body']}; }}

    QLabel#mono, QLabel#value {{ font-family: {mono}; font-size: 9pt; color: {C['ink']}; }}
    QLabel#legend {{ font-family: {mono}; font-size: 8.5pt; color: {C['body']}; }}
    QLabel#device {{ font-family: {mono}; font-size: 8pt; color: {C['muted']};
                     background: {C['canvas_soft']}; border: 1px solid {C['hairline_soft']};
                     border-radius: 8px; padding: 8px; }}
    QLabel#badge {{ background: {C['surface_strong']}; color: {C['ink']}; border-radius: 9px;
                    padding: 2px 10px; font-size: 8pt; font-weight: 600; }}
    QLabel#badge[state="ok"] {{ background: #e3f1ea; color: {C['success']}; }}
    QLabel#badge[state="demo"] {{ background: #f6e9df; color: {C['gold']}; }}
    QLabel#badge[state="err"] {{ background: #f8e3e9; color: {C['error']}; }}

    QTableWidget {{ background: {C['card']}; border: none; font-family: {mono}; font-size: 9pt;
                    gridline-color: transparent; alternate-background-color: {C['canvas_soft']};
                    selection-background-color: {C['surface_strong']}; selection-color: {C['ink']}; }}
    QTableWidget::item {{ border-bottom: 1px solid {C['hairline_soft']}; padding: 2px; }}
    QHeaderView::section {{ background: {C['card']}; color: {C['muted']}; border: none;
                            border-bottom: 1px solid {C['hairline']}; padding: 6px 2px;
                            font-size: 8pt; font-weight: 600; }}

    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {C['hairline_strong']}; border-radius: 3px; min-height: 24px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QSplitter::handle {{ background: {C['canvas']}; }}
    QStatusBar {{ background: {C['canvas']}; color: {C['muted']}; border-top: 1px solid {C['hairline']};
                  font-family: {mono}; font-size: 8.5pt; }}
    QStatusBar::item {{ border: none; }}
    """
