"""Composition root: builds and wires every subsystem; the UI and tests use this one object."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from jarvis_books.assistant import Assistant, ToolBox
from jarvis_books.assistant.permissions import PermissionGate
from jarvis_books.audio import Narrator
from jarvis_books.audio.export import export_audiobook
from jarvis_books.export import export_docx, export_epub, export_markdown
from jarvis_books.jobs import JobManager
from jarvis_books.keystore import load_key
from jarvis_books.llm.engine import AdaptiveLLMEngine
from jarvis_books.llm.gemini import GeminiClient
from jarvis_books.llm.store import EngineStore
from jarvis_books.memory import ContextBuilder, StoryIndex
from jarvis_books.paths import data_dir, mirror_dir
from jarvis_books.publish.service import Publisher
from jarvis_books.storage.capacity import CapacityMonitor
from jarvis_books.storage.db import Database
from jarvis_books.storage.library import Library
from jarvis_books.storage.mirror import BackupManager, BookMirror
from jarvis_books.warroom import WAR_ROOM_GUIDE, WarRoom
from jarvis_books.writer import BookWriter

log = logging.getLogger(__name__)


class Studio:
    def __init__(self, home: Optional[Path] = None, mirror: Optional[Path] = None, client=None,
                 api_key: Optional[str] = None, engine_kwargs: Optional[dict] = None):
        self.home = Path(home) if home else data_dir()
        self.home.mkdir(parents=True, exist_ok=True)
        self.mirror_root = Path(mirror) if mirror else mirror_dir()
        self.db = Database(self.home / "library.db")
        self.library = Library(self.db)
        self.mirror = BookMirror(self.library, self.mirror_root)
        self.backups = BackupManager(self.library, self.mirror_root / "_Backups")
        self.capacity = CapacityMonitor(self.library, self.home / "audio")
        if client is None:
            key = api_key or load_key(self.home)
            client = GeminiClient(key) if key else None
        self.client = client
        self.engine = AdaptiveLLMEngine(client, EngineStore(self.home / "adaptive_llm.db"), **(engine_kwargs or {})) \
            if client else None
        self.index = StoryIndex(self.library, self.engine)
        self.context = ContextBuilder(self.library, self.index)
        self.writer = BookWriter(self.library, self.engine, self.index, self.context)
        self.jobs = JobManager(self.library, workers=2)
        self.undo: list = []  # newest last; shared so 'undo that' works from any Jarvis
        self.gate = PermissionGate(self.library)  # one yes/no gate for every Jarvis (voice, chat, War Room)
        self.tools = ToolBox(self.library, self.writer, self.index, self.jobs, gate=self.gate, undo=self.undo)
        self.tools.export_handler = self.export
        self.assistant = Assistant(self.engine, self.library, self.tools)
        self.warroom = WarRoom(self.library, self.engine, self.context)
        self.tools.warroom = self.warroom
        # The War Room has its own conversation (voice and text share it) and its own session state.
        self.war_tools = ToolBox(self.library, self.writer, self.index, self.jobs, gate=self.gate, undo=self.undo)
        self.war_tools.warroom = self.warroom
        self.war_assistant = Assistant(self.engine, self.library, self.war_tools, channel="warroom",
                                       guide=WAR_ROOM_GUIDE, per_book=True)
        settings = self.settings()
        self.narrator = Narrator(self.library, self.engine, self.home / "audio", voice=settings["voice"],
                                 edge_voice=settings["edge_voice"])
        self.publisher = Publisher(self.home, self.library, self.narrator, self.jobs)
        self.tools.publisher = self.war_tools.publisher = self.publisher
        self.tools.settings_store = self.war_tools.settings_store = self
        self.ready = threading.Event()
        self.start_error: Optional[str] = None
        self.library.subscribe(self._on_change)

    # ---- settings (non-secret, stored in the library meta table) ---------------------
    DEFAULTS = {"voice": "Charon", "edge_voice": "en-US-AndrewNeural", "jarvis_voice": "Charon", "author": "",
                "wake_word": "1", "narration_speed": "1.0",
                "auto_mute": "1"}

    def settings(self) -> dict:
        return {k: self.library.get_meta(f"setting.{k}", v) for k, v in self.DEFAULTS.items()}

    def set_setting(self, key: str, value: str) -> None:
        if key not in self.DEFAULTS:
            raise KeyError(key)
        self.library.set_meta(f"setting.{key}", str(value))
        if key == "voice":
            self.narrator.voice = value
        elif key == "edge_voice":
            self.narrator.edge_voice = value

    # ---- lifecycle -------------------------------------------------------------------
    def start(self, probe: bool = True) -> None:
        """Slow startup work (model discovery and probes). Run it off the UI thread."""
        self.mirror.start()
        try:
            self.backups.backup_if_due()
            self.capacity.enforce()
        except Exception:
            log.exception("storage maintenance failed")
        if self.engine is None:
            self.start_error = "No Gemini API key is set. Add it in Settings."
        else:
            try:
                self.engine.initialize(probe=False)
            except Exception as exc:
                log.exception("engine start failed")
                self.start_error = f"Could not reach Gemini: {exc}"
        self.ready.set()
        if probe and self.engine is not None and not self.start_error:
            self.engine.refine()

    def set_api_key(self, key: str) -> None:
        from jarvis_books.keystore import save_key
        save_key(key, self.home)
        self.client = GeminiClient(key)
        self.engine = AdaptiveLLMEngine(self.client, EngineStore(self.home / "adaptive_llm.db"))
        for obj in (self.index, self.writer, self.assistant, self.narrator):
            obj.engine = self.engine
        self.start_error = None
        self.engine.initialize(probe=False)
        threading.Thread(target=self.engine.refine, daemon=True).start()

    def _on_change(self, event: str, book_id: str, detail: dict) -> None:
        if event == "chapter_text" and self.engine is not None:
            chapter_id = detail.get("chapter_id")
            if chapter_id:
                threading.Thread(target=self._reindex, args=(chapter_id,), daemon=True).start()

    def _reindex(self, chapter_id: str) -> None:
        try:
            self.index.index_chapter(chapter_id)
            self.index.embed_pending()
        except Exception as exc:
            log.info("indexing deferred: %s", exc)

    def export(self, book_id: str, fmt: str, out_dir: Optional[Path] = None) -> dict:
        out = Path(out_dir) if out_dir else self.mirror.folder_for(book_id) / "Exports"
        author = self.settings()["author"]
        if fmt in ("mp3", "m4b"):
            book = self.library.get_book(book_id)
            job = self.jobs.submit("audiobook", f"Making the {fmt.upper()} audiobook of {book.title}",
                                   lambda p, c: str(export_audiobook(self.narrator, book_id, out, fmt, p, c)), book_id)
            return {"started": True, "job_id": job.id, "folder": str(out)}
        fn = {"docx": export_docx, "epub": export_epub, "markdown": export_markdown, "md": export_markdown}[fmt]
        path = fn(self.library, book_id, out, author=author) if fn is not export_markdown else fn(self.library, book_id, out)
        return {"saved": str(path)}

    def close(self) -> None:
        self.jobs.shutdown(wait=False)
        self.mirror.stop()
        try:
            self.mirror.flush()
        except Exception:
            log.exception("final mirror flush failed")
        # Startup probes may still be running on another thread; let them finish writing first.
        started = self.ready.wait(timeout=30)
        if self.engine is not None and started:
            self.engine.shutdown()
        if started:
            self.db.close()
