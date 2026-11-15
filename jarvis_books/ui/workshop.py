"""The Workshop tab: a small code editor for projects on the author's computer, with a terminal, Run and
test buttons, and Jarvis (Gemini) proposing code changes shown as a diff to apply or discard."""
from __future__ import annotations

import os
import re
import sys
import threading
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QDir, QFileSystemWatcher, QRegularExpression, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFileSystemModel, QFont, QSyntaxHighlighter, QTextCharFormat
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QFileDialog, QPlainTextEdit, QPushButton, QSplitter,
                             QTreeView, QVBoxLayout, QWidget)

from jarvis_books.computer.files import diff, is_secret
from jarvis_books.errors import JarvisError
from jarvis_books.llm.models import LLMRequest
from jarvis_books.ui.workers import run_async

MAX_OPEN_BYTES = 2_000_000
CODE_SCHEMA = {"type": "OBJECT", "properties": {"summary": {"type": "STRING"}, "content": {"type": "STRING"}},
               "required": ["summary", "content"]}
CODER = ("You are Jarvis, an expert programmer editing one file in the author's project. Return the COMPLETE new "
         "file in 'content' (never a fragment, never markdown fences) and one or two plain sentences in 'summary' "
         "saying what changed. Change only what the request needs, keep the file's style, and keep it working.")
_KEYWORDS = ("and as assert async await break class continue def del elif else except False finally for from global "
             "if import in is lambda None nonlocal not or pass raise return True try while with yield function var "
             "let const param foreach").split()


class CodeHighlighter(QSyntaxHighlighter):
    """Colors for keywords, strings, numbers and comments (Python, PowerShell, JavaScript and friends)."""

    def __init__(self, document):
        super().__init__(document)

        def fmt(color: str, bold: bool = False) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Weight.Bold)
            return f
        self.rules = [(QRegularExpression(r"\b(" + "|".join(_KEYWORDS) + r")\b"), fmt("#0033b3", True)),
                      (QRegularExpression(r"\b\d+(\.\d+)?\b"), fmt("#1750eb")),
                      (QRegularExpression(r"(?<=\bdef |\bclass |\bfunction )\w+"), fmt("#00627a", True)),
                      (QRegularExpression(r"\"[^\"\\n]*\"|'[^'\\n]*'"), fmt("#067d17")),
                      (QRegularExpression(r"(#|//).*$"), fmt("#8c8c8c"))]

    def highlightBlock(self, text: str) -> None:
        for pattern, f in self.rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), f)


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def python_exe() -> str:
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")  # the app itself runs windowless as pythonw.exe
    return str(console if exe.name.lower() == "pythonw.exe" and console.exists() else exe)


