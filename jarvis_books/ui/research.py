"""The Research tab: the background researcher's topics, its notes with clickable links, and its controls."""
from __future__ import annotations

import html
import logging
import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QPushButton, QSpinBox, QTextBrowser, QVBoxLayout, QWidget)

from jarvis_books.research import KINDS
from jarvis_books.text import linkify

log = logging.getLogger(__name__)
REFRESH_MS = 15000
ALL = "All topics"


class ResearchTab(QWidget):
    def __init__(self, studio, parent=None):
        super().__init__(parent)
        self.studio = studio
        self.researcher = studio.researcher
        self._closed = False
        lay = QHBoxLayout(self)

        left = QVBoxLayout()
        self.on_box = QCheckBox("Research in the background (free model on this computer)")
        self.on_box.toggled.connect(self._switch)
        left.addWidget(self.on_box)
        pace = QHBoxLayout()
        pace.addWidget(QLabel("Minutes between rounds:"))
        self.pace = QSpinBox()
        self.pace.setRange(1, 24 * 60)
        self.pace.editingFinished.connect(self._set_pace)
        pace.addWidget(self.pace)
        self.brain = QComboBox()
        self.brain.addItem("Free model on this computer", "local")
        self.brain.addItem("Groq (free online, about 1,000 a day)", "groq")
        self.brain.addItem("Gemini (uses its daily requests)", "gemini")
        self.brain.currentIndexChanged.connect(self._set_brain)
        pace.addWidget(self.brain)
        left.addLayout(pace)
        self.topic_list = QListWidget()
        self.topic_list.currentItemChanged.connect(lambda *_: self._show_notes())
        left.addWidget(self.topic_list, 1)
        buttons = QHBoxLayout()
        for label, slot in (("Research now", self._now), ("Pause", lambda: self._status("paused")),
                            ("Resume", lambda: self._status("active")), ("Done", lambda: self._status("done")),
                            ("Drop", self._drop)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        left.addLayout(buttons)
        add = QHBoxLayout()
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("New topic or project, e.g. 'solar generators'")
        self.new_name.returnPressed.connect(self._add)
        self.new_focus = QLineEdit()
        self.new_focus.setPlaceholderText("Focus (optional)")
        self.new_kind = QComboBox()
        for k in KINDS:
            self.new_kind.addItem(k.title(), k)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add)
        for w, stretch in ((self.new_name, 3), (self.new_focus, 2), (self.new_kind, 0), (add_btn, 0)):
            add.addWidget(w, stretch)
        left.addLayout(add)
        self.status = QLabel()
        self.status.setWordWrap(True)
        left.addWidget(self.status)
        lay.addLayout(left, 2)

        right = QVBoxLayout()
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        right.addWidget(self.notes, 1)
        self.activity = QTextBrowser()
        self.activity.setMaximumHeight(160)
        right.addWidget(QLabel("What the researcher did lately:"))
        right.addWidget(self.activity)
        lay.addLayout(right, 3)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    # ---- display ------------------------------------------------------------------------
    def _selected(self):
        item = self.topic_list.currentItem()
        return item.data(256) if item is not None else None

    def refresh(self) -> None:
        if self._closed:
            return
        try:
            settings = self.studio.settings()
            for w, v in ((self.on_box, settings["researcher"] == "1"),):
                w.blockSignals(True)
                w.setChecked(v)
                w.blockSignals(False)
            if not self.pace.hasFocus():
                self.pace.blockSignals(True)
                self.pace.setValue(int(float(settings["research_pace"] or 10)))
                self.pace.blockSignals(False)
            self.brain.blockSignals(True)
            self.brain.setCurrentIndex(max(0, self.brain.findData(settings["research_brain"])))
            self.brain.blockSignals(False)
            keep = self._selected()
            topics = self.researcher.topics()
            self.topic_list.blockSignals(True)
            self.topic_list.clear()
            first = QListWidgetItem(ALL)
            first.setData(256, None)
            self.topic_list.addItem(first)
            for t in topics:
                kind = "Project" if t["kind"] == "project" else "Topic"
                item = QListWidgetItem(f"{t['name']}  [{kind}, {t['status']}, {t['findings']} notes]")
                item.setData(256, t["id"])
                self.topic_list.addItem(item)
                if t["id"] == keep:
                    self.topic_list.setCurrentItem(item)
            if self.topic_list.currentItem() is None:
                self.topic_list.setCurrentRow(0)
            self.topic_list.blockSignals(False)
            self._show_notes()
            self.activity.setHtml("".join(
                f"<p>{time.strftime('%b %d %I:%M %p', time.localtime(a['created_at']))}: "
                f"{html.escape(a['detail'])}</p>" for a in self.researcher.activity(20)))
            problem = self.researcher.problem
            self.status.setText(("Researching in the background." if settings["researcher"] == "1" else
                                 "The researcher is off.") + (f" Problem: {problem}" if problem else ""))
        except Exception:
            log.exception("research tab refresh failed")

    def _show_notes(self) -> None:
        tid = self._selected()
        rows = self.researcher.findings(tid, limit=200)
        if not rows:
            self.notes.setHtml("<p>No notes yet. Add a topic, or talk with Jarvis about what interests you: "
                               "subjects you keep coming back to get researched by themselves.</p>")
            return
        self.notes.setHtml("".join(
            f"<h3>{html.escape(r['title'])}</h3><p><i>{html.escape(r['topic'])}</i> - "
            f"{linkify(r['summary'])}</p><p>{linkify(r['url'])}</p>" for r in rows))

    # ---- actions ------------------------------------------------------------------------
    def _run(self, name: str, args: dict) -> dict:
        out = self.studio.tools.run(name, args)
        if "error" in out:
            self.status.setText(out["error"])
        self.refresh()
        return out

    def _switch(self, on: bool) -> None:
        self._run("researcher_switch", {"on": bool(on)})

    def _set_pace(self) -> None:
        self._run("researcher_switch", {"minutes": int(self.pace.value())})

    def _set_brain(self, _index: int) -> None:
        self._run("researcher_switch", {"brain": self.brain.currentData()})

    def _add(self) -> None:
        name = self.new_name.text().strip()
        if not name:
            return
        out = self.studio.tools.run("research_topic", {"name": name, "focus": self.new_focus.text().strip(),
                                                       "kind": self.new_kind.currentData()})
        if "error" not in out:
            self.new_name.clear()
            self.new_focus.clear()
        self.refresh()
        if "error" in out:
            self.status.setText(out["error"])

    def _status(self, status: str) -> None:
        tid = self._selected()
        if tid is not None:
            self._run("change_research", {"topic": str(tid), "status": status})

    def _drop(self) -> None:
        tid = self._selected()
        if tid is not None:
            self._run("drop_research", {"topic": str(tid)})

    def _now(self) -> None:
        tid = self._selected()
        out = self._run("research_now", {"topic": str(tid)} if tid is not None else {})
        if out.get("started"):
            self.status.setText("Researching now in the background.")

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
