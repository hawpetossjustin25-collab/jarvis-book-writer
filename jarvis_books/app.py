"""Application entry point."""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading

from jarvis_books.paths import data_dir


def setup_logging() -> None:
    log_dir = data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(log_dir / "jarvis.log", maxBytes=2_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for noisy in ("urllib3", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_logging()
    from PyQt6.QtWidgets import QApplication

    from jarvis_books.studio import Studio
    from jarvis_books.ui.main_window import MainWindow

    app = QApplication(sys.argv[:1] + argv)
    app.setApplicationName("Jarvis Book Writer")
    studio = Studio()
    threading.Thread(target=studio.start, name="studio-start", daemon=True).start()
    window = MainWindow(studio)
    window.show()
    if "--smoke-test" in argv:
        from PyQt6.QtCore import QTimer
        def check():
            if studio.ready.is_set():
                print("SMOKE", "error=" + str(studio.start_error), flush=True)
                QTimer.singleShot(1500, window.close)
            else:
                QTimer.singleShot(500, check)
        QTimer.singleShot(500, check)
    return app.exec()
