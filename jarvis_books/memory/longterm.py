"""Jarvis's long-term memory: what he has learned about the author and their books across every
conversation (chat, voice and the War Room). After a conversation, Gemini picks out what is worth
keeping and either adds it or updates an older memory it replaces. Before each answer, the most
relevant memories and recent conversation summaries go into Jarvis's instructions.

Learning is recursive: conversation summaries are folded into bigger summaries, and those again, up to
a long-term picture; reflection passes reread the memories (and the lessons drawn earlier) together with
the author's reactions (ideas accepted, edited or rejected, rewrites of Jarvis's text, yes/no answers) to
merge, retire and draw new lessons; memories that keep helping get stronger and unused ones fade."""
from __future__ import annotations

import difflib
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
LESSON = "lesson"  # drawn by reflection from several memories / reactions, never extracted directly
KIND_LABELS = {"about_you": "about you", "preference": "how you like it", "decision": "decided", "plan": "plan",
               "fact": "fact", LESSON: "lesson"}
ALL_BOOKS, THIS_BOOK = "all books", "this book"
BATCH = 12  # learn after this many new messages...
IDLE_S = 20 * 60  # ...or once a conversation has gone quiet this long
MAX_SHOWN = 12  # memories put in front of Jarvis for each answer
MAX_TRANSCRIPT = 200  # messages read in one pass
MAX_NEW_PER_PASS = 12
MAX_MEMORIES = 1500  # unpinned memories kept; the least important, oldest go first
CHANNEL_NAMES = {"chat": "chat", "voice": "voice", "warroom": "War Room"}
ROLLUP = 5  # summaries folded into one at the next level up
TOP_LEVEL = 3  # level 3 is the long-term picture; new level-3 summaries are merged into it
ROLLUP_SENTENCES = {1: 4, 2: 6, 3: 10}
REFLECT_EVERY = 20  # reflect after this many new or changed memories...
EVENTS_EVERY = 5  # ...or this many new reactions...
REFLECT_DAILY = 24 * 3600  # ...or once a day when anything changed
MIN_FOR_REFLECTION = 5
MAX_REFLECT = 150  # memories reread in one reflection pass
MAX_EVENTS_SHOWN = 60
REINFORCE = 0.25  # strength gained when a recalled memory helped and was not corrected
FADE_TAU_DAYS = 120  # unused memories fade with this time constant per importance point
FADE_BELOW = 0.1  # effective strength under which a memory is retired to the archive
IGNORED_TOOLS = {"remember", "recall_memories", "forget_memory"}

STR = {"type": "STRING"}
SCHEMA = {"type": "OBJECT", "properties": {
    "summary": STR,
    "memories": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "replaces": {"type": "INTEGER"}, "text": STR, "kind": {"type": "STRING", "enum": list(KINDS)},
        "scope": {"type": "STRING", "enum": [ALL_BOOKS, THIS_BOOK]}, "importance": {"type": "INTEGER"}},
        "required": ["replaces", "text", "kind", "scope", "importance"]}}},
    "required": ["summary", "memories"]}

LESSON_SCHEMA = {"type": "OBJECT", "properties": {
    "replaces": {"type": "INTEGER"}, "text": STR, "scope": {"type": "STRING", "enum": [ALL_BOOKS, THIS_BOOK]},
    "confidence": {"type": "INTEGER"}, "evidence": {"type": "ARRAY", "items": {"type": "INTEGER"}},
    "events": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
    "required": ["replaces", "text", "scope", "confidence", "evidence", "events"]}
REFLECT_SCHEMA = {"type": "OBJECT", "properties": {
    "merges": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "ids": {"type": "ARRAY", "items": {"type": "INTEGER"}}, "text": STR,
        "kind": {"type": "STRING", "enum": list(KINDS)}, "importance": {"type": "INTEGER"}},
        "required": ["ids", "text", "kind", "importance"]}},
    "retire": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"id": {"type": "INTEGER"}, "why": STR},
                                          "required": ["id", "why"]}},
    "lessons": {"type": "ARRAY", "items": LESSON_SCHEMA}},
    "required": ["merges", "retire", "lessons"]}
ROLLUP_SCHEMA = {"type": "OBJECT", "properties": {"summary": STR}, "required": ["summary"]}

ROLLUP_PROMPT = (
    "You keep the long-term picture of the work between Jarvis, an AI writing partner, and one author. Combine "
    "these summaries of earlier conversations (oldest first) into one summary of at most {n} sentences. Keep "
    "every decision, plan, open question and anything learned about the author; where a later summary "
    "changes an earlier decision, keep only the later one. Drop small talk. Do not invent anything.")