class Workshop(QWidget):
    command_finished = pyqtSignal(dict)

    def __init__(self, studio, parent=None):
        super().__init__(parent)
        self.studio = studio
        self.path: Optional[Path] = None
        self.saved_text = ""
        self.pending: Optional[dict] = None  # a change Jarvis proposed, waiting for Apply / Discard
        self._cancel: Optional[threading.Event] = None
        self._closing = False

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Project folder:"))
        self.folder_edit = QLineEdit(str(Path.home()))
        self.folder_edit.returnPressed.connect(lambda: self.open_folder(self.folder_edit.text()))
        top.addWidget(self.folder_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        top.addWidget(browse)
        layout.addLayout(top)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.model = QFileSystemModel(self)
        self.model.setFilter(QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        for col in (1, 2, 3):
            self.tree.hideColumn(col)
        self.tree.setHeaderHidden(True)
        self.tree.doubleClicked.connect(lambda idx: self.open_file(self.model.filePath(idx))
                                        if not self.model.isDir(idx) else None)
        split.addWidget(self.tree)

        right = QSplitter(Qt.Orientation.Vertical)
        editor_box = QWidget()
        ev = QVBoxLayout(editor_box)
        ev.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.file_label = QLabel("Double-click a file to open it.")
        bar.addWidget(self.file_label, 1)
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self.save)
        self.revert_btn = QPushButton("Revert")
        self.revert_btn.clicked.connect(self.revert)
        bar.addWidget(self.save_btn)
        bar.addWidget(self.revert_btn)
        ev.addLayout(bar)
        mono = QFont("Consolas" if sys.platform == "win32" else "Monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.editor = QPlainTextEdit()
        self.editor.setFont(mono)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.textChanged.connect(self._update_title)
        self.highlighter = CodeHighlighter(self.editor.document())
        ev.addWidget(self.editor, 1)
        ask = QHBoxLayout()
        self.ask_edit = QLineEdit()
        self.ask_edit.setPlaceholderText("Ask Jarvis to change this file, e.g. 'add a --verbose option'")
        self.ask_edit.returnPressed.connect(self.ask_jarvis)
        self.ask_btn = QPushButton("Ask Jarvis")
        self.ask_btn.clicked.connect(self.ask_jarvis)
        ask.addWidget(self.ask_edit, 1)
        ask.addWidget(self.ask_btn)
        ev.addLayout(ask)
        self.change_box = QWidget()
        cv = QVBoxLayout(self.change_box)
        cv.setContentsMargins(0, 0, 0, 0)
        self.change_label = QLabel()
        self.change_label.setWordWrap(True)
        self.change_view = QPlainTextEdit()
        self.change_view.setReadOnly(True)
        self.change_view.setFont(mono)
        row = QHBoxLayout()
        self.apply_btn = QPushButton("Apply and save")
        self.apply_btn.clicked.connect(self.apply_change)
        self.discard_btn = QPushButton("Discard")
        self.discard_btn.clicked.connect(self.discard_change)
        row.addStretch(1)
        row.addWidget(self.apply_btn)
        row.addWidget(self.discard_btn)
        cv.addWidget(self.change_label)
        cv.addWidget(self.change_view, 1)
        cv.addLayout(row)
        self.change_box.hide()
        ev.addWidget(self.change_box, 1)
        right.addWidget(editor_box)

        term = QWidget()
        tv = QVBoxLayout(term)
        tv.setContentsMargins(0, 0, 0, 0)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(mono)
        self.output.setMaximumBlockCount(5000)
        tv.addWidget(self.output, 1)
        crow = QHBoxLayout()
        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText(f"Type a {studio.terminal.shell} command and press Enter")
        self.command_edit.returnPressed.connect(lambda: self.run_command(self.command_edit.text()))
        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(lambda: self.run_command(self.command_edit.text()))
        self.run_file_btn = QPushButton("Run file")
        self.run_file_btn.clicked.connect(self.run_file)
        self.test_btn = QPushButton("Run tests")
        self.test_btn.clicked.connect(self.run_tests)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        for w in (self.command_edit, self.run_btn, self.run_file_btn, self.test_btn, self.stop_btn):
            crow.addWidget(w, 1 if w is self.command_edit else 0)
        tv.addLayout(crow)
        right.addWidget(term)
        right.setSizes([500, 220])
        split.addWidget(right)
        split.setSizes([260, 900])
        layout.addWidget(split, 1)
        self.status = QLabel()
        layout.addWidget(self.status)

        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._changed_on_disk)
        self._watch_mtime: Optional[float] = None
        self._poll = QTimer(self)
        self._poll.setInterval(500)
        self._poll.timeout.connect(self._poll_disk)
        self._poll.start()
        self.open_folder(str(Path.home()))
        self._update_title()

    # ---- folders and files ----------------------------------------------------------------------
    @property
    def folder(self) -> Path:
        return Path(self.folder_edit.text())

    @property
    def dirty(self) -> bool:
        return self.path is not None and self.editor.toPlainText() != self.saved_text

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Project folder", str(self.folder))
        if chosen:
            self.open_folder(chosen)

    def open_folder(self, folder: str) -> bool:
        p = Path(os.path.expandvars(os.path.expanduser(folder.strip().strip('"'))))
        if not p.is_dir():
            self.status.setText(f"No folder at {p}")
            return False
        self.folder_edit.setText(str(p))
        self.model.setRootPath(str(p))
        self.tree.setRootIndex(self.model.index(str(p)))
        self.status.setText(f"Project: {p}")
        return True

    def open_file(self, path: str) -> bool:
        p = Path(path)
        if self.dirty and p != self.path:
            self.status.setText(f"Save or revert {self.path.name} first.")
            return False
        if is_secret(p):
            self.status.setText(f"{p.name} holds passwords or keys, so it isn't opened here.")
            return False
        try:
            data = p.read_bytes()
        except OSError as exc:
            self.status.setText(f"Can't open {p.name}: {exc}")
            return False
        if len(data) > MAX_OPEN_BYTES or b"\x00" in data[:8192]:
            self.status.setText(f"{p.name} isn't a text file (or is over 2 MB).")
            return False
        if self.path is not None and str(self.path) in self.watcher.files():
            self.watcher.removePath(str(self.path))
        self.path = p
        try:
            self._watch_mtime = p.stat().st_mtime
        except OSError:
            self._watch_mtime = None
        self.saved_text = data.decode("utf-8", errors="replace")
        self.editor.setPlainText(self.saved_text)
        self.watcher.addPath(str(p))
        self.discard_change()
        self._update_title()
        self.status.setText(f"Opened {p}")
        return True

    def save(self) -> bool:
        if self.path is None:
            return False
        text = self.editor.toPlainText()
        try:
            out = self.studio.computer.write_file(str(self.path), text)
        except (JarvisError, ValueError, OSError) as exc:
            self.status.setText(f"Not saved: {exc}")
            return False
        self.saved_text = text
        path, backup = out["written"], out["backup"]
        self.studio.tools._push_undo(f"saving {path} in the Workshop",
                                     lambda: self.studio.computer.restore(backup, path))
        self._update_title()
        self.status.setText(f"Saved {self.path.name} (the old version is backed up)")
        return True

    def revert(self) -> None:
        if self.path is not None:
            self.editor.setPlainText(self.saved_text)
            self._update_title()

    def _poll_disk(self) -> None:
        """Windows QFileSystemWatcher can miss writes; poll the open file's mtime as a fallback."""
        if self.path is None or not self.path.exists():
            return
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return
        if self._watch_mtime is not None and m != self._watch_mtime:
            self._changed_on_disk(str(self.path))
        self._watch_mtime = m

    def _changed_on_disk(self, path: str) -> None:
        """Jarvis (or another program) changed the open file: show it, unless the author has unsaved edits."""
        if self.path is None or Path(path) != self.path or not self.path.exists():
            return
        if str(self.path) not in self.watcher.files():
            self.watcher.addPath(str(self.path))
        text = self.path.read_text(encoding="utf-8", errors="replace")
        if text == self.saved_text:
            return
        if self.dirty:
            self.status.setText(f"{self.path.name} changed on disk; Revert to load it (your edits would be lost).")
            self.saved_text = text
            return
        self.saved_text = text
        self.editor.setPlainText(text)
        self.status.setText(f"{self.path.name} was changed outside the editor and reloaded.")

    def _update_title(self) -> None:
        has = self.path is not None
        self.save_btn.setEnabled(has and self.dirty)
        self.revert_btn.setEnabled(has and self.dirty)
        self.ask_btn.setEnabled(has)
        self.run_file_btn.setEnabled(has and self._runner(self.path) is not None)
        if has:
            self.file_label.setText(("● " if self.dirty else "") + str(self.path))

    # ---- terminal --------------------------------------------------------------------------------------
    def _runner(self, path: Optional[Path]) -> Optional[str]:
        if path is None:
            return None
        ext = path.suffix.lower()
        win = self.studio.terminal.windows
        q = _ps_quote if win else (lambda s: "'" + str(s).replace("'", "'\\''") + "'")
        call = "& " if win else ""
        if ext == ".py":
            return f"{call}{q(python_exe())} {q(path)}"
        if ext == ".ps1" and win:
            return f"& {q(path)}"
        if ext in (".sh",) and not win:
            return f"bash {q(path)}"
        if ext in (".js", ".mjs"):
            return f"node {q(path)}"
        return None

    def run_file(self) -> None:
        if self.dirty and not self.save():
            return
        cmd = self._runner(self.path)
        if cmd:
            self.run_command(cmd, folder=str(self.path.parent))

    def run_tests(self) -> None:
        if self.dirty and not self.save():
            return
        q = _ps_quote(python_exe()) if self.studio.terminal.windows else "'" + python_exe() + "'"
        self.run_command(("& " if self.studio.terminal.windows else "") + f"{q} -m pytest -q")

    def run_command(self, command: str, folder: str = "") -> bool:
        command = command.strip()
        if not command or self._cancel is not None:
            return False
        folder = folder or str(self.folder)
        self._cancel = cancel = threading.Event()
        self.output.appendPlainText(f"\n{folder}> {command}")
        for b in (self.run_btn, self.run_file_btn, self.test_btn):
            b.setEnabled(False)
        self.stop_btn.setEnabled(True)
        terminal = self.studio.terminal
        run_async(lambda: terminal.run(command, folder, 1800, cancel), self._done,
                  lambda err: self._done({"command": command, "errors": err, "exit_code": None, "ok": False,
                                          "output": "", "seconds": 0}))
        return True

    def stop(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _done(self, result: dict) -> None:
        self._cancel = None
        if self._closing:
            return
        if result.get("output"):
            self.output.appendPlainText(result["output"].rstrip())
        if result.get("errors"):
            self.output.appendPlainText(result["errors"].rstrip())
        tail = result.get("stopped") or f"exit code {result.get('exit_code')}"
        self.output.appendPlainText(f"[{'done' if result.get('ok') else 'failed'}: {tail}, {result.get('seconds')}s]")
        for b in (self.run_btn, self.test_btn):
            b.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.command_edit.clear()
        self._update_title()
        self.command_finished.emit(result)

    # ---- Jarvis writes code ---------------------------------------------------------------------------
    def coder(self):
        """Code is written by Gemini: LFM3 is too small to change code reliably, so it never stands in here."""
        engine = self.studio.engine
        return engine if getattr(engine, "_config", None) is not None else None

    def ask_jarvis(self) -> bool:
        request = self.ask_edit.text().strip()
        if not request or self.path is None:
            return False
        engine = self.coder()
        if engine is None:
            self.status.setText("Writing code needs Gemini, and Gemini isn't available right now "
                                "(LFM3 is too small to change code safely).")
            return False
        path, original = self.path, self.editor.toPlainText()
        listing = ", ".join(sorted(p.name for p in path.parent.iterdir())[:60])
        prompt = (f"File: {path.name}\nOther files in its folder: {listing}\n\nRequest: {request}\n\n"
                  f"Current content of {path.name}:\n{original}")
        self.ask_btn.setEnabled(False)
        self.status.setText("Jarvis is writing the change with Gemini…")

        def work():
            resp = engine.generate(LLMRequest(role="planner", system=CODER, instruction=prompt,
                                              json_schema=CODE_SCHEMA, temperature=0.2, purpose="workshop code",
                                              allow_fallback=True))
            return resp.data or {}

        run_async(work, lambda data: self._proposed(path, original, request, data),
                  lambda err: (self.status.setText(f"Jarvis couldn't write that: {err}"), self._update_title()))
        return True

    def _proposed(self, path: Path, original: str, request: str, data: dict) -> None:
        self._update_title()
        if self._closing or path != self.path:
            return
        new = _strip_fences(data.get("content", ""))
        if not new.strip() or new == original:
            self.status.setText("Jarvis didn't change anything.")
            return
        self.pending = {"path": path, "original": original, "new": new, "request": request}
        self.change_label.setText(f"Jarvis's change: {data.get('summary', '').strip()}  Apply it, or discard it.")
        self.change_view.setPlainText(diff(original, new, path.name, max_lines=400))
        self.change_box.show()
        self.status.setText("Review Jarvis's change below.")

    def apply_change(self) -> bool:
        if not self.pending or self.pending["path"] != self.path:
            return False
        if self.editor.toPlainText() != self.pending["original"]:
            self.status.setText("The file changed since Jarvis read it; ask him again.")
            self.discard_change()
            return False
        self.editor.setPlainText(self.pending["new"])
        self.discard_change()
        return self.save()

    def discard_change(self) -> None:
        self.pending = None
        self.change_box.hide()

    def shutdown(self) -> None:
        self._closing = True
        self.stop()
        self._poll.stop()
        if self.watcher.files():
            self.watcher.removePaths(self.watcher.files())


def _strip_fences(text: str) -> str:
    m = re.fullmatch(r"\s*```[\w+-]*\n(.*?)\n?```\s*", text, re.S)
    return m.group(1) if m else text
