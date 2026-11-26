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
from jarvis_books.assistant.permissions import PermissionGate, level_of
from jarvis_books.audio import Narrator
from jarvis_books.audio.script import VoiceDirector
from jarvis_books.audio.export import export_audiobook
from jarvis_books.export import export_docx, export_epub, export_markdown
from jarvis_books.jobs import JobManager
from jarvis_books.keystore import load_key
from jarvis_books.phone import PhoneServer, Tunnel, URL_META, phone_line, phone_link
from jarvis_books.telegram import TelegramBridge, forget_bot_key, load_bot_key, save_bot_key
from jarvis_books.voice.notes import VoiceNotes
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
from jarvis_books.nexus.api import NexusHub, NexusServer, TokenBook, systems_for
from jarvis_books.nexus.os1 import OS1
from jarvis_books.nexus.os2 import OS2
from jarvis_books.nexus.os3 import OS3, registry_from_toolbox
from jarvis_books.nexus.search import EngineEmbedder
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
PHONE_AUTOSTART = True  # the phone app's server and secure link start with Jarvis
NEXUS_API_AUTOSTART = True  # the NEXUS API listens on 127.0.0.1 only, behind bearer tokens
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
        # The chat brain stays in memory (keep_alive -1) so a message after a quiet spell doesn't wait for the disk.
        self.local = LocalEngine(local_client or OllamaClient(), state_path=self.home / "local_llm.json",
                                 keep_alive=-1, **({"hardware_probe": probe} if probe else {}))
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
        # Chat, voice, Telegram and the phone app are one conversation with the same Jarvis.
        talk = ("chat", "voice", "telegram", "phone")
        self.assistant = Assistant(self.brain, self.library, self.tools, history_channels=talk)
        self.telegram_assistant = Assistant(self.brain, self.library, self.tools, channel="telegram",
                                            history_channels=talk)
        self.phone_assistant = Assistant(self.brain, self.library, self.tools, channel="phone", history_channels=talk)
        self.fast = FastRouter(self.home / "tool_brain")
        self.fast.enabled = self.settings()["tool_brain"] != "0"
        self.assistant.fast = self.telegram_assistant.fast = self.phone_assistant.fast = self.tools.fast = self.fast
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
        self.assistant.memory = self.war_assistant.memory = self.telegram_assistant.memory = self.memory
        self.phone_assistant.memory = self.memory
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
        self.nexus = OS1(self.home, embedder=EngineEmbedder(self.engine) if self.engine is not None else None,
                         seed_search=self._nexus_seeds)
        self.tools.nexus = self.war_tools.nexus = self.nexus
        self.nexus_os2 = OS2(self.nexus)
        self.tools._nexus_os2 = self.war_tools._nexus_os2 = self.nexus_os2
        self.nexus_os3 = OS3(registry_from_toolbox(self.tools, self.nexus.store.db),
                             selector=lambda text: self.fast.route(text, self.tools.declarations(), level_of))
        self.nexus_hub = NexusHub(systems_for(self.nexus, self.nexus_os2, self.nexus_os3))
        self.nexus_api = NexusServer(self.nexus_hub, TokenBook(self.home))
        self.planner = Planner(self.db)
        self.reminder_clock = ReminderClock(self.planner)
        self.voice_notes = VoiceNotes(self._look if self.engine is not None else None, self._groq_key,
                                      lambda: self.settings()["edge_voice"])
        self.telegram = TelegramBridge(self.library, self.telegram_assistant, self.gate,
                                       lambda: load_bot_key(self.home), planner=self.planner, voice=self.voice_notes)
        self.tools.planner = self.war_tools.planner = self.planner
        self.phone = PhoneServer(self.library, self.phone_assistant, self.gate, voice=self.voice_notes)
        self.tunnel = Tunnel(self.home / "bin", lambda: self.phone.port, on_url=self._on_phone_url)
        self.tools.phone_info = self.war_tools.phone_info = self.phone_info
        self.tools.phone_device = self.war_tools.phone_device = self.phone.device_link
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
        self.lab_clock = LabClock(self.lab, busy=self._lab_busy, defer=self._lab_defer)
        self.research_clock.hold = lambda: self.lab.running
        # A self-upgrade and the Brain Lab take turns: one lab round after each upgrade try.
        self._turn_seen, self._turn_tries, self._lab_turn = 0, -1, False
        self.upgrader.hold = lambda: self.lab.running or (self._lab_turn and self.lab.enabled())
        for box in (self.tools, self.war_tools):
            box.researcher, box.research_clock = self.researcher, self.research_clock
            box.lab, box.lab_clock = self.lab, self.lab_clock
        self.assistant.researcher = self.war_assistant.researcher = self.telegram_assistant.researcher = self.researcher
        self.phone_assistant.researcher = self.researcher
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
                "rsi": "1", "telegram": "1", "phone": "1", "nexus_api": "1"}

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
        elif key == "telegram":
            self.telegram.start() if str(value) != "0" else self.telegram.stop()
        elif key == "phone":
            self._start_phone() if str(value) != "0" else self._stop_phone()
        elif key == "nexus_api":
            self.nexus_api.start() if str(value) != "0" else self.nexus_api.stop()

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
        if self.settings()["telegram"] != "0":
            self.telegram.start()
        if NEXUS_API_AUTOSTART and self.settings()["nexus_api"] != "0":
            self.nexus_api.start()
        if PHONE_AUTOSTART and self.settings()["phone"] != "0":
            self._start_phone()
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
        jobs = ", ".join(j.title for j in self.jobs.active() if j.kind != "upgrade")
        if jobs != getattr(self, "_lab_jobs", ""):
            self._lab_jobs = jobs
            if jobs:
                log.info("brain lab waits for: %s", jobs)
        return bool(jobs) or time.time() - last < IDLE_S


    def _lab_defer(self) -> bool:
        """Other background work using the computer: the researcher, or a self-upgrade between turns."""
        if self.research_clock.working:
            return True
        upgrades = sum(j.kind == "upgrade" for j in self.jobs.active())
        if not upgrades:
            return False
        if self.lab_clock.attempts != self._turn_seen:
            self._turn_seen, self._turn_tries, self._lab_turn = self.lab_clock.attempts, self.upgrader.tries_done, False
        if self.upgrader.tries_done != self._turn_tries:
            self._lab_turn = True  # the upgrade pauses after the try it is on
        return self.upgrader.resting < upgrades

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

    def set_telegram_key(self, key: str) -> dict:
        """Save a bot key from @BotFather and start answering on Telegram. A new key means a new pairing."""
        if key.strip() != (load_bot_key(self.home) or ""):
            save_bot_key(key, self.home)
            self.telegram.forget()
        self.set_setting("telegram", "1")
        self.telegram.restart()
        return self.telegram.status()

    # ---- phone app -------------------------------------------------------------------
    def _start_phone(self) -> None:
        if self.phone.start():
            self.tunnel.start()

    def _stop_phone(self) -> None:
        self.tunnel.stop()
        self.phone.stop()

    def phone_info(self, new_code: bool = True) -> dict:
        """The phone app's link (with a pairing code), or why there isn't one yet."""
        host = self.tunnel.host
        code = self.phone.pair_code() if host and new_code else ""
        problem = None if host else (self.phone.error or self.tunnel.error or "the secure link is still starting")
        return {"link": phone_link(host, code), "code": code, "host": host, "problem": problem,
                "line": phone_line(self.phone.status(), self.tunnel.status(), phone_link(host, code))}

    def _on_phone_url(self, host) -> None:
        before = self.library.get_meta(URL_META, "")
        self.library.set_meta(URL_META, host or "")
        if host and host != before:
            log.info("phone: secure link is up")
            note = ("Jarvis phone app link (open it on your phone, then Add to Home Screen): "
                    f"{phone_link(host, self.phone.pair_code())}")
            if before and any(d.get("kind") == "agent" for d in self.phone.devices()):
                note += f"\nSo I can keep using your Android, paste this into Termux: jarvis-link {host}"
            self.telegram.notify(note)

    def remove_telegram(self) -> None:
        self.telegram.stop()
        forget_bot_key(self.home)
        self.telegram.forget()

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
        if self.local.initialize():
            self.local.warm()
            return
        if "downloaded" not in self.local.problem:
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

    def _nexus_seeds(self, topic: str, count: int) -> list[str]:
        """Starting pages for a topic-only web exploration: the live web search's top results."""
        return [r["url"] for r in self.web.search(topic, count)["results"]][:count]

    def close(self) -> None:
        self.nexus_api.stop()
        self.nexus_hub.close()
        self.nexus_os3.close()
        self.telegram.stop()
        self.tunnel.stop(keep=True)
        self.phone.stop()
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
            self.nexus.close()
            self.db.close()
