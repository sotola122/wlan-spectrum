"""Entry point: ``python -m wifi_spectrum``."""

import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow
    from .theme import apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName("Wi-Fi Spectrum Analyzer")
    apply_theme(app)
    win = MainWindow(start_demo="--demo" in sys.argv)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