REFLECT = (
    "You are reflecting on the long-term memory of Jarvis, an AI writing partner, about one author. You see "
    "his memories (with #ids), lessons he drew earlier (kind lesson, with the evidence behind them), and recent "
    "reactions from the author (E-ids: ideas accepted, edited or rejected, rewrites of Jarvis's text, yes/no "
    "answers). Improve the memory:\n"
    "- merges: memories that say the same thing; give all their ids and one combined sentence.\n"
    "- retire: memories that are out of date, contradicted by a newer memory, or no longer useful, each with "
    "a short reason. Never retire something only because it is old.\n"
    "- lessons: general conclusions about the author's taste and way of working that follow from several "
    "memories or reactions together (for example: rejected four gloomy endings, so prefers hopeful endings). "
    "Each lesson cites the memory ids (evidence) and reaction ids (events, numbers only) behind it, at least "
    "two in total; a lesson may build on earlier lessons. A single reaction is not a lesson. To revise an "
    "earlier lesson (new evidence, or contradicted), set replaces to its id; otherwise 0. confidence: 3 = a "
    "strong pattern, 2 = likely, 1 = tentative. scope 'this book' only when it is about this book alone.\n"
    "Lessons are about the author, never story canon. Never record secrets such as passwords or keys. "
    "Return empty lists when nothing needs changing.")

