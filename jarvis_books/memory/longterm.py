"""Jarvis's long-term memory: what he has learned about the author and their books across every
conversation (chat, voice and the War Room). After a conversation, Gemini picks out what is worth
keeping and either adds it or updates an older memory it replaces. Before each answer, the most
relevant memories and recent conversation summaries go into Jarvis's instructions."""
from __future__ import annotations

import logging
import math
import queue
import re
import threading
import time
from collections import Counter
from typing import Callable, Optional

from jarvis_books.errors import NotFound
from jarvis_books.llm.models import LLMRequest
from jarvis_books.memory.index import _cosine, _pack, _unpack
from jarvis_books.storage.library import Library

log = logging.getLogger(__name__)

KINDS = ("about_you", "preference", "decision", "plan", "fact")
KIND_LABELS = {"about_you": "about you", "preference": "how you like it", "decision": "decided", "plan": "plan",
               "fact": "fact"}
ALL_BOOKS, THIS_BOOK = "all books", "this book"
BATCH = 12  # learn after this many new messages...
IDLE_S = 20 * 60  # ...or once a conversation has gone quiet this long
MAX_SHOWN = 12  # memories put in front of Jarvis for each answer
MAX_TRANSCRIPT = 200  # messages read in one pass
MAX_NEW_PER_PASS = 12
MAX_MEMORIES = 1500  # unpinned memories kept; the least important, oldest go first
CHANNEL_NAMES = {"chat": "chat", "voice": "voice", "warroom": "War Room"}

STR = {"type": "STRING"}
SCHEMA = {"type": "OBJECT", "properties": {
    "summary": STR,
    "memories": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "replaces": {"type": "INTEGER"}, "text": STR, "kind": {"type": "STRING", "enum": list(KINDS)},
        "scope": {"type": "STRING", "enum": [ALL_BOOKS, THIS_BOOK]}, "importance": {"type": "INTEGER"}},
        "required": ["replaces", "text", "kind", "scope", "importance"]}}},
    "required": ["summary", "memories"]}

EXTRACT = (
    "You maintain the long-term memory of Jarvis, an AI writing partner, about one author. From a "
    "conversation transcript, write a 1-3 sentence summary of what was discussed and decided, and pick out "
    "only what is worth remembering for months: facts about the author, how they like to work, preferences, "
    "decisions about their books, and plans. Skip small talk, one-off requests, anything already obvious from "
    "the books themselves, and secrets such as passwords or keys. Write each memory as one short standalone "
    "sentence that names the book or character it is about. When a memory updates or contradicts a known "
    "memory, set replaces to that memory's id and write the new version; otherwise replaces is 0. scope is "
    "'this book' for things about the book being discussed, else 'all books'. importance: 3 = the author said "
    "it matters or it is a lasting preference, 2 = useful, 1 = minor. Return no memories if nothing qualifies.")

HEADER = ("LONG-TERM MEMORY (what you remember from past conversations with the author; #ids are for "
          "forget_memory). Use it naturally and don't recite it. It is not story canon: if a memory disagrees "
          "with the chapters or story bible, trust the book and mention the difference.")

_STOP = {"the", "and", "for", "that", "this", "with", "you", "are", "was", "but", "not", "have", "has", "his",
         "her", "she", "they", "them", "what", "when", "about", "can", "will", "would", "should", "just", "like",
         "from", "into", "your", "our", "all", "any", "how", "who", "why", "please", "jarvis"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if len(w) > 2 and w not in _STOP}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


def _clean(text) -> str:
    return " ".join(str(text or "").split())[:1000]


