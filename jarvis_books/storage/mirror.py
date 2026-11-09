"""Mirror every book to readable Markdown files and keep rolling database backups.

The mirror folder lives in OneDrive Documents, so books are synced off the laptop and can be
opened in any editor even without the app.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from jarvis_books.errors import NotFound
from jarvis_books.storage.library import Library
from jarvis_books.text import safe_filename

log = logging.getLogger(__name__)


class BookMirror:
    def __init__(self, library: Library, root: Path, debounce_s: float = 1.5):
        self.library = library
        self.root = Path(root)
        self.debounce_s = debounce_s
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._folders: dict[str, Path] = {}

    def start(self) -> None:
        self.library.subscribe(self._on_change)

    def stop(self) -> None:
        self.library.unsubscribe(self._on_change)
        self.flush()

    def _on_change(self, kind: str, book_id: str, detail: dict) -> None:
        if kind == "book" and detail.get("action") == "deleted":
            return
        with self._lock:
            self._pending.add(book_id)
            if self.debounce_s <= 0:
                pending, self._pending = self._pending, set()
            else:
                if self._timer is None:
                    self._timer = threading.Timer(self.debounce_s, self.flush)
                    self._timer.daemon = True
                    self._timer.start()
                return
        for bid in pending:
            self.write_book(bid)

    def flush(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, set()
            self._timer = None
        for bid in pending:
            try:
                self.write_book(bid)
            except Exception:
                log.exception("mirror write failed for %s", bid)

    def folder_for(self, book_id: str) -> Path:
        book = self.library.get_book(book_id)
        folder = self.root / f"{safe_filename(book.title)} [{book.id[:6]}]"
        old = self._folders.get(book_id)
        if old and old != folder and old.exists() and not folder.exists():
            old.rename(folder)  # book was renamed
        else:
            for existing in self.root.glob(f"* [{book.id[:6]}]"):
                if existing != folder and not folder.exists():
                    existing.rename(folder)
        self._folders[book_id] = folder
        return folder

    def write_book(self, book_id: str) -> Path | None:
        try:
            book = self.library.get_book(book_id)
        except NotFound:
            return None
        folder = self.folder_for(book_id)
        chapters_dir = folder / "Chapters"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        chapters = self.library.list_chapters(book_id)
        wanted: set[str] = set()
        outline_lines = [f"# {book.title} - Outline", ""]
        for ch in chapters:
            outline_lines += [f"## {ch.heading}", ch.outline or "(no outline yet)", ""]
            if ch.text.strip():
                name = f"{ch.number:03d} - {safe_filename(ch.title or 'Chapter')}.md"
                wanted.add(name)
                _write_if_changed(chapters_dir / name, f"# {ch.heading}\n\n{ch.text.strip()}\n")
        for stale in chapters_dir.glob("*.md"):
            if stale.name not in wanted:
                stale.unlink()
        _write_if_changed(folder / "Outline.md", "\n".join(outline_lines))
        bible_lines = [f"# {book.title} - Story Bible", "", f"**Genre:** {book.genre}  ", f"**Logline:** {book.logline}",
                       "", f"**Premise:** {book.premise}", ""]
        current_kind = None
        for entry in self.library.list_bible(book_id):
            if entry.kind != current_kind:
                current_kind = entry.kind
                bible_lines += [f"## {entry.kind.title()}s", ""]
            bible_lines.append(f"- **{entry.name}**: {entry.summary}")
        _write_if_changed(folder / "Story Bible.md", "\n".join(bible_lines) + "\n")
        full = [f"# {book.title}", ""] + [f"## {c.heading}\n\n{c.text.strip()}\n" for c in chapters if c.text.strip()]
        _write_if_changed(folder / f"{safe_filename(book.title)} - Full Manuscript.md", "\n".join(full))
        return folder


def _write_if_changed(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


class BackupManager:
    """Copies the live database to the mirror folder with SQLite's online backup API."""

    def __init__(self, library: Library, folder: Path, keep: int = 10, min_interval_s: float = 6 * 3600):
        self.library = library
        self.folder = Path(folder)
        self.keep = keep
        self.min_interval_s = min_interval_s

    def backups(self) -> list[Path]:
        return sorted(self.folder.glob("library-*.db"))

    def backup_if_due(self, now: float | None = None) -> Path | None:
        now = time.time() if now is None else now
        last = float(self.library.get_meta("last_backup_at", "0") or 0)
        if now - last < self.min_interval_s:
            return None
        return self.backup(now)

    def backup(self, now: float | None = None) -> Path:
        now = time.time() if now is None else now
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        target = self.folder / f"library-{stamp}.db"
        tmp = target.with_suffix(".tmp")
        self.library.db.backup_to(tmp)
        tmp.replace(target)
        self.library.set_meta("last_backup_at", str(now))
        for old in self.backups()[:-self.keep]:
            old.unlink(missing_ok=True)
        return target

    def restore(self, backup: Path, live_path: Path) -> None:
        """Restore a backup over the live database file (app must reopen the database)."""
        shutil.copy2(backup, live_path)
