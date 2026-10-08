"""Entry point: ``python -m wifi_spectrum``."""

import sys


def main() -> int:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow
    from .theme import apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName("Wi-Fi Spectrum Analyzer")
    apply_theme(app)
    # Production store: non-sensitive knobs only (fps/history/aggregation/
    # dwell/attempts + mode/band/sweep/fft/rate); ordinary fixtures pass
    # settings=None and never touch it.
    settings = QSettings("wifi-spectrum", "monitor")
    win = MainWindow(start_demo="--demo" in sys.argv, settings=settings)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