class JarvisMemory:
    def __init__(self, library: Library, engine=None, settings: Optional[Callable[[], dict]] = None,
                 clock: Callable[[], float] = time.time, background: bool = True):
        self.library = library
        self.engine = engine
        self.settings = settings
        self.clock = clock
        self.background = background
        self._lock = threading.Lock()  # one learning pass at a time
        self._qlock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._queued: set = set()
        self._worker: Optional[threading.Thread] = None
        self._closed = False

    def learning(self) -> bool:
        return self.settings is None or self.settings().get("memory", "1") == "1"

    # ---- what the author sees and controls ------------------------------------------
    def _scope(self, book_id: Optional[str]) -> str:
        if not book_id:
            return ALL_BOOKS
        try:
            return self.library.get_book(book_id).title
        except NotFound:
            return ALL_BOOKS

    def public(self, m: dict) -> dict:
        return {"id": m["id"], "text": m["text"], "kind": m["kind"], "scope": self._scope(m["book_id"]),
                "book_id": m["book_id"], "pinned": m["pinned"], "source": m["source"],
                "importance": m["importance"], "updated_at": m["updated_at"]}

    def all(self) -> list[dict]:
        return [self.public(m) for m in self.library.list_memories()]

    def remember(self, text: str, book_id: Optional[str] = None, kind: str = "fact", source: str = "you",
                 importance: int = 3) -> dict:
        """Save a memory now. Saying the same thing again refreshes the old memory instead of adding a copy."""
        text = _clean(text)
        same = [m for m in self.library.list_memories(book_id, only_scope=True) if _norm(m["text"]) == _norm(text)]
        if same:
            m = self.library.update_memory(same[0]["id"], importance=max(same[0]["importance"], importance))
            return {**self.public(m), "new": False}
        m = self.library.add_memory(text, kind=kind, book_id=book_id, source=source, importance=importance)
        return {**self.public(m), "new": True}

    def edit(self, memory_id: int, text: str) -> dict:
        return self.public(self.library.update_memory(memory_id, text=_clean(text), source="you", importance=3))

    def toggle_pin(self, memory_id: int) -> dict:
        m = self.library.get_memory(memory_id)
        return self.public(self.library.update_memory(memory_id, pinned=not m["pinned"]))

    def forget(self, memory_id: int) -> dict:
        return self.library.delete_memory(memory_id)

    def restore(self, row: dict) -> dict:
        """Put a forgotten memory back (undo)."""
        return self.library.add_memory(row["text"], kind=row["kind"], book_id=row["book_id"], source=row["source"],
                                       importance=row["importance"], pinned=row["pinned"])

    # ---- recall ---------------------------------------------------------------------
    def recall(self, query: str = "", book_id: Optional[str] = None, k: int = MAX_SHOWN) -> list[dict]:
        """The memories that matter most right now: pinned first, then by how well they match what the
        author is saying (keywords and Gemini embeddings), how important they are and how recent."""
        mems = self.library.list_memories(book_id)
        if not mems:
            return []
        q = _tokens(query)
        sem = self._semantic(query, mems) if q and len(mems) > k else {}
        now = self.clock()

        def score(m: dict) -> float:
            kw = len(q & _tokens(m["text"])) / math.sqrt(len(q)) if q else 0.0
            age_days = max(0.0, now - m["updated_at"]) / 86400
            return (100.0 if m["pinned"] else 0.0) + 2.0 * sem.get(m["id"], 0.0) + kw + 0.3 * m["importance"] \
                + 0.5 * math.exp(-age_days / 60)
        return [self.public(m) for m in sorted(mems, key=score, reverse=True)[:k]]

    def _semantic(self, query: str, mems: list[dict]) -> dict[int, float]:
        if self.engine is None:
            return {}
        try:
            self._embed_missing(mems)
            model, vectors = self.engine.embed([query], "RETRIEVAL_QUERY")
        except Exception as exc:  # keyword ranking still works offline or out of quota
            log.info("memory search without embeddings: %s", exc)
            return {}
        stored = {r["id"]: r for r in self.library.memory_embeddings([m["id"] for m in mems])}
        return {i: _cosine(vectors[0], _unpack(r["embedding"])) for i, r in stored.items()
                if r["embedding"] is not None and r["embed_model"] == model}

    def _embed_missing(self, mems: Optional[list[dict]] = None, limit: int = 64) -> None:
        rows = [r for r in self.library.memory_embeddings([m["id"] for m in mems] if mems is not None else None)
                if r["embedding"] is None][:limit]
        if rows and self.engine is not None:
            model, vectors = self.engine.embed([r["text"] for r in rows], "RETRIEVAL_DOCUMENT")
            self.library.set_memory_embeddings([(r["id"], _pack(v)) for r, v in zip(rows, vectors)], model)

    def prompt(self, query: str = "", book_id: Optional[str] = None) -> str:
        """The memory section of Jarvis's instructions for one answer (empty when he remembers nothing)."""
        try:
            mems = self.recall(query, book_id)
            sums = self.library.conversation_summaries(book_id, limit=4)
        except Exception:
            log.exception("memory recall failed")
            return ""
        if not mems and not sums:
            return ""
        lines = [HEADER]
        for m in mems:
            tag = m["scope"] if m["book_id"] else KIND_LABELS.get(m["kind"], m["kind"])
            lines.append(f"- #{m['id']} [{tag}] {m['text']}")
        if sums:
            lines.append("Earlier conversations (newest first):")
            for s in sums:
                when = time.strftime("%b %d", time.localtime(s["created_at"]))
                about = f", {self._scope(s['book_id'])}" if s["book_id"] else ""
                lines.append(f"- {when} ({CHANNEL_NAMES.get(s['channel'], s['channel'])}{about}): {s['summary']}")
        return "\n".join(lines)

    # ---- learning from conversations ------------------------------------------------
    def _pending(self, channel: str, book_id: Optional[str], limit: int):
        after = self.library.last_summarized_id(channel, book_id)
        return self.library.messages_after(channel, after, book_id, limit=limit)

    def _is_due(self, msgs) -> bool:
        return bool(msgs) and (len(msgs) >= BATCH or self.clock() - msgs[-1].created_at >= IDLE_S)

    def due(self, channel: str, book_id: Optional[str] = None) -> bool:
        return self._is_due(self._pending(channel, book_id, BATCH))

    def schedule(self, channel: str, book_id: Optional[str] = None, force: bool = False) -> None:
        """Learn from a conversation in the background once it is long enough, has gone quiet, or ended."""
        if self.engine is None:
            return
        try:
            if not (self._pending(channel, book_id, 1) if force else self.due(channel, book_id)):
                return
        except Exception:
            log.exception("memory check failed")
            return
        if not self.background:
            self._run(channel, book_id, force)
            return
        key = (channel, book_id)
        with self._qlock:
            if self._closed or key in self._queued:
                return
            self._queued.add(key)
            self._queue.put((key, force))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, daemon=True, name="jarvis-memory")
                self._worker.start()

    def catch_up(self) -> None:
        """At startup: learn from conversations that ended while the app was closed."""
        for channel in ("chat", "voice"):
            self.schedule(channel)
        for book_id in self.library.message_books("warroom"):
            self.schedule("warroom", book_id)

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            (channel, book_id), force = item
            with self._qlock:
                self._queued.discard((channel, book_id))
            self._run(channel, book_id, force)

    def _run(self, channel: str, book_id: Optional[str], force: bool) -> None:
        try:
            self.consolidate(channel, book_id, force=force)
        except Exception as exc:  # tried again after the next conversation
            log.info("memory update deferred: %s", exc)

    def shutdown(self, timeout: float = 15.0) -> None:
        with self._qlock:
            self._closed = True
            worker = self._worker
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._queue.put(None)
        if worker is not None and worker.is_alive():
            worker.join(timeout)

    def consolidate(self, channel: str, book_id: Optional[str] = None, force: bool = False) -> Optional[dict]:
        """One learning pass over the messages not learned from yet. Returns what changed, or None."""
        with self._lock:
            msgs = self._pending(channel, book_id, MAX_TRANSCRIPT)
            if not msgs or (not force and not self._is_due(msgs)) or self.engine is None:
                return None
            about = book_id or _dominant_book(msgs)
            if not self.learning():  # the author turned learning off: mark it read, keep nothing
                self.library.add_conversation_summary(channel, about, msgs[0].id, msgs[-1].id, "")
                return {"summary": "", "added": [], "updated": []}
            transcript = "\n".join(f"{'Author' if m.role == 'user' else 'Jarvis'}: {m.text[:1500]}" for m in msgs)
            known = self.recall(transcript[-3000:], about, k=30)
            book_line = f"The conversation is about the book '{self._scope(about)}'.\n" if about else ""
            known_lines = "\n".join(f"#{m['id']} [{m['scope']}] {m['text']}" for m in known) or "(none yet)"
            ask = (f"{book_line}Known memories:\n{known_lines}\n\nTranscript ({CHANNEL_NAMES.get(channel, channel)}):\n"
                   f"{transcript[-30000:]}")
            resp = self.engine.generate(LLMRequest(role="summarizer", system=EXTRACT, instruction=ask,
                                                   json_schema=SCHEMA, temperature=0.1, purpose="jarvis memory"))
            d = resp.data if isinstance(resp.data, dict) else {}
            added, updated = self._apply(d.get("memories") or [], {m["id"]: m for m in known}, about)
            summary = _clean(d.get("summary"))[:1200]
            self.library.add_conversation_summary(channel, about, msgs[0].id, msgs[-1].id, summary)
            self.library.prune_memories(MAX_MEMORIES)
            try:
                self._embed_missing()
            except Exception as exc:
                log.info("memory embeddings deferred: %s", exc)
            return {"summary": summary, "added": added, "updated": updated}

    def _apply(self, items: list, known: dict, about: Optional[str]) -> tuple[list[str], list[str]]:
        added, updated = [], []
        for item in items[:MAX_NEW_PER_PASS]:
            if not isinstance(item, dict):
                continue
            text = _clean(item.get("text"))
            if len(text) < 4:
                continue
            kind = item.get("kind") if item.get("kind") in KINDS else "fact"
            importance = max(1, min(3, int(item.get("importance") or 2)))
            scope_book = about if item.get("scope") == THIS_BOOK and about else None
            try:
                target = known.get(int(item.get("replaces") or 0))
            except (TypeError, ValueError):
                target = None
            if target is not None:
                current = self.library.get_memory(target["id"])
                if current["pinned"] or _norm(current["text"]) == _norm(text):
                    continue  # pinned memories only change when the author changes them
                self.library.update_memory(target["id"], text=text, kind=kind,
                                           importance=max(importance, current["importance"]))
                updated.append(text)
                continue
            if any(_norm(m["text"]) == _norm(text) for m in self.library.list_memories(scope_book, only_scope=True)):
                continue
            self.library.add_memory(text, kind=kind, book_id=scope_book, source="jarvis", importance=importance)
            added.append(text)
        return added, updated


def _dominant_book(msgs) -> Optional[str]:
    books = Counter(m.book_id for m in msgs if m.book_id)
    return books.most_common(1)[0][0] if books else None
