"""Main window: Jarvis (voice), Chat, Library, Write, Listen and Settings tabs."""
from __future__ import annotations

import html
import logging
import os
import sys
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                             QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSpinBox, QSplitter, QTabWidget,
                             QTextBrowser, QVBoxLayout, QWidget)

from jarvis_books.audio.narrator import EDGE_VOICES, GEMINI_VOICES
from jarvis_books.craft import GENRES
from jarvis_books.studio import Studio
from jarvis_books.text import word_count
from jarvis_books.ui.player import AudiobookPlayer
from jarvis_books.ui.voice_bridge import VoiceBridge
from jarvis_books.ui.workers import Bridge, run_async, wait_for_tasks
from jarvis_books.voice.live import LiveSession
from jarvis_books.writer import prompts

log = logging.getLogger(__name__)

STYLE = """
QWidget { background:#11151c; color:#e6e9ef; font-size:14px; }
QTabBar::tab { background:#1b2130; padding:10px 22px; margin-right:2px; border-top-left-radius:6px;
               border-top-right-radius:6px; }
QTabBar::tab:selected { background:#2b6cb0; color:white; }
QPushButton { background:#2b6cb0; border:none; padding:8px 14px; border-radius:6px; color:white; }
QPushButton:hover { background:#3182ce; } QPushButton:disabled { background:#2d3748; color:#718096; }
QPushButton#danger { background:#9b2c2c; }
QLineEdit, QPlainTextEdit, QTextBrowser, QListWidget, QComboBox, QSpinBox { background:#1a202c;
    border:1px solid #2d3748; border-radius:6px; padding:6px; }
QPlainTextEdit#editor { font-family:Georgia, 'Times New Roman'; font-size:16px; padding:18px; }
QProgressBar { border:1px solid #2d3748; border-radius:6px; text-align:center; }
QProgressBar::chunk { background:#38a169; border-radius:6px; }
QLabel#orb { font-size:20px; padding:30px; border-radius:90px; background:#1e3a5f; min-width:180px;
             min-height:180px; max-width:180px; max-height:180px; qproperty-alignment:AlignCenter; }
"""


class NewBookDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New book")
        self.resize(560, 360)
        form = QFormLayout(self)
        self.idea = QPlainTextEdit()
        self.idea.setPlaceholderText("Tell me the story you want: who, where, what goes wrong, the feel...")
        self.title = QLineEdit()
        self.title.setPlaceholderText("optional - I'll suggest one")
        self.genre = QComboBox()
        self.genre.addItem("Let Jarvis decide", "")
        for g in GENRES.values():
            self.genre.addItem(g.name, g.name)
        self.chapters = QSpinBox()
        self.chapters.setRange(0, 80)
        self.chapters.setSpecialValueText("Recommended for the genre")
        form.addRow("Story idea", self.idea)
        form.addRow("Title", self.title)
        form.addRow("Genre", self.genre)
        form.addRow("Chapters", self.chapters)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class MainWindow(QMainWindow):
    def __init__(self, studio: Studio):
        super().__init__()
        self.studio = studio
        self.setWindowTitle("Jarvis Book Writer")
        self.resize(1280, 820)
        self.setStyleSheet(STYLE)
        self.bridge = Bridge()
        studio.library.subscribe(lambda e, b, d: self.bridge.library_changed.emit(e, b))
        studio.jobs.subscribe(self.bridge.job_updated.emit)
        self.bridge.library_changed.connect(self._on_library_changed)
        self.bridge.job_updated.connect(self._on_job)
        studio.tools.audio_handler = self._audio_tool
        self.player = AudiobookPlayer(studio.narrator, self)
        self.voice = VoiceBridge(self._make_live_session, self)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._jarvis_tab(), "Jarvis")
        self.tabs.addTab(self._chat_tab(), "Chat")
        self.tabs.addTab(self._library_tab(), "Library")
        self.tabs.addTab(self._write_tab(), "Write")
        self.tabs.addTab(self._listen_tab(), "Listen")
        self.tabs.addTab(self._settings_tab(), "Settings")
        self.job_bar = QProgressBar()
        self.job_bar.setMaximumWidth(380)
        self.job_bar.hide()
        self.statusBar().addPermanentWidget(self.job_bar)
        self._closed = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(400)
        self._refresh_timer.timeout.connect(self.refresh_books)
        self._autosave = QTimer(self)
        self._autosave.setSingleShot(True)
        self._autosave.setInterval(1500)
        self._autosave.timeout.connect(self._save_editor)
        self.current_chapter_id: Optional[str] = None
        self._loading_editor = False
        self.refresh_books()
        self.statusBar().showMessage("Starting Gemini...")
        run_async(lambda: (studio.ready.wait(), studio.start_error)[1], self._on_started)

    # ---- startup -----------------------------------------------------------------------
    def _on_started(self, error) -> None:
        if error or self.studio.engine is None or self.studio.engine._config is None:
            self.statusBar().showMessage(error or self.studio.start_error or "Gemini is not connected")
            self.tabs.setCurrentIndex(5)
        else:
            cfg = self.studio.engine.get_runtime_configuration()
            self.statusBar().showMessage(f"Ready - writing with {cfg.routes['writer'][0]}, "
                                         f"planning with {cfg.routes['planner'][0]}", 8000)
            if self.studio.settings()["wake_word"] == "1":
                self.wake_box.setChecked(True)
        self._refresh_settings()

    # ---- Jarvis -----------------------------------------------------------------------
    def _jarvis_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.orb = QLabel("Jarvis")
        self.orb.setObjectName("orb")
        top.addStretch()
        top.addWidget(self.orb)
        top.addStretch()
        lay.addLayout(top)
        self.voice_state = QLabel("Press Talk or say \"Hey Jarvis\"")
        self.voice_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.voice_state)
        self.mic_level = QProgressBar()
        self.mic_level.setRange(0, 100)
        self.mic_level.setTextVisible(False)
        self.mic_level.setMaximumHeight(8)
        lay.addWidget(self.mic_level)
        row = QHBoxLayout()
        self.talk_btn = QPushButton("Talk to Jarvis")
        self.talk_btn.setMinimumHeight(48)
        self.talk_btn.clicked.connect(self._toggle_talk)
        self.mute_box = QCheckBox("Mute mic")
        self.mute_box.toggled.connect(lambda on: setattr(self.voice, "muted", on))
        self.wake_box = QCheckBox("Listen for \"Hey Jarvis\"")
        self.wake_box.toggled.connect(self._toggle_wake)
        row.addWidget(self.talk_btn, 2)
        row.addWidget(self.mute_box)
        row.addWidget(self.wake_box)
        lay.addLayout(row)
        self.voice_log = QTextBrowser()
        lay.addWidget(self.voice_log, 1)
        typed = QHBoxLayout()
        self.voice_input = QLineEdit()
        self.voice_input.setPlaceholderText("Type to Jarvis during a voice session...")
        self.voice_input.returnPressed.connect(self._voice_type)
        typed.addWidget(self.voice_input)
        lay.addLayout(typed)
        self.voice.state.connect(self._on_voice_state)
        self.voice.transcript.connect(self._on_voice_transcript)
        self.voice.tool_used.connect(lambda n, r: self.voice_log.append(f"<i style='color:#90cdf4'>[{n}]</i>"))
        self.voice.level.connect(lambda v: self.mic_level.setValue(int(v * 100)))
        self._voice_line = {"user": "", "jarvis": ""}
        return w

    def _make_live_session(self, **callbacks) -> LiveSession:
        engine = self.studio.engine
        if engine is None:
            raise RuntimeError("Add your Gemini API key in Settings first")
        model = engine.live_model()
        if not model:
            raise RuntimeError("Your Gemini key has no live voice model available")
        profile = engine.profile(model)
        books = self.studio.library.list_books()
        system = prompts.ASSISTANT + "\n\nYou are speaking out loud. Keep answers to a few sentences unless " \
            "reading a book aloud. When reading, read the text exactly as written.\nBooks: " + \
            ", ".join(f"'{b.title}'" for b in books[:30])
        return LiveSession(self.studio.client.live_url(), self.studio.client.live_headers(), model, system,
                           self.studio.tools.declarations(), self.studio.tools.call,
                           voice=self.studio.settings()["jarvis_voice"],
                           context_tokens=profile.context_window if profile else None, **callbacks)

    def _toggle_talk(self) -> None:
        if self.voice.active:
            self.voice.stop()
            self.talk_btn.setText("Talk to Jarvis")
            return
        self.talk_btn.setEnabled(False)
        self.voice_state.setText("Connecting...")
        try:
            self.voice.start()
            self.talk_btn.setText("Stop talking")
        except Exception as exc:
            self.voice_state.setText(f"Voice error: {exc}")
        finally:
            self.talk_btn.setEnabled(True)

    def _toggle_wake(self, on: bool) -> None:
        self.studio.set_setting("wake_word", "1" if on else "0")
        if on and not self.voice.enable_wake_word(True):
            self.voice_state.setText(f"Wake word unavailable ({self.voice.wake_error}). Use the Talk button.")
            self.wake_box.blockSignals(True)
            self.wake_box.setChecked(False)
            self.wake_box.blockSignals(False)
        elif not on:
            self.voice.enable_wake_word(False)

    def _voice_type(self) -> None:
        text = self.voice_input.text().strip()
        if text and self.voice.active:
            self.voice.send_text(text)
            self.voice_log.append(f"<b>You:</b> {html.escape(text)}")
            self.voice_input.clear()

    def _on_voice_state(self, state: str) -> None:
        self.voice_state.setText(state.capitalize())
        colors = {"listening": "#22543d", "speaking": "#2b6cb0", "working": "#744210", "connecting": "#4a5568"}
        self.orb.setStyleSheet(f"background:{colors.get(state, '#1e3a5f')}")
        self.orb.setText({"listening": "Listening", "speaking": "Speaking", "working": "Working"}.get(state, "Jarvis"))
        if state in ("idle", "offline"):
            self.talk_btn.setText("Talk to Jarvis")
            if self.voice.session is not None and state == "offline":
                self.voice.stop()

    def _on_voice_transcript(self, role: str, text: str) -> None:
        if role == "turn":
            for who, label in (("user", "You"), ("jarvis", "Jarvis")):
                if self._voice_line[who].strip():
                    self.voice_log.append(f"<b>{label}:</b> {html.escape(self._voice_line[who].strip())}")
                self._voice_line[who] = ""
            return
        self._voice_line[role] = self._voice_line.get(role, "") + text

    # ---- Chat -----------------------------------------------------------------------------
    def _chat_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.chat_view = QTextBrowser()
        self.chat_view.setOpenExternalLinks(False)
        lay.addWidget(self.chat_view, 1)
        row = QHBoxLayout()
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("Ask Jarvis to plan, write, revise, read or brainstorm...")
        self.chat_input.returnPressed.connect(self._send_chat)
        self.chat_send = QPushButton("Send")
        self.chat_send.clicked.connect(self._send_chat)
        row.addWidget(self.chat_input, 1)
        row.addWidget(self.chat_send)
        lay.addLayout(row)
        for m in self.studio.library.recent_messages("chat", 40):
            self._chat_append("You" if m.role == "user" else "Jarvis", m.text)
        return w

    def _chat_append(self, who: str, text: str) -> None:
        color = "#90cdf4" if who == "Jarvis" else "#fbd38d"
        body = html.escape(text).replace("\n", "<br>")
        self.chat_view.append(f"<p><b style='color:{color}'>{who}:</b> {body}</p>")

    def _send_chat(self) -> None:
        text = self.chat_input.text().strip()
        if not text:
            return
        if self.studio.engine is None:
            QMessageBox.warning(self, "Gemini", "Add your Gemini API key in Settings first.")
            return
        self.chat_input.clear()
        self._chat_append("You", text)
        self.chat_send.setEnabled(False)
        self.chat_view.append("<i style='color:#718096'>Jarvis is thinking...</i>")

        def done(reply):
            self.chat_send.setEnabled(True)
            self._chat_append("Jarvis", reply or "(done)")

        def fail(err):
            self.chat_send.setEnabled(True)
            self._chat_append("Jarvis", f"Sorry, that failed: {err}")
        run_async(lambda: self.studio.assistant.send(text), done, fail)

    # ---- Library ----------------------------------------------------------------------------
    def _library_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()
        self.book_list = QListWidget()
        self.book_list.currentItemChanged.connect(lambda *_: self._show_book())
        new_btn = QPushButton("New book")
        new_btn.clicked.connect(self._new_book)
        left.addWidget(new_btn)
        left.addWidget(self.book_list, 1)
        self.storage_label = QLabel()
        self.storage_label.setWordWrap(True)
        left.addWidget(self.storage_label)
        lay.addLayout(left, 1)
        right = QVBoxLayout()
        self.book_info = QTextBrowser()
        right.addWidget(self.book_info, 1)
        row = QHBoxLayout()
        for label, slot in (("Write next chapter", lambda: self._write_next(1)),
                            ("Write next 3", lambda: self._write_next(3)),
                            ("Open in editor", self._open_in_editor), ("Listen", self._listen_selected)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        right.addLayout(row)
        row2 = QHBoxLayout()
        for fmt in ("docx", "epub", "markdown", "m4b", "mp3"):
            b = QPushButton(f"Export {fmt.upper()}")
            b.clicked.connect(lambda _, f=fmt: self._export(f))
            row2.addWidget(b)
        arch = QPushButton("Archive")
        arch.setObjectName("danger")
        arch.clicked.connect(self._archive)
        row2.addWidget(arch)
        right.addLayout(row2)
        lay.addLayout(right, 2)
        return w

    def selected_book_id(self) -> Optional[str]:
        item = self.book_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def refresh_books(self) -> None:
        if self._closed:
            return
        current = self.selected_book_id()
        books = self.studio.library.list_books()
        for combo in (getattr(self, "write_book", None), getattr(self, "listen_book", None)):
            if combo is None:
                continue
            keep = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for b in books:
                combo.addItem(b.title, b.id)
            idx = combo.findData(keep)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)
        self.book_list.blockSignals(True)
        self.book_list.clear()
        for b in books:
            stats = self.studio.library.book_stats(b.id)
            item = QListWidgetItem(f"{b.title}\n   {b.genre} - {stats.get('words', 0):,} words - {b.status}")
            item.setData(Qt.ItemDataRole.UserRole, b.id)
            self.book_list.addItem(item)
            if b.id == current:
                self.book_list.setCurrentItem(item)
        self.book_list.blockSignals(False)
        if self.book_list.currentItem() is None and self.book_list.count():
            self.book_list.setCurrentRow(0)
        self._show_book()
        try:
            self.storage_label.setText(self.studio.capacity.report().describe())
        except Exception:
            pass

    def _show_book(self) -> None:
        book_id = self.selected_book_id()
        if not book_id:
            self.book_info.setHtml("<h2>No books yet</h2><p>Click <b>New book</b>, or tell Jarvis your idea.</p>")
            return
        b = self.studio.library.get_book(book_id)
        self.studio.tools.current_book_id = b.id
        rows = "".join(f"<tr><td>{c.number}</td><td>{html.escape(c.title)}</td><td>{c.status}</td>"
                       f"<td align='right'>{c.word_count:,}</td></tr>" for c in self.studio.library.list_chapters(b.id))
        chars = ", ".join(html.escape(e.name) for e in self.studio.library.list_bible(b.id, "character")[:12])
        self.book_info.setHtml(
            f"<h2>{html.escape(b.title)}</h2><p><i>{html.escape(b.logline or b.premise)}</i></p>"
            f"<p>{html.escape(b.genre)} - {html.escape(b.pov)}, {html.escape(b.tense)} tense - target "
            f"{b.target_words:,} words</p><p><b>Characters:</b> {chars or '-'}</p>"
            f"<table width='100%' cellspacing=4><tr><th>#</th><th>Chapter</th><th>Status</th><th>Words</th></tr>"
            f"{rows}</table>")

    def _new_book(self) -> None:
        dlg = NewBookDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.idea.toPlainText().strip():
            return
        result = self.studio.tools.call("create_book", {"idea": dlg.idea.toPlainText().strip(),
                                                        "title": dlg.title.text().strip(),
                                                        "genre": dlg.genre.currentData() or "",
                                                        "chapters": dlg.chapters.value()})
        self._report(result)

    def _write_next(self, count: int) -> None:
        book_id = self.selected_book_id()
        if book_id:
            self._report(self.studio.tools.call("write_chapters", {"book": book_id, "count": count}))

    def _open_in_editor(self) -> None:
        book_id = self.selected_book_id()
        if book_id:
            self.write_book.setCurrentIndex(self.write_book.findData(book_id))
            self.tabs.setCurrentIndex(3)

    def _export(self, fmt: str) -> None:
        book_id = self.selected_book_id()
        if not book_id:
            return
        try:
            result = self.studio.export(book_id, fmt)
        except Exception as exc:
            result = {"error": str(exc)}
        if result.get("saved"):
            QMessageBox.information(self, "Exported", f"Saved to:\n{result['saved']}")
            if sys.platform == "win32":
                os.startfile(os.path.dirname(result["saved"]))  # type: ignore[attr-defined]
        else:
            self._report(result)

    def _archive(self) -> None:
        book_id = self.selected_book_id()
        if book_id and QMessageBox.question(self, "Archive", "Hide this book from the library? (Nothing is "
                                            "deleted; its files stay in OneDrive.)") == QMessageBox.StandardButton.Yes:
            self.studio.library.update_book(book_id, status="archived")

    def _report(self, result: dict) -> None:
        if result.get("error"):
            QMessageBox.warning(self, "Jarvis", result["error"])
        elif result.get("started"):
            self.statusBar().showMessage("Started in the background - you can keep working.", 5000)

    # ---- Write --------------------------------------------------------------------------------
    def _write_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        self.write_book = QComboBox()
        self.write_book.currentIndexChanged.connect(lambda *_: self._load_chapters())
        self.chapter_list = QListWidget()
        self.chapter_list.currentItemChanged.connect(lambda *_: self._load_chapter())
        add = QPushButton("Add chapter")
        add.clicked.connect(self._add_chapter)
        ll.addWidget(self.write_book)
        ll.addWidget(self.chapter_list, 1)
        ll.addWidget(add)
        split.addWidget(left)
        right = QWidget()
        rl = QVBoxLayout(right)
        self.outline_edit = QPlainTextEdit()
        self.outline_edit.setMaximumHeight(90)
        self.outline_edit.setPlaceholderText("Chapter outline")
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("editor")
        self.editor.textChanged.connect(self._editor_changed)
        self.outline_edit.textChanged.connect(self._editor_changed)
        self.editor_status = QLabel()
        rl.addWidget(self.outline_edit)
        rl.addWidget(self.editor, 1)
        row = QHBoxLayout()
        for label, slot in (("AI: write this chapter", self._ai_write), ("AI: revise...", self._ai_revise),
                            ("Accept chapter", self._accept), ("History", self._history),
                            ("Read it to me", self._listen_chapter)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        rl.addLayout(row)
        rl.addWidget(self.editor_status)
        split.addWidget(right)
        split.setSizes([280, 1000])
        lay.addWidget(split)
        return w

    def _load_chapters(self) -> None:
        if self._closed:
            return
        self._save_editor()
        book_id = self.write_book.currentData()
        keep = self.current_chapter_id
        self.chapter_list.blockSignals(True)
        self.chapter_list.clear()
        if book_id:
            for c in self.studio.library.list_chapters(book_id):
                item = QListWidgetItem(f"{c.number}. {c.title or 'Untitled'}  [{c.status}, {c.word_count:,}]")
                item.setData(Qt.ItemDataRole.UserRole, c.id)
                self.chapter_list.addItem(item)
                if c.id == keep:
                    self.chapter_list.setCurrentItem(item)
        self.chapter_list.blockSignals(False)
        if self.chapter_list.currentItem() is None and self.chapter_list.count():
            self.chapter_list.setCurrentRow(0)
        self._load_chapter()

    def _load_chapter(self) -> None:
        item = self.chapter_list.currentItem()
        new_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if new_id != self.current_chapter_id:
            self._save_editor()
        self.current_chapter_id = new_id
        self._loading_editor = True
        if new_id:
            ch = self.studio.library.get_chapter_by_id(new_id)
            if self.editor.toPlainText() != ch.text:
                self.editor.setPlainText(ch.text)
            if self.outline_edit.toPlainText() != ch.outline:
                self.outline_edit.setPlainText(ch.outline)
            self.editor_status.setText(f"{ch.heading} - {ch.word_count:,} / ~{ch.target_words:,} words - {ch.status}")
        else:
            self.editor.clear()
            self.outline_edit.clear()
        self._loading_editor = False

    def _editor_changed(self) -> None:
        if not self._loading_editor and self.current_chapter_id:
            self.editor_status.setText(f"{word_count(self.editor.toPlainText()):,} words - editing...")
            self._autosave.start()

    def _save_editor(self) -> None:
        if not self.current_chapter_id or self._loading_editor:
            return
        self._autosave.stop()
        ch = self.studio.library.get_chapter_by_id(self.current_chapter_id)
        text, outline = self.editor.toPlainText(), self.outline_edit.toPlainText()
        if outline != ch.outline:
            self.studio.library.update_chapter(ch.id, outline=outline)
        if text != ch.text:
            self.studio.library.save_chapter_text(ch.id, text, source="user")
            self.editor_status.setText(f"Saved - {word_count(text):,} words")

    def _chapter(self):
        self._save_editor()
        return self.studio.library.get_chapter_by_id(self.current_chapter_id) if self.current_chapter_id else None

    def _add_chapter(self) -> None:
        book_id = self.write_book.currentData()
        if book_id:
            title, ok = QInputDialog.getText(self, "Add chapter", "Chapter title")
            if ok:
                self.studio.tools.call("change_outline", {"book": book_id, "chapter": 0, "title": title})

    def _ai_write(self) -> None:
        ch = self._chapter()
        if not ch:
            return
        if ch.text.strip() and QMessageBox.question(self, "Rewrite", "Replace this chapter with a new AI draft? "
                                                    "(The current text stays in History.)") != QMessageBox.StandardButton.Yes:
            return
        guide, _ = QInputDialog.getText(self, "Write chapter", "Any direction for this chapter? (optional)")
        job = self.studio.jobs.submit("write", f"Writing {ch.heading}", lambda p, c: self.studio.writer.write_chapter(
            ch.book_id, ch.number, guide, progress=p, cancel=c).number, book_id=ch.book_id)
        self.statusBar().showMessage(f"Writing {ch.heading} in the background (job {job.id})", 5000)

    def _ai_revise(self) -> None:
        ch = self._chapter()
        if not ch:
            return
        notes, ok = QInputDialog.getMultiLineText(self, "Revise", "What should change? (e.g. more tension, "
                                                  "cut the flashback, write it my way: ...)")
        if ok and notes.strip():
            self._report(self.studio.tools.call("rewrite_chapter", {"book": ch.book_id, "chapter": ch.number,
                                                                    "instructions": notes}))

    def _accept(self) -> None:
        ch = self._chapter()
        if ch and ch.text.strip():
            self.studio.jobs.submit("accept", f"Updating the story bible from {ch.heading}",
                                    lambda p, c: self.studio.writer.accept_chapter(ch.book_id, ch.number).number,
                                    book_id=ch.book_id)

    def _history(self) -> None:
        ch = self._chapter()
        if not ch:
            return
        revs = self.studio.library.revisions(ch.id)
        if not revs:
            QMessageBox.information(self, "History", "No earlier versions yet.")
            return
        import time as _t
        labels = [f"{_t.strftime('%b %d %H:%M', _t.localtime(r.created_at))} - {r.source} - {r.word_count:,} words"
                  for r in revs]
        choice, ok = QInputDialog.getItem(self, "Restore a version", "Versions (newest first):", labels, 0, False)
        if ok:
            rev = revs[labels.index(choice)]
            self.studio.library.restore_revision(rev.id)
            self.current_chapter_id = None
            self._load_chapters()

    # ---- Listen ---------------------------------------------------------------------------------
    def _listen_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.listen_book = QComboBox()
        lay.addWidget(self.listen_book)
        self.now_playing = QLabel("Pick a book and press Play. Unfinished books play everything written so far.")
        self.now_playing.setWordWrap(True)
        self.now_playing.setFont(QFont("Georgia", 15))
        lay.addWidget(self.now_playing)
        self.listen_progress = QProgressBar()
        lay.addWidget(self.listen_progress)
        row = QHBoxLayout()
        for label, slot in (("<< Chapter", lambda: self.player.jump_chapter(-1)), ("< Back", lambda: self.player.skip(-1)),
                            ("Play", self._play), ("Pause", self.player.pause), ("Next >", lambda: self.player.skip(1)),
                            ("Chapter >>", lambda: self.player.jump_chapter(1))):
            b = QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Speed"))
        self.speed = QComboBox()
        for s in ("0.75", "0.9", "1.0", "1.1", "1.25", "1.5", "1.75", "2.0"):
            self.speed.addItem(f"{s}x", float(s))
        self.speed.setCurrentIndex(self.speed.findData(float(self.studio.settings()["narration_speed"])))
        self.speed.currentIndexChanged.connect(self._speed_changed)
        row2.addWidget(self.speed)
        row2.addWidget(QLabel("Volume"))
        vol = QSlider(Qt.Orientation.Horizontal)
        vol.setRange(0, 100)
        vol.setValue(80)
        vol.valueChanged.connect(lambda v: self.player.set_volume(v / 100))
        self.player.set_volume(0.8)
        row2.addWidget(vol, 1)
        lay.addLayout(row2)
        lay.addStretch()
        self.player.position_changed.connect(self._on_play_position)
        self.player.state_changed.connect(lambda s: self.statusBar().showMessage(f"Audiobook: {s}", 4000))
        self.player.error.connect(lambda e: self.now_playing.setText(e))
        return w

    def _speed_changed(self) -> None:
        rate = self.speed.currentData()
        self.player.set_speed(rate)
        self.studio.set_setting("narration_speed", str(rate))

    def _play(self) -> None:
        book_id = self.listen_book.currentData()
        if not book_id:
            return
        if self.player.book_id != book_id:
            self.player.load(book_id)
        else:
            self.player.refresh()
        self.player.set_speed(self.speed.currentData())
        self.player.play()

    def _on_play_position(self, chapter: int, index: int, total: int) -> None:
        book = self.studio.library.get_book(self.player.book_id)
        ch = self.studio.library.get_chapter(book.id, chapter)
        self.now_playing.setText(f"{book.title}\n{ch.heading}")
        self.listen_progress.setRange(0, max(1, total))
        self.listen_progress.setValue(index + 1)

    def _listen_selected(self) -> None:
        book_id = self.selected_book_id()
        if book_id:
            self.listen_book.setCurrentIndex(self.listen_book.findData(book_id))
            self.tabs.setCurrentIndex(4)
            self._play()

    def _listen_chapter(self) -> None:
        ch = self._chapter()
        if ch:
            self.listen_book.setCurrentIndex(self.listen_book.findData(ch.book_id))
            self.tabs.setCurrentIndex(4)
            self.player.load(ch.book_id, ch.number)
            self.player.play()

    def _audio_tool(self, action: str, args: dict) -> dict:
        """Called by Jarvis tools from a worker thread; hop to the UI thread."""
        def do():
            if action == "play":
                self.listen_book.setCurrentIndex(self.listen_book.findData(args["book_id"]))
                self.player.load(args["book_id"], args.get("chapter") or 0)
                self.player.play()
            else:
                self.player.pause()
        QTimer.singleShot(0, do)
        return {"ok": True, "action": action}

    # ---- Settings ---------------------------------------------------------------------------------
    def _settings_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Gemini API key (saved encrypted on this laptop)")
        save_key = QPushButton("Save key")
        save_key.clicked.connect(self._save_key)
        krow = QHBoxLayout()
        krow.addWidget(self.key_edit, 1)
        krow.addWidget(save_key)
        form.addRow("Gemini key", krow)
        s = self.studio.settings()
        self.author_edit = QLineEdit(s["author"])
        self.author_edit.editingFinished.connect(lambda: self.studio.set_setting("author", self.author_edit.text()))
        form.addRow("Author name", self.author_edit)
        self.narrator_voice = QComboBox()
        self.narrator_voice.addItems(GEMINI_VOICES)
        self.narrator_voice.setCurrentText(s["voice"])
        self.narrator_voice.currentTextChanged.connect(lambda v: self.studio.set_setting("voice", v))
        form.addRow("Audiobook narrator", self.narrator_voice)
        self.edge_voice = QComboBox()
        self.edge_voice.addItems(EDGE_VOICES)
        self.edge_voice.setCurrentText(s["edge_voice"])
        self.edge_voice.currentTextChanged.connect(lambda v: self.studio.set_setting("edge_voice", v))
        form.addRow("Backup narrator (when Gemini speech quota is used up)", self.edge_voice)
        self.jarvis_voice = QComboBox()
        self.jarvis_voice.addItems(GEMINI_VOICES)
        self.jarvis_voice.setCurrentText(s["jarvis_voice"])
        self.jarvis_voice.currentTextChanged.connect(lambda v: self.studio.set_setting("jarvis_voice", v))
        form.addRow("Jarvis voice", self.jarvis_voice)
        lay.addLayout(form)
        lay.addWidget(QLabel("<b>Adaptive Gemini engine</b>"))
        self.engine_view = QTextBrowser()
        lay.addWidget(self.engine_view, 1)
        row = QHBoxLayout()
        self.adaptive_box = QCheckBox("Automatic tuning")
        self.adaptive_box.setChecked(True)
        self.adaptive_box.toggled.connect(self._toggle_adaptive)
        rb = QPushButton("Roll back last change")
        rb.clicked.connect(self._rollback)
        ref = QPushButton("Refresh")
        ref.clicked.connect(self._refresh_settings)
        row.addWidget(self.adaptive_box)
        row.addWidget(rb)
        row.addWidget(ref)
        lay.addLayout(row)
        return w

    def _save_key(self) -> None:
        key = self.key_edit.text().strip()
        if not key:
            return
        self.key_edit.clear()
        self.statusBar().showMessage("Checking the key with Gemini...")
        run_async(lambda: self.studio.set_api_key(key), lambda _: self._on_started(None),
                  lambda e: QMessageBox.warning(self, "Gemini key", f"That key did not work: {e}"))

    def _toggle_adaptive(self, on: bool) -> None:
        if self.studio.engine is not None:
            self.studio.engine.set_adaptive(on)
            self._refresh_settings()

    def _rollback(self) -> None:
        if self.studio.engine is None:
            return
        try:
            self.studio.engine.rollback(reason="user rollback from Settings")
        except Exception as exc:
            QMessageBox.information(self, "Rollback", str(exc))
        self._refresh_settings()

    def _refresh_settings(self) -> None:
        engine = self.studio.engine
        if engine is None or engine._config is None:
            self.engine_view.setHtml(f"<p>{html.escape(self.studio.start_error or 'Not connected yet.')}</p>")
            return
        st = engine.status()
        routes = "".join(f"<tr><td>{r}</td><td>{', '.join(v[:2])}</td></tr>" for r, v in st["routes"].items() if v)
        quota = "".join(f"<tr><td>{m}</td><td>{q['used_today']}/{q['daily_limit'] or '?'} today</td>"
                        f"<td>{html.escape(q['blocked'] or 'ok')}</td></tr>" for m, q in list(st["quota"].items())[:10])
        warn = "".join(f"<li>{html.escape(x)}</li>" for x in st["warnings"])
        self.engine_view.setHtml(
            f"<p>Config v{st['version']} ({html.escape(st['reason'])}). Writes up to {st['words_per_call']:,} "
            f"words per request, story memory {st['memory_budget_tokens']:,} tokens, {st['concurrency']} parallel "
            f"jobs. Recent: {html.escape(st['stats'])}</p><table cellspacing=6>{routes}</table>"
            f"<p><b>Quota</b></p><table cellspacing=6>{quota}</table>" + (f"<ul>{warn}</ul>" if warn else ""))
        self.adaptive_box.blockSignals(True)
        self.adaptive_box.setChecked(st["adaptive"])
        self.adaptive_box.blockSignals(False)

    # ---- events -------------------------------------------------------------------------------------
    def _on_library_changed(self, event: str, book_id: str) -> None:
        self._refresh_timer.start()
        if event in ("chapter_text", "chapter", "outline") and book_id == self.write_book.currentData() \
                and not self.editor.hasFocus():
            QTimer.singleShot(450, self._load_chapters)

    def _on_job(self, job) -> None:
        active = self.studio.jobs.active()
        if active:
            j = active[0]
            self.job_bar.show()
            self.job_bar.setValue(int(j.progress * 100))
            self.job_bar.setFormat(f"{j.message[:60]} %p%")
        else:
            self.job_bar.hide()
        if job.status in ("done", "failed", "cancelled"):
            msg = {"done": f"Finished: {job.title}", "failed": f"Failed: {job.title} - {job.error}",
                   "cancelled": f"Stopped: {job.title}"}[job.status]
            self.statusBar().showMessage(msg, 10000)
            if job.status == "failed":
                QMessageBox.warning(self, "Background job", msg)
            if job.kind == "audiobook" and job.status == "done" and job.result and sys.platform == "win32":
                os.startfile(os.path.dirname(str(job.result)))  # type: ignore[attr-defined]
            if self.voice.active and job.status == "done":
                self.voice.send_text(f"(System note: the background job '{job.title}' just finished. "
                                     "Tell the user briefly.)")

    def closeEvent(self, event) -> None:
        self._autosave.stop()
        self._refresh_timer.stop()
        self._save_editor()
        self._closed = True
        self.player.stop()
        self.voice.enable_wake_word(False)
        self.voice.stop()
        wait_for_tasks()
        self.studio.close()
        super().closeEvent(event)
