"""Composition root: builds and wires every subsystem; the UI and tests use this one object."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from jarvis_books.computer.apikeys import ApiKeys
from jarvis_books.computer.desktop import Desktop
from jarvis_books.computer.files import Computer
from jarvis_books.computer.terminal import Terminal
from jarvis_books.computer.web import Web
from jarvis_books.assistant import Assistant, ToolBox
from jarvis_books.assistant.permissions import PermissionGate
from jarvis_books.audio import Narrator
from jarvis_books.audio.script import VoiceDirector
from jarvis_books.audio.export import export_audiobook
from jarvis_books.export import export_docx, export_epub, export_markdown
from jarvis_books.jobs import JobManager
from jarvis_books.keystore import load_key
from jarvis_books.llm.brain import GEMINI, Brain
from jarvis_books.llm.groq import CHAT_MODEL as GROQ_CHAT, RESEARCH_MODEL as GROQ_RESEARCH, GroqEngine, \
    GroqSearch, load_groq_key, save_groq_key
from jarvis_books.llm.engine import AdaptiveLLMEngine
from jarvis_books.llm.local import LABEL, LocalEngine, OllamaClient
from jarvis_books.llm.models import LLMRequest
from jarvis_books.llm.gemini import GeminiClient
from jarvis_books.llm.store import EngineStore
from jarvis_books.llm.errors import LLMError
from jarvis_books.llm.lab import LABELS, BrainLab, LabClock
from jarvis_books.llm.tool_brain import FastRouter
from jarvis_books.memory import ContextBuilder, StoryIndex
from jarvis_books.memory.longterm import JarvisMemory
from jarvis_books.paths import data_dir, mirror_dir
from jarvis_books.game import Game
from jarvis_books.game.trading import TradingClock
from jarvis_books.planner import Planner, ReminderClock
from jarvis_books.publish.service import Publisher
from jarvis_books.research import RESEARCH_MODEL, RESEARCH_TIME_S, ResearchClock, Researcher
from jarvis_books.storage.capacity import CapacityMonitor
from jarvis_books.storage.db import Database
from jarvis_books.storage.library import Library
from jarvis_books.storage.mirror import BackupManager, BookMirror
from jarvis_books.upgrade import UPGRADE_ANSWER_CAP_S, UPGRADE_LOCAL_CTX, Upgrader
from jarvis_books.warroom import WAR_ROOM_GUIDE, WarRoom
from jarvis_books.writer import BookWriter

log = logging.getLogger(__name__)
TOOL_BRAIN_AUTOBUILD = True  # tests turn this off so they don't spend minutes training
LAB_AUTOSTART = True  # likewise for the Brain Lab's background rounds
IDLE_S = 180  # the Brain Lab waits this long after the author's last message


class Studio:
    def __init__(self, home: Optional[Path] = None, mirror: Optional[Path] = None, client=None,
                 api_key: Optional[str] = None, engine_kwargs: Optional[dict] = None, local_client=None):
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
        probe = (engine_kwargs or {}).get("hardware_probe")
        self.local = LocalEngine(local_client or OllamaClient(), state_path=self.home / "local_llm.json",
                                 **({"hardware_probe": probe} if probe else {}))
        lab_model = self.library.get_meta("lab_model", "")  # the Brain Lab's best model for this computer
        if lab_model:
            self.local.model, self.local.label = lab_model, LABELS.get(lab_model, lab_model)
        # Gemini and LFM3 behind one generate(); the index and narrator use Gemini-only embeddings and speech.
        self.groq = GroqEngine(self._groq_key, GROQ_CHAT)
        self.brain = Brain(self.engine, self.local, self.settings, groq=self.groq)
        self.index = StoryIndex(self.library, self.engine)
        self.context = ContextBuilder(self.library, self.index)
        self.writer = BookWriter(self.library, self.brain, self.index, self.context)
        self.jobs = JobManager(self.library, workers=2)
        self.undo: list = []  # newest last; shared so 'undo that' works from any Jarvis
        self.gate = PermissionGate(self.library)  # one yes/no gate for every Jarvis (voice, chat, War Room)
        self.tools = ToolBox(self.library, self.writer, self.index, self.jobs, gate=self.gate, undo=self.undo)
        self.tools.export_handler = self.export
        self.assistant = Assistant(self.brain, self.library, self.tools, history_channels=("chat", "voice"))
        self.fast = FastRouter(self.home / "tool_brain")
        self.fast.enabled = self.settings()["tool_brain"] != "0"
        self.assistant.fast = self.tools.fast = self.fast
        self.warroom = WarRoom(self.library, self.brain, self.context)
        self.tools.warroom = self.warroom
        # The War Room has its own conversation (voice and text share it) and its own session state.
        self.war_tools = ToolBox(self.library, self.writer, self.index, self.jobs, gate=self.gate, undo=self.undo)
        self.war_tools.warroom = self.warroom
        self.war_assistant = Assistant(self.brain, self.library, self.war_tools, channel="warroom",
                                       guide=WAR_ROOM_GUIDE, per_book=True)
        settings = self.settings()
        self.narrator = Narrator(self.library, self.engine, self.home / "audio", voice=settings["voice"],
                                 edge_voice=settings["edge_voice"])
        self.publisher = Publisher(self.home, self.library, self.narrator, self.jobs)
        self.director = VoiceDirector(self.library, self.brain, self.settings)
        self.tools.director = self.war_tools.director = self.director
        self.narrator.director = self.director
        self.tools.narrator = self.war_tools.narrator = self.narrator
        self.tools.publisher = self.war_tools.publisher = self.publisher
        self.tools.settings_store = self.war_tools.settings_store = self
        self.memory = JarvisMemory(self.library, self.brain, self.settings)
        self.tools.memory = self.war_tools.memory = self.memory
        self.assistant.memory = self.war_assistant.memory = self.memory
        self.writer.guidance = self.memory.writing_guidance
        self.warroom.memory = self.memory
        self.gate.subscribe(self.memory.on_permission)
        # When Gemini has been out for hours, upgrades carry on with Qwen3-4B on this computer, given a long
        # context and plenty of time, since an upgrade reads far more code than a chat turn.
        self.upgrade_local = LocalEngine(self.local.client, model=RESEARCH_MODEL,
                                         state_path=self.home / "upgrade_llm.json",
                                         answer_budget_s=1800.0, prompt_budget_s=1800.0, label="Qwen3-4B",
                                         fit_request=True, stream_cap_s=UPGRADE_ANSWER_CAP_S,
                                         max_ctx=UPGRADE_LOCAL_CTX,
                                         **({"hardware_probe": probe} if probe else {}))
        self.upgrader = Upgrader(self.library, self.brain.only(GEMINI), self.home / "upgrades",
                                 fallback=self.upgrade_local)
        self.upgrader.fallback_name = "Qwen3-4B on this computer"
        self.tools.upgrader = self.war_tools.upgrader = self.upgrader
        self.upgrader.closing = lambda: self.jobs.closing
        # Jarvis's hands on this computer and the internet. Changes wait for the author's yes unless they
        # turned on Full control in Settings.
        self.gate.full_control = lambda: self.settings()["full_control"] == "1"
        self.computer = Computer(self.home / "computer")
        self.terminal = Terminal(self.home / "computer")
        self.desktop = Desktop(self.home / "computer")
        self.web = Web()
        self.web.ai_search = GroqSearch(self._groq_key).search
        for box in (self.tools, self.war_tools):
            box.computer, box.terminal, box.desktop, box.vision = self.computer, self.terminal, self.desktop, \
                self._look if self.engine is not None else None
        self.tools.web = self.war_tools.web = self.web
        self.planner = Planner(self.db)
        self.reminder_clock = ReminderClock(self.planner)
        self.tools.planner = self.war_tools.planner = self.planner
        self.api_keys = ApiKeys(self.home)
        # The researcher thinks with a free model on this computer, so it never uses Gemini's daily requests.
        # Nobody waits on a research round, so Qwen gets room to read whole pages and write full notes.
        self.research_local = LocalEngine(self.local.client, model=RESEARCH_MODEL,
                                          state_path=self.home / "research_llm.json",
                                          answer_budget_s=RESEARCH_TIME_S, prompt_budget_s=RESEARCH_TIME_S,
                                          label="Qwen3-4B",
                                          **({"hardware_probe": probe} if probe else {}))
        self.researcher = Researcher(self.db, self.library, self.web, self.settings, local=self.research_local,
                                     brain=self.brain, notes_dir=self.mirror_root / "_Research")
        self.researcher.backup = self.local
        self.researcher.cloud = GroqEngine(self._groq_key, GROQ_RESEARCH)
        self.research_clock = ResearchClock(self.researcher)
        self.lab = BrainLab(self.home / "brain_lab", self.local.client, self.fast, self.tools.declarations,
                            self.settings, web=self.web, measure_model_tools=self._lab_tool_score,
                            apply=self._apply_lab)
        self.lab_clock = LabClock(self.lab, busy=self._lab_busy, defer=lambda: self.research_clock.working)
        self.research_clock.hold = lambda: self.lab.running
        for box in (self.tools, self.war_tools):
            box.researcher, box.research_clock = self.researcher, self.research_clock
            box.lab, box.lab_clock = self.lab, self.lab_clock
        self.assistant.researcher = self.war_assistant.researcher = self.researcher
        self.tools.api_keys = self.war_tools.api_keys = self.api_keys
        self.game = Game(self.home / "game" / "atlanta.json")
        self.tools.game = self.war_tools.game = self.game
        self.trading_clock = TradingClock(self.game)
        self.ready = threading.Event()
        self.start_error: Optional[str] = None
        self.library.subscribe(self._on_change)

    # ---- settings (non-secret, stored in the library meta table) ---------------------
    DEFAULTS = {"voice": "Charon", "edge_voice": "en-US-AndrewNeural", "jarvis_voice": "Charon", "author": "",
                "wake_word": "1", "narration_speed": "1.0",
                "auto_mute": "1", "dialogue_tags": "3", "memory": "1", "brain": "gemini", "brain_auto": "1",
                "full_control": "0", "researcher": "1", "research_pace": "10", "research_brain": "local", "tool_brain": "1",
                "rsi": "1"}

    def _look(self, system: str, parts: list[dict]) -> str:
        """Gemini looks at an image (LFM3 can't see, so this never falls back to it)."""
        resp = self.engine.generate(LLMRequest(role="chat", system=system, history=[{"role": "user", "parts": parts}],
                                               max_output_tokens=8192, temperature=0.2, purpose="vision"))
        return resp.text.strip()

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
        elif key == "tool_brain":
            self.fast.enabled = str(value) != "0"

    # ---- lifecycle -------------------------------------------------------------------
    def start(self, probe: bool = True) -> None:
        """Slow startup work (model discovery and probes). Run it off the UI thread."""
        self.mirror.start()
        self.reminder_clock.start()  # reminders that came due while the app was closed ring now
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
        if TOOL_BRAIN_AUTOBUILD:
            threading.Thread(target=self._build_tool_brain, name="jarvis-tool-brain", daemon=True).start()
        self.research_clock.start()
        if LAB_AUTOSTART:
            self.lab_clock.start()
        self.trading_clock.start()
        try:
            self._start_local()
        except Exception:
            log.exception("LFM3 start failed")
        if self.engine is not None and not self.start_error:
            self.memory.catch_up()
        if self.brain.ready():
            try:
                self.tools.resume_writing()
            except Exception:
                log.exception("could not resume writing runs")
        if self.engine is not None and not self.start_error:
            try:
                self.tools.resume_upgrades()
            except Exception:
                log.exception("could not resume app upgrades")
        if probe and self.engine is not None and not self.start_error:
            self.engine.refine()

    def _build_tool_brain(self) -> None:
        """Loads the quick tool brain, or trains it (a few minutes, once) when the app's tools have changed."""
        from jarvis_books.llm.tool_eval import load_cases
        self.fast.min_confidence = self.lab.champion_genome()["tool_confidence"]
        self.fast.build(self.tools.declarations(), held_out=load_cases(), **self.lab.tool_kwargs())
        if self.fast.error:
            log.warning("quick tool brain unavailable: %s", self.fast.error)

    def _lab_busy(self) -> bool:
        """The Brain Lab works only while Jarvis has nothing else to do: no running jobs and no recent chat."""
        last = max(getattr(a, "last_used", 0.0) for a in (self.assistant, self.war_assistant))
        jobs = ", ".join(j.title for j in self.jobs.active())
        if jobs != getattr(self, "_lab_jobs", ""):
            self._lab_jobs = jobs
            if jobs:
                log.info("brain lab waits for: %s", jobs)
        return bool(jobs) or time.time() - last < IDLE_S

    def _lab_tool_score(self, model: str, options: dict) -> float:
        """How well a local model picks Jarvis's tools: every third case of the fixed tool test."""
        from jarvis_books.llm.tool_eval import load_cases, run_tool_eval
        from jarvis_books.writer import prompts
        engine = LocalEngine(self.local.client, model=model, label=LABELS.get(model, model))
        if not engine.initialize():
            raise LLMError(engine.problem, kind="unknown")
        report = run_tool_eval(engine, self.tools.declarations(), load_cases()[::3], prompts.ASSISTANT,
                               select=self.tools.select, retries=0)
        return report.accuracy

    def _apply_lab(self, genome: dict) -> None:
        """A better setup from the Brain Lab: its model becomes the brain on this computer."""
        model = genome["model"]
        if model == self.local.model:
            return
        self.local.model, self.local.label = model, LABELS.get(model, model)
        self.local.speed = type(self.local.speed)()
        self.library.set_meta("lab_model", model)
        threading.Thread(target=self.local.initialize, name="jarvis-lab-model", daemon=True).start()

    def _groq_key(self) -> Optional[str]:
        return load_groq_key(self.home)

    def set_groq_key(self, key: str) -> None:
        save_groq_key(key, self.home)

    def set_api_key(self, key: str) -> None:
        from jarvis_books.keystore import save_key
        save_key(key, self.home)
        self.client = GeminiClient(key)
        self.engine = AdaptiveLLMEngine(self.client, EngineStore(self.home / "adaptive_llm.db"))
        for obj in (self.index, self.narrator):
            obj.engine = self.engine
        self.brain.gemini = self.engine
        self.start_error = None
        self.engine.initialize(probe=False)
        threading.Thread(target=self.engine.refine, daemon=True).start()

    def _start_local(self) -> None:
        """Get LFM3 ready on this computer; downloads it once in the background when Ollama is there."""
        if self.local.initialize() or "downloaded" not in self.local.problem:
            return
        if self.library.get_meta("local_download_failed", "") == self.local.model:
            return
        self.download_local()

    def download_local(self):
        def run(progress, cancel):
            try:
                ok = self.local.download(lambda f, msg: progress(f, f"Downloading {LABEL}: {msg}"), cancel.is_set)
            except Exception:
                self.library.set_meta("local_download_failed", self.local.model)
                raise
            if not ok:
                raise RuntimeError(self.local.problem)
            return self.local.status()
        return self.jobs.submit("download", f"Downloading {LABEL} for Jarvis (about 1 GB)", run)

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
        self.research_clock.stop()
        self.lab_clock.stop()
        self.trading_clock.stop()
        self.reminder_clock.stop()
        self.memory.shutdown()
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
