"""Application entry point."""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading

from jarvis_books.lifecycle import exit_now, install_running, relaunch
from jarvis_books.paths import data_dir
from jarvis_books.watchdog import FreezeWatchdog


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


def restart_stuck_app() -> None:
    """The window froze: start a fresh Jarvis and end this one. Saved work is in the database already
    (the editor saves 1.5 s after typing stops), and background jobs resume on the next start."""
    try:
        relaunch()
    except OSError:
        logging.getLogger("jarvis_books").exception("could not start a fresh Jarvis")
    exit_now(3)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_logging()
    from PyQt6.QtWidgets import QApplication

    from jarvis_books.studio import Studio
    from jarvis_books.ui.main_window import MainWindow

    def log_unhandled(kind, value, tb):
        # PyQt aborts the whole app on an unhandled slot exception unless a hook is installed.
        logging.getLogger("jarvis_books").error("unhandled error", exc_info=(kind, value, tb))
    sys.excepthook = log_unhandled
    app = QApplication(sys.argv[:1] + argv)
    app.setApplicationName("Jarvis Book Writer")
    if install_running():
        # Its files are being replaced and the installer reopens it when done; starting now would freeze.
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.information(None, "Jarvis Book Writer",
                                "Jarvis is being updated right now. He opens by himself when the update is done.")
        return 0
    from PyQt6.QtCore import QTimer

    studio = Studio()
    threading.Thread(target=studio.start, name="studio-start", daemon=True).start()
    watchdog = FreezeWatchdog(restart_stuck_app, history=data_dir() / "freeze_restarts.json")
    window = MainWindow(studio, watchdog=watchdog)
    heartbeat = QTimer(window)
    heartbeat.setInterval(1000)
    heartbeat.timeout.connect(watchdog.beat)
    heartbeat.start()
    window.show()
    watchdog.start()
    if "--smoke-test" in argv:
        def check():
            if studio.ready.is_set():
                print("SMOKE", "error=" + str(studio.start_error), flush=True)
                QTimer.singleShot(1500, window.close)
            else:
                QTimer.singleShot(500, check)
        QTimer.singleShot(500, check)
    return app.exec()
