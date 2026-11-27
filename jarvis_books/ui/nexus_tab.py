"""The NEXUS tab: a live dashboard of the API, search, web exploring, facts, tools, Collective workers, models,
self-improvement and releases. The snapshot is read on a worker thread so a busy database never freezes the window."""
from __future__ import annotations

import logging
import threading

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from jarvis_books.nexus.dashboard import render, snapshot

log = logging.getLogger(__name__)
REFRESH_MS = 10000


class _Relay(QObject):
    ready = pyqtSignal(str)


class NexusTab(QWidget):
    def __init__(self, studio, parent=None):
        super().__init__(parent)
        self.studio = studio
        self._closed = False
        self._busy = threading.Lock()
        self.relay = _Relay(self)
        self.relay.ready.connect(self._show)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Live NEXUS status, straight from Jarvis's own records."))
        top.addStretch(1)
        self.live = QCheckBox("Update every 10 seconds")
        self.live.setChecked(True)
        self.live.toggled.connect(lambda on: self._timer.start() if on else self._timer.stop())
        top.addWidget(self.live)
        self.now_btn = QPushButton("Update now")
        self.now_btn.clicked.connect(self.refresh)
        top.addWidget(self.now_btn)
        lay.addLayout(top)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.view.setHtml("<p>Reading NEXUS...</p>")
        lay.addWidget(self.view, 1)
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self) -> bool:
        """Start one background read; False when one is already running or the tab is closed."""
        if self._closed or not self._busy.acquire(blocking=False):
            return False
        threading.Thread(target=self._read, name="nexus-dashboard", daemon=True).start()
        return True

    def _read(self) -> None:
        try:
            page = render(snapshot(self.studio))
        except Exception as exc:
            log.exception("nexus dashboard failed")
            page = f"<p>Couldn't read NEXUS: {type(exc).__name__}</p>"
        finally:
            self._busy.release()
        if not self._closed:
            self.relay.ready.emit(page)

    def _show(self, page: str) -> None:
        if self._closed:
            return
        bar = self.view.verticalScrollBar()
        keep = bar.value()
        self.view.setHtml(page)
        bar.setValue(keep)

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
