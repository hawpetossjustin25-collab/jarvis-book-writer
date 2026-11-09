"""The yes/no card shown in every Jarvis conversation while Jarvis is waiting for the author's answer."""
from __future__ import annotations

import html
from typing import Callable

from PyQt6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton

from jarvis_books.assistant.permissions import CHANGE, LEVEL_LABELS, Request


class ApprovalBar(QFrame):
    def __init__(self, answer: Callable[[bool, int, bool], None], parent=None):
        super().__init__(parent)
        self.setObjectName("approval")
        self.setStyleSheet("#approval{background:#744210;border-radius:6px;padding:4px}")
        self._answer = answer
        self.request_id: int | None = None
        row = QHBoxLayout(self)
        self.label = QLabel()
        self.label.setWordWrap(True)
        row.addWidget(self.label, 1)
        self.session_box = QCheckBox("Don't ask again this session")
        row.addWidget(self.session_box)
        self.yes_btn = QPushButton("Yes")
        self.no_btn = QPushButton("No")
        self.yes_btn.clicked.connect(lambda: self._send(True))
        self.no_btn.clicked.connect(lambda: self._send(False))
        row.addWidget(self.yes_btn)
        row.addWidget(self.no_btn)
        self.hide()

    def ask(self, req: Request) -> None:
        self.request_id = req.id
        self.label.setText(f"<b>Jarvis wants to:</b> {html.escape(req.summary)} "
                           f"<i>({LEVEL_LABELS.get(req.level, req.level)})</i> - say or click yes or no")
        self.session_box.setChecked(False)
        self.session_box.setVisible(req.level == CHANGE)  # outside-world and system changes are always asked
        self.show()

    def close_request(self) -> None:
        self.request_id = None
        self.hide()

    def _send(self, yes: bool) -> None:
        if self.request_id is not None:
            self._answer(yes, self.request_id, self.session_box.isChecked() and self.session_box.isVisible())