EXTRACT = (
    "You maintain the long-term memory of Jarvis, an AI writing partner, about one author. From a "
    "conversation transcript, write a 1-3 sentence summary of what was discussed and decided, and pick out "
    "only what is worth remembering for months: facts about the author, how they like to work, preferences, "
    "decisions about their books, and plans. Skip small talk, one-off requests, anything already obvious from "
    "the books themselves, and secrets such as passwords or keys. When the author corrects Jarvis ('no, I "
    "meant...'), remember what they wanted as a preference. Write each memory as one short standalone "
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


def _clean(text, limit: int = 1000) -> str:
    return " ".join(str(text or "").split())[:limit]


def _ints(values) -> list[int]:
    out = []
    for v in values or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def _rewrite_sample(before: str, after: str, limit: int = 900) -> str:
    """A few of the sentences the author changed, as 'Jarvis wrote X / the author made it Y'."""
    split = lambda t: [x for x in re.split(r"(?<=[.!?])\s+", t or "") if x.strip()]  # noqa: E731
    a, b = split(before), split(after)
    parts = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        old, new = " ".join(a[i1:i2])[:250], " ".join(b[j1:j2])[:250]
        parts.append(f"Jarvis wrote: '{old}' -> the author made it: '{new}'" if old and new else
                     (f"the author cut: '{old}'" if old else f"the author added: '{new}'"))
    return (" | ".join(parts))[:limit]


class JarvisMemory:
    def __init__(self, library: Library, engine=None, settings: Optional[Callable[[], dict]] = None,
                 clock: Callable[[], float] = time.time, background: bool = True):
        self.library = library
        self.engine = engine
        self.settings = settings
        self.clock = clock
        self.background = background
        self._lock = threading.RLock()  # one learning pass at a time
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
                "importance": m["importance"], "updated_at": m["updated_at"], "evidence": m.get("evidence", [])}

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
                                       importance=row["importance"], pinned=row["pinned"],
                                       evidence=row.get("evidence"), strength=row.get("strength", 1.0))

    def archived(self) -> list[dict]:
        return [{**self.public(m), "why": m["archive_reason"]} for m in self.library.archived_memories()]

    def bring_back(self, memory_id: int) -> dict:
        m = self.library.unarchive_memory(memory_id)
        self.library.log_learning("brought back", m["text"], m["book_id"])
        return self.public(m)

    def log(self, limit: int = 100) -> list[dict]:
        return self.library.learning_log(limit)

    def explain(self, memory_id: int) -> dict:
        """Why Jarvis believes something: the memories and reactions a lesson was drawn from."""
        m = self.library.get_memory(memory_id)
        mem_ids = [i for i in m["evidence"] if i > 0]
        events = self.library.learning_events_by_id([-i for i in m["evidence"] if i < 0])
        evidence = []
        for i in mem_ids:
            try:
                e = self.library.get_memory(i)
                evidence.append({"memory_id": i, "text": e["text"] + (" (retired)" if e["archived_at"] else "")})
            except NotFound:
                continue
        evidence += [{"event_id": i, "text": e["detail"]} for i, e in events.items()]
        return {**self.public(m), "strength": round(self.effective_strength(m), 2), "uses": m["uses"],
                "evidence": evidence}

    def effective_strength(self, m: dict) -> float:
        """Stored strength, faded by how long the memory has gone unused (pinned memories never fade)."""
        if m["pinned"]:
            return max(1.0, m["strength"])
        last = max(m["updated_at"], m.get("last_used_at") or 0.0)
        days = max(0.0, self.clock() - last) / 86400
        return m["strength"] * math.exp(-days / (FADE_TAU_DAYS * max(1, m["importance"])))

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
                + 0.5 * math.exp(-age_days / 60) + 0.4 * min(self.effective_strength(m), 3.0)
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
            big = self.library.big_picture_summaries(book_id, limit=2)
            self.library.touch_memories([m["id"] for m in mems])
        except Exception:
            log.exception("memory recall failed")
            return ""
        if not mems and not sums and not big:
            return ""
        lines = [HEADER]
        if big:
            lines.append("The long view of your work together:")
            for s in big:
                about = f" ({self._scope(s['book_id'])})" if s["book_id"] else ""
                lines.append(f"- {s['summary']}{about}")
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

    def writing_guidance(self, book_id: Optional[str] = None, limit: int = 10) -> str:
        """The author's taste, for drafting, planning, review and revision: lessons and preferences."""
        mems = [m for m in self.library.list_memories(book_id) if m["kind"] in (LESSON, "preference")]
        if not mems:
            return ""
        mems.sort(key=lambda m: (m["pinned"], m["importance"] * min(self.effective_strength(m), 3.0)), reverse=True)
        mems = mems[:limit]
        self.library.touch_memories([m["id"] for m in mems])
        return ("WHAT YOU HAVE LEARNED ABOUT THIS AUTHOR'S TASTE (long-term memory; follow it unless the "
                "instructions, story bible or chapters say otherwise):\n" + "\n".join(f"- {m['text']}" for m in mems))

    # ---- learning from reactions ----------------------------------------------------
    def note(self, kind: str, detail: str, book_id: Optional[str] = None) -> Optional[int]:
        """Record something the author did that Jarvis can learn from (it is not a memory by itself)."""
        if not self.learning():
            return None
        try:
            event_id = self.library.add_learning_event(kind, detail, book_id)
        except NotFound:
            event_id = self.library.add_learning_event(kind, detail, None)
            book_id = None
        if self.engine is not None and self.reflection_due(book_id):
            self._submit(("maintain", book_id), lambda: self.maintain((book_id,)))
        return event_id

    def on_permission(self, event: str, info: dict) -> None:
        """Gate listener: the author's yes/no answers are reactions too."""
        if event != "closed" or info.get("status") not in ("approved", "denied"):
            return
        req = info["request"]
        if req.tool in IGNORED_TOOLS:
            return
        try:
            self.note("said_yes" if info["status"] == "approved" else "said_no",
                      f"Jarvis asked: {req.summary} The author said {'yes' if info['status'] == 'approved' else 'no'}.")
        except Exception:
            log.exception("could not note a yes/no answer")

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
        self._submit((channel, book_id), lambda: self._run(channel, book_id, force))

    def _submit(self, key, fn: Callable[[], None]) -> None:
        if not self.background:
            fn()
            return
        with self._qlock:
            if self._closed or key in self._queued:
                return
            self._queued.add(key)
            self._queue.put((key, fn))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, daemon=True, name="jarvis-memory")
                self._worker.start()

    def catch_up(self) -> None:
        """At startup: learn from conversations that ended while the app was closed, then reflect."""
        for channel in ("chat", "voice"):
            self.schedule(channel)
        for book_id in self.library.message_books("warroom"):
            self.schedule("warroom", book_id)
        if self.engine is not None:
            self._submit(("maintain", "all"), lambda: self.maintain(self.library.learning_scopes()))

    def reflect_now(self) -> None:
        """The author asked: fold summaries and reflect on every scope now (in the background)."""
        if self.engine is not None:
            self._submit(("maintain", "now"), lambda: self.maintain(self.library.learning_scopes(), force=True))

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            key, fn = item
            with self._qlock:
                self._queued.discard(key)
            try:
                fn()
            except Exception:
                log.exception("memory work failed")

    def _run(self, channel: str, book_id: Optional[str], force: bool) -> None:
        try:
            out = self.consolidate(channel, book_id, force=force)
        except Exception as exc:  # tried again after the next conversation
            log.info("memory update deferred: %s", exc)
            return
        if out is not None:
            self.maintain((None, out.get("book_id")))

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
                return {"summary": "", "added": [], "updated": [], "book_id": about}
            transcript = "\n".join(f"{'Author' if m.role == 'user' else 'Jarvis'}: {m.text[:1500]}" for m in msgs)
            known = self.recall(transcript[-3000:], about, k=30)
            book_line = f"The conversation is about the book '{self._scope(about)}'.\n" if about else ""
            known_lines = "\n".join(f"#{m['id']} [{m['scope']}] {m['text']}" for m in known) or "(none yet)"
            ask = (f"{book_line}Known memories:\n{known_lines}\n\nTranscript ({CHANNEL_NAMES.get(channel, channel)}):\n"
                   f"{transcript[-30000:]}")
            resp = self.engine.generate(LLMRequest(role="summarizer", system=EXTRACT, instruction=ask,
                                                   json_schema=SCHEMA, temperature=0.1, purpose="jarvis memory"))
            d = resp.data if isinstance(resp.data, dict) else {}
            added, updated, replaced = self._apply(d.get("memories") or [], {m["id"]: m for m in known}, about)
            summary = _clean(d.get("summary"))[:1200]
            self.library.add_conversation_summary(channel, about, msgs[0].id, msgs[-1].id, summary)
            # Memories Jarvis used during this conversation that were not corrected helped: they get stronger.
            start = msgs[0].created_at - 300
            for m in self.library.list_memories(about):
                if m["id"] not in replaced and (m.get("last_used_at") or 0) >= start:
                    self.library.set_memory_strength(m["id"], m["strength"] + REINFORCE)
            for text in added:
                self.library.log_learning("learned", text, about)
            for text in updated:
                self.library.log_learning("updated", text, about)
            self.library.prune_memories(MAX_MEMORIES)
            try:
                self._embed_missing()
            except Exception as exc:
                log.info("memory embeddings deferred: %s", exc)
            return {"summary": summary, "added": added, "updated": updated, "book_id": about}

    def _apply(self, items: list, known: dict, about: Optional[str]) -> tuple[list[str], list[str], set]:
        added, updated, replaced = [], [], set()
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
                self.library.set_memory_strength(target["id"], 1.0)  # corrected: earned strength starts over
                replaced.add(target["id"])
                updated.append(text)
                continue
            if any(_norm(m["text"]) == _norm(text) for m in self.library.list_memories(scope_book, only_scope=True)):
                continue
            self.library.add_memory(text, kind=kind, book_id=scope_book, source="jarvis", importance=importance)
            added.append(text)
        return added, updated, replaced

    # ---- recursive learning: roll-ups, reflection, fading ------------------------------
    def maintain(self, scopes, force: bool = False) -> dict:
        """Fold summaries and reflect wherever it is due, then let unused memories fade."""
        done = {"rollups": 0, "reflections": 0, "faded": 0}
        for scope in dict.fromkeys(scopes):
            try:
                done["rollups"] += self.rollup(scope)
                if self.reflect(scope, force=force) is not None:
                    done["reflections"] += 1
            except Exception as exc:  # tried again next time
                log.info("memory reflection deferred: %s", exc)
        done["faded"] = len(self.fade())
        self.library.prune_learning_events()
        return done

    def rollup(self, book_id: Optional[str] = None) -> int:
        """Fold every ROLLUP summaries at one level into one at the next; repeat upward to the long-term picture."""
        if self.engine is None or not self.learning():
            return 0
        made = 0
        with self._lock:
            for level in range(TOP_LEVEL + 1):
                rows = self.library.unrolled_summaries(book_id, level)
                if level < TOP_LEVEL:
                    while len(rows) >= ROLLUP:
                        group, rows = rows[:ROLLUP], rows[ROLLUP:]
                        self._fold(group, book_id, level + 1)
                        made += 1
                elif len(rows) >= 2:
                    self._fold(rows, book_id, TOP_LEVEL)
                    made += 1
        return made

    def _fold(self, group: list[dict], book_id: Optional[str], level: int) -> int:
        lines = "\n".join(f"- {time.strftime('%b %d %Y', time.localtime(s['created_at']))}: {s['summary']}"
                          for s in group)
        about = f"The summaries are about the book '{self._scope(book_id)}'.\n" if book_id else ""
        resp = self.engine.generate(LLMRequest(
            role="summarizer", system=ROLLUP_PROMPT.format(n=ROLLUP_SENTENCES[level]),
            instruction=f"{about}Summaries (oldest first):\n{lines}", json_schema=ROLLUP_SCHEMA, temperature=0.1,
            purpose="jarvis memory rollup"))
        summary = _clean((resp.data or {}).get("summary") if isinstance(resp.data, dict) else "", 3000)
        if not summary:
            raise RuntimeError("Gemini returned an empty roll-up")
        new_id = self.library.roll_up_summaries([s["id"] for s in group], book_id, level, summary)
        self.library.log_learning("summarized", f"Folded {len(group)} summaries into a level-{level} summary: "
                                  f"{summary[:300]}", book_id)
        return new_id

    def _reflected_key(self, book_id: Optional[str]) -> str:
        return f"memory.reflected.{book_id or 'all'}"

    def reflection_due(self, book_id: Optional[str] = None) -> bool:
        since = float(self.library.get_meta(self._reflected_key(book_id), "0") or 0)
        mems = self.library.list_memories(book_id, only_scope=True)
        changed = sum(1 for m in mems if m["updated_at"] > since)
        signals = len(self.library.learning_events(book_id, since, limit=EVENTS_EVERY))
        if book_id:
            signals += len(self.library.author_rewrites(book_id, since))
        if changed >= REFLECT_EVERY or signals >= EVENTS_EVERY:
            return True
        return bool(changed or signals) and len(mems) + signals >= MIN_FOR_REFLECTION \
            and self.clock() - since >= REFLECT_DAILY

    def reflect(self, book_id: Optional[str] = None, force: bool = False) -> Optional[dict]:
        """Reread what Jarvis knows about one scope with the author's reactions: merge, retire, draw lessons."""
        if self.engine is None or not self.learning():
            return None
        with self._lock:
            if not force and not self.reflection_due(book_id):
                return None
            key = self._reflected_key(book_id)
            since = float(self.library.get_meta(key, "0") or 0)
            if book_id:  # rewrites become reactions, kept so patterns can build across passes
                for r in self.library.author_rewrites(book_id, since):
                    sample = _rewrite_sample(r["jarvis_text"], r["text"])
                    if sample:
                        self.library.add_learning_event(
                            "rewrite", f"In chapter {r['number']} the author rewrote Jarvis's text: {sample}", book_id)
            mems = sorted(self.library.list_memories(book_id, only_scope=True),
                          key=lambda m: (m["pinned"], m["kind"] == LESSON, m["importance"], m["updated_at"]),
                          reverse=True)[:MAX_REFLECT]
            context = [m for m in self.library.list_memories(None, only_scope=True)
                       if m["kind"] in (LESSON, "preference")][:40] if book_id else []
            known = {m["id"]: m for m in mems + context}
            events = self.library.learning_events(book_id, 0.0, limit=MAX_EVENTS_SHOWN)
            stamp = time.time()
            if len(known) + len(events) < 2:
                self.library.set_meta(key, str(stamp))
                return None
            mem_lines = "\n".join(self._reflect_line(m) for m in known.values()) or "(none)"
            ev_lines = "\n".join(f"E{e['id']} ({time.strftime('%b %d', time.localtime(e['created_at']))}, "
                                 f"{e['kind']}) {e['detail']}" for e in events) or "(none)"
            about = f"This pass is about the book '{self._scope(book_id)}'.\n" if book_id else \
                "This pass is about the author in general (all books).\n"
            resp = self.engine.generate(LLMRequest(
                role="summarizer", system=REFLECT, instruction=f"{about}Memories:\n{mem_lines}\n\nReactions:\n"
                f"{ev_lines}", json_schema=REFLECT_SCHEMA, temperature=0.2, purpose="jarvis memory reflection"))
            d = resp.data if isinstance(resp.data, dict) else {}
            out = self._apply_reflection(d, known, {e["id"] for e in events}, book_id)
            self.library.set_meta(key, str(stamp))
            self.library.log_learning("reflected", f"merged {out['merged']}, retired {out['retired']}, "
                                      f"{len(out['lessons_added'])} new lessons, {len(out['lessons_updated'])} "
                                      "revised", book_id)
            try:
                self._embed_missing()
            except Exception as exc:
                log.info("memory embeddings deferred: %s", exc)
            return out

    def _reflect_line(self, m: dict) -> str:
        flags = ", pinned" if m["pinned"] else ""
        line = f"#{m['id']} [{m['scope'] if 'scope' in m else self._scope(m['book_id'])}{flags}] ({m['kind']}) {m['text']}"
        if m["kind"] == LESSON and m["evidence"]:
            line += " (evidence: " + ", ".join(f"#{i}" if i > 0 else f"E{-i}" for i in m["evidence"]) + ")"
        return line

    def _apply_reflection(self, d: dict, known: dict, event_ids: set, book_id: Optional[str]) -> dict:
        out = {"merged": 0, "retired": 0, "lessons_added": [], "lessons_updated": []}
        gone: set = set()
        for mg in (d.get("merges") or [])[:MAX_NEW_PER_PASS]:
            if not isinstance(mg, dict):
                continue
            ids = [i for i in _ints(mg.get("ids")) if i in known and i not in gone and not known[i]["pinned"]]
            ids = [i for i in ids if known[i]["book_id"] == known[ids[0]]["book_id"]] if ids else []
            text = _clean(mg.get("text"))
            if len(ids) < 2 or len(text) < 4:
                continue
            keep = ids[0]
            kind = mg.get("kind") if mg.get("kind") in KINDS else known[keep]["kind"]
            importance = max([known[i]["importance"] for i in ids] + [int(mg.get("importance") or 1)])
            self.library.update_memory(keep, text=text, kind=kind, importance=importance)
            self.library.set_memory_strength(keep, sum(known[i]["strength"] for i in ids))  # agreement adds up
            for other in ids[1:]:
                self.library.archive_memory(other, f"merged into #{keep}")
                gone.add(other)
            self.library.log_learning("merged", f"{', '.join(f'#{i}' for i in ids)} -> {text}", book_id)
            out["merged"] += 1
        for r in d.get("retire") or []:
            if not isinstance(r, dict):
                continue
            try:
                mid = int(r.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if mid not in known or mid in gone or known[mid]["pinned"]:
                continue
            why = _clean(r.get("why"), 200) or "out of date"
            self.library.archive_memory(mid, why)
            gone.add(mid)
            self.library.log_learning("retired", f"{known[mid]['text']} ({why})", known[mid]["book_id"])
            out["retired"] += 1
        for ls in (d.get("lessons") or [])[:MAX_NEW_PER_PASS]:
            if not isinstance(ls, dict):
                continue
            text = _clean(ls.get("text"))
            evidence = [i for i in _ints(ls.get("evidence")) if i in known] + \
                [-i for i in _ints(ls.get("events")) if i in event_ids]
            if len(text) < 4 or len(evidence) < 2:
                continue  # one memory or one reaction is not a pattern
            confidence = max(1, min(3, int(ls.get("confidence") or 1)))
            scope_book = book_id if ls.get("scope") == THIS_BOOK and book_id else None
            try:
                target = known.get(int(ls.get("replaces") or 0))
            except (TypeError, ValueError):
                target = None
            if target is not None and target["kind"] == LESSON and not target["pinned"] and target["id"] not in gone:
                merged_ev = list(dict.fromkeys(target["evidence"] + evidence))
                self.library.update_memory(target["id"], text=text, importance=confidence, evidence=merged_ev)
                self.library.set_memory_strength(target["id"], 1.0 + 0.25 * (len(merged_ev) - 2))
                self.library.log_learning("revised lesson", text, target["book_id"])
                out["lessons_updated"].append(text)
                continue
            if any(_norm(m["text"]) == _norm(text) for m in self.library.list_memories(scope_book, only_scope=True)):
                continue
            self.library.add_memory(text, kind=LESSON, book_id=scope_book, source="reflection",
                                    importance=confidence, evidence=evidence,
                                    strength=1.0 + 0.25 * (len(evidence) - 2))
            self.library.log_learning("new lesson", text, scope_book)
            out["lessons_added"].append(text)
        return out

    def fade(self) -> list[str]:
        """Retire unpinned memories that have gone unused so long their strength has faded away."""
        faded = []
        for m in self.library.list_memories():
            if not m["pinned"] and self.effective_strength(m) < FADE_BELOW:
                self.library.archive_memory(m["id"], "faded: not needed in a long time")
                faded.append(m["text"])
        if faded:
            self.library.log_learning("faded", f"{len(faded)} unused memories moved to the archive: "
                                      + "; ".join(t[:80] for t in faded[:5]))
        return faded


def _dominant_book(msgs) -> Optional[str]:
    books = Counter(m.book_id for m in msgs if m.book_id)
    return books.most_common(1)[0][0] if books else None
