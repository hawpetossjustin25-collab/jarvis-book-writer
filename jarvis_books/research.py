"""Jarvis's researcher: works in the background, around the clock while the app is open.

It reads what the author talks about and searches for with Jarvis, turns the subjects they keep coming back to
into research topics, and keeps expanding each one from the web: it picks the next question, reads real pages
and saves what it learned with the links. The author or Jarvis can also hand it a topic or a project to build,
pause it, change its focus or drop it. Jarvis reads the notebook when he answers, so he knows what it found.

It thinks with the free model on this computer by default (no daily limit, and Gemini's free requests stay
for talking with the author). Every finding keeps the address of the page it came from; links the model makes
up are thrown away.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from jarvis_books.errors import NotFound, ValidationError
from jarvis_books.llm.errors import LLMError
from jarvis_books.llm.models import LLMRequest

log = logging.getLogger(__name__)

KINDS = ("topic", "project")
STATUSES = ("watching", "active", "paused", "done")
CHANNELS = ("chat", "voice", "warroom")
RESEARCH_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
ACTIVE_AT = 4.0  # interest points before a subject the author keeps raising is researched by itself
ASKED = ACTIVE_AT + 6  # a topic the author or Jarvis hands over is researched straight away
HALF_LIFE_DAYS = 7.0  # interest the author stops showing fades by half each week
OBSERVE_MIN = 3  # new author messages before the researcher reads the conversation again
OBSERVE_MAX = 40
PAGES = 3
SOURCE_CHARS = 2500
MINUTE_LIMIT_WAIT_S = 65.0  # Groq's tokens-per-minute limit refills within a minute
DRY_LIMIT = 5  # rounds in a row with nothing new before a topic rests for a day
BLOCKED_WAIT_S = 20 * 60  # when the search engines limit searches, the topic waits this long
RESEARCH_TIME_S = 300.0  # seconds a free local model may take to read pages and to write notes
DAY = 86400.0
START_DELAY_S = 120.0  # let the app finish starting first
WAIT_STEPS = (60, 300, 900, 1800, 3600)  # no model answering: wait longer each time
NOTE_CHARS = 2500  # of findings put in front of Jarvis for one answer

STR = {"type": "STRING"}
INTEREST_SCHEMA = {"type": "OBJECT", "properties": {
    "interests": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "topic": STR, "why": STR, "strength": {"type": "INTEGER"},
        "kind": {"type": "STRING", "enum": list(KINDS)}},
        "required": ["topic", "why", "strength", "kind"]}}},
    "required": ["interests"]}
QUESTION_SCHEMA = {"type": "OBJECT", "properties": {"question": STR}, "required": ["question"]}
DIGEST_SCHEMA = {"type": "OBJECT", "properties": {
    "findings": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "title": STR, "summary": STR, "url": STR}, "required": ["title", "summary", "url"]}}},
    "required": ["findings"]}

INTEREST = (
    "You are the research assistant of an author who talks with Jarvis. Read what the author said and list "
    "the real-world subjects they show interest in: things they ask about, search for, want to buy, learn, "
    "build or keep coming back to. Skip small talk, app commands (play, stop, open a tab, set a reminder) and "
    "the plot of their own books. strength: 1 = mentioned once, 2 = asked about it, 3 = keeps coming back to it "
    "or plainly cares about it. kind: 'project' when they want to build or make something, else 'topic'. Use a "
    "short name (2 to 6 words) for each subject, the same name as a known topic when it is the same subject. "
    "Return an empty list when there is nothing.")
QUESTION = (
    "You run a research notebook. Pick the single most useful next web search to expand this topic: something "
    "the notes don't cover yet, more specific than what was already asked, current and practical. For a project, "
    "ask what is needed to build it: parts, tools, code libraries, step-by-step guides, costs, pitfalls. Return "
    "one search query of 4 to 12 words.")
DIGEST = (
    "You add to a research notebook. From the pages below, write the new, specific, useful facts about the "
    "topic: names, numbers, dates, prices, steps, how-tos. One finding per page at most, each a short title and "
    "a 2 to 5 sentence summary in plain words, and the exact url of the page it came from (copy it from the "
    "list; never make one up). Leave out pages with nothing useful or new. Return an empty list if none.")

_STOP = {"the", "and", "for", "with", "that", "this", "what", "how", "about", "from", "into", "are", "was",
         "you", "your", "can", "could", "would", "should", "find", "search", "look", "tell", "more", "some",
         "any", "all", "get", "want", "need", "like", "make", "out", "new", "best", "jarvis", "please"}


def _same_url(url: str) -> str:
    """A link as models tend to copy it back: same page whatever the scheme, case of the host, 'www.',
    a trailing slash or a #fragment."""
    u = urlsplit(url.strip())
    host = (u.hostname or "").lower().removeprefix("www.")
    return f"{host}{u.path.rstrip('/')}" + (f"?{u.query}" if u.query else "")


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s$.+#-]", " ", (name or "").lower())).strip()


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9$]{3,}", (text or "").lower()) if w not in _STOP}


def _same(a: str, b: str) -> bool:
    """Two names for one subject: the same words, or mostly the same (at least two words each)."""
    wa, wb = (set(re.findall(r"[a-z0-9$]+", _norm(x))) - {"the", "a", "an", "of", "and", "for"} for x in (a, b))
    if wa == wb:
        return bool(wa) or _norm(a) == _norm(b)
    return min(len(wa), len(wb)) >= 2 and len(wa & wb) / len(wa | wb) >= 0.6


def _slug(name: str) -> str:
    return re.sub(r"[^\w-]+", "-", _norm(name)).strip("-")[:60] or "topic"


class Researcher:
    def __init__(self, db, library, web, settings: Callable[[], dict], local=None, brain=None,
                 clock: Callable[[], float] = time.time, notes_dir: Optional[Path] = None):
        self.db = db
        self.library = library
        self.web = web
        self.settings = settings
        self.local = local  # the model on this computer that researches for free (Qwen3-4B)
        self.backup = None  # the app's own local model (LFM3), when the research model isn't there
        self.cloud = None  # Groq's free cloud model: chosen in Settings, or the last resort when no local model runs
        self.brain = brain  # Gemini first; used only when the author picks it in Settings
        self.clock = clock
        self.pause: Callable[[float], None] = time.sleep
        self.notes_dir = notes_dir
        self.problem = ""
        self.last_round: Optional[dict] = None
        self._lock = threading.RLock()

    # ---- thinking ---------------------------------------------------------------------
    def choice(self) -> str:
        c = self.settings().get("research_brain", "local")
        return c if c in ("local", "groq", "gemini") else "local"

    def uses_gemini(self) -> bool:
        return self.choice() == "gemini"

    def _think(self, system: str, ask: str, schema: dict, purpose: str) -> dict:
        req = LLMRequest(role="summarizer", system=system, instruction=ask, json_schema=schema, temperature=0.2,
                         purpose=purpose)
        if self.uses_gemini():
            if self.brain is None:
                raise LLMError("Gemini is not set up", kind="unavailable")
            data = self.brain.generate(req).data
            self.problem = ""
            return data if isinstance(data, dict) else {}
        problems = []
        chain = [self.cloud, self.local, self.backup] if self.choice() == "groq" else [self.local, self.backup, self.cloud]
        first = next((e for e in chain if e is not None), None)
        for engine in chain:
            if engine is None:
                continue
            if not engine.available and not engine.initialize():
                problems.append(f"{engine.model}: {engine.problem}")
                continue
            try:
                data = self._generate(engine, req)
            except LLMError as exc:
                if exc.kind in ("invalid", "invalid_output"):
                    raise
                problems.append(f"{engine.model}: {exc}")
                continue
            self.problem = "" if engine is first else (
                f"the chosen research model can't answer ({'; '.join(problems)})"[:300] +
                f", so {engine.model} is used")
            return data if isinstance(data, dict) else {}
        raise LLMError("no free model can research right now: " + ("; ".join(problems) or "none set up"),
                       kind="unavailable")

    def _generate(self, engine, req: LLMRequest):
        """Groq's free plan counts tokens per minute, and its web search alone uses most of a minute's share, so the
        notes right after a search are often refused for a few seconds. Waiting that out beats handing the pages to
        the much slower model on this computer."""
        try:
            return engine.generate(req).data
        except LLMError as exc:
            wait = exc.retry_after_s if exc.retry_after_s is not None else MINUTE_LIMIT_WAIT_S
            if engine is not self.cloud or exc.kind != "rate" or wait > MINUTE_LIMIT_WAIT_S:
                raise
            log.info("Groq's per-minute limit is used up; waiting %.0fs", wait)
            self.pause(wait + 1)
            return engine.generate(req).data

    # ---- topics -----------------------------------------------------------------------
    def _row(self, r) -> dict:
        d = dict(r)
        d["findings"] = self.db.query_one("SELECT COUNT(*) FROM research_findings WHERE topic_id=?", (d["id"],))[0]
        d["priority"] = round(self.priority(d), 2)
        return d

    def priority(self, t: dict) -> float:
        age = max(0.0, self.clock() - (t.get("interest_at") or self.clock())) / DAY
        return float(t.get("interest") or 0) * 0.5 ** (age / HALF_LIFE_DAYS)

    def topics(self, statuses: tuple[str, ...] = STATUSES) -> list[dict]:
        rows = self.db.query(f"SELECT * FROM research_topics WHERE status IN ({','.join('?' * len(statuses))})",
                             tuple(statuses))
        order = {s: i for i, s in enumerate(("active", "watching", "paused", "done"))}
        return sorted((self._row(r) for r in rows), key=lambda t: (order[t["status"]], -t["priority"], t["id"]))

    def topic(self, which) -> dict:
        """A topic by id, exact name, or the closest name."""
        if isinstance(which, int) or str(which).strip().isdigit():
            r = self.db.query_one("SELECT * FROM research_topics WHERE id=?", (int(which),))
            if r:
                return self._row(r)
        name = str(which or "").strip()
        if not name:
            raise ValidationError("which research topic?")
        rows = [dict(r) for r in self.db.query("SELECT * FROM research_topics ORDER BY id")]
        for test in (lambda t: _norm(t["name"]) == _norm(name), lambda t: _norm(name) in _norm(t["name"]),
                     lambda t: _same(t["name"], name)):
            hit = [t for t in rows if test(t)]
            if hit:
                return self._row(hit[0])
        raise NotFound(f"no research topic called '{name}'" + (
            "; topics: " + ", ".join(t["name"] for t in rows[:12]) if rows else "; the notebook is empty"))

    def add_topic(self, name: str, focus: str = "", kind: str = "topic", why: str = "",
                  source: str = "you") -> dict:
        name = re.sub(r"\s+", " ", (name or "").strip())[:120]
        if not name:
            raise ValidationError("what should the researcher look into?")
        kind = kind if kind in KINDS else "topic"
        now = self.clock()
        try:
            t = self.topic(name)
        except NotFound:
            tid = self.db.execute(
                "INSERT INTO research_topics (name, kind, focus, why, source, status, interest, interest_at, "
                "created_at, updated_at) VALUES (?,?,?,?,?,'active',?,?,?,?)",
                (name, kind, (focus or "").strip()[:600], (why or "").strip()[:300], source, ASKED, now, now, now)
            ).lastrowid
            self._log(tid, "started", f"{'Project' if kind == 'project' else 'Topic'} added by {source}: {name}")
            return {**self.topic(tid), "new": True}
        self.db.execute(
            "UPDATE research_topics SET status='active', kind=?, focus=?, interest=MAX(interest, ?), interest_at=?, "
            "dry_rounds=0, next_run_at=0, updated_at=? WHERE id=?",
            (kind, (focus or t["focus"]).strip()[:600], ASKED, now, now, t["id"]))
        self._log(t["id"], "started", f"Back on by {source}: {t['name']}")
        return {**self.topic(t["id"]), "new": False}

    def update(self, which, status: Optional[str] = None, focus: Optional[str] = None,
               kind: Optional[str] = None) -> dict:
        t = self.topic(which)
        if status is not None and status not in STATUSES:
            raise ValidationError(f"status must be one of {', '.join(STATUSES)}")
        if kind is not None and kind not in KINDS:
            raise ValidationError(f"kind must be one of {', '.join(KINDS)}")
        now = self.clock()
        changes: dict = {"updated_at": now}
        said = []
        if status:
            changes["status"] = status
            said.append(status)
            if status == "active":
                changes.update(dry_rounds=0, next_run_at=0, interest=max(t["interest"], ASKED), interest_at=now)
        if focus is not None:
            changes.update(focus=focus.strip()[:600], dry_rounds=0, next_run_at=0)
            said.append(f"focus: {focus.strip()[:100]}")
        if kind:
            changes["kind"] = kind
            said.append(kind)
        sets = ", ".join(f"{k}=?" for k in changes)
        self.db.execute(f"UPDATE research_topics SET {sets} WHERE id=?", (*changes.values(), t["id"]))
        self._log(t["id"], "changed", f"{t['name']}: " + (", ".join(said) or "no change"))
        return self.topic(t["id"])

    def delete(self, which) -> dict:
        t = self.topic(which)
        self.db.execute("DELETE FROM research_topics WHERE id=?", (t["id"],))
        self._log(None, "dropped", f"Dropped {t['name']} and its {t['findings']} notes")
        return t

    def findings(self, which=None, query: str = "", limit: int = 20) -> list[dict]:
        where, params = ("WHERE f.topic_id=?", (self.topic(which)["id"],)) if which not in (None, "") else ("", ())
        rows = [dict(r) for r in self.db.query(
            f"SELECT f.*, t.name AS topic FROM research_findings f JOIN research_topics t ON t.id=f.topic_id "
            f"{where} ORDER BY f.id DESC LIMIT 2000", params)]
        q = _words(query)
        if q:
            scored = [(len(q & _words(f"{r['title']} {r['summary']} {r['topic']}")), r) for r in rows]
            rows = [r for s, r in sorted(scored, key=lambda x: -x[0]) if s]
        return rows[:max(1, int(limit or 20))]

    def activity(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM research_log ORDER BY id DESC LIMIT ?", (int(limit),))]

    def _log(self, topic_id: Optional[int], action: str, detail: str) -> None:
        self.db.execute("INSERT INTO research_log (topic_id, action, detail, created_at) VALUES (?,?,?,?)",
                        (topic_id, action, detail[:500], self.clock()))
        self.db.execute("DELETE FROM research_log WHERE id <= (SELECT MAX(id) - 500 FROM research_log)")

    # ---- noticing what the author cares about --------------------------------------------
    def observe(self, force: bool = False) -> Optional[list[dict]]:
        """Read the author's new messages; the subjects they keep raising gain interest and, past a point,
        become research topics. None when there wasn't enough new to read."""
        with self._lock:
            cursor = int(self.library.get_meta("research.cursor", "0") or 0)
            rows = self.db.query(
                f"SELECT id, text FROM messages WHERE id>? AND role='user' AND channel IN "
                f"({','.join('?' * len(CHANNELS))}) ORDER BY id LIMIT ?", (cursor, *CHANNELS, OBSERVE_MAX))
            if not rows or (len(rows) < OBSERVE_MIN and not force):
                return None
            known = self.topics(("active", "watching", "paused"))
            ask = ("Known topics: " + (", ".join(t["name"] for t in known[:40]) or "(none)") +
                   "\n\nWhat the author said, oldest first:\n" + "\n".join(f"- {r['text'][:600]}" for r in rows))
            data = self._think(INTEREST, ask, INTEREST_SCHEMA, "researcher interests")
            self.library.set_meta("research.cursor", str(rows[-1]["id"]))
            noticed = []
            for item in (data.get("interests") or [])[:8]:
                if not isinstance(item, dict) or not str(item.get("topic", "")).strip():
                    continue
                try:
                    strength = max(1, min(3, int(item.get("strength") or 1)))
                except (TypeError, ValueError):
                    strength = 1
                noticed.append(self._notice(str(item["topic"]), str(item.get("why") or ""), strength,
                                            item.get("kind") if item.get("kind") in KINDS else "topic"))
            return noticed

    def _notice(self, name: str, why: str, strength: int, kind: str) -> dict:
        now = self.clock()
        name = re.sub(r"\s+", " ", name.strip())[:120]
        known = [dict(r) for r in self.db.query("SELECT * FROM research_topics ORDER BY id")]
        t = next((k for k in known if _same(k["name"], name)), None)
        if t is None:
            status = "active" if strength >= ACTIVE_AT else "watching"
            tid = self.db.execute(
                "INSERT INTO research_topics (name, kind, why, source, status, interest, interest_at, created_at, "
                "updated_at) VALUES (?,?,?,'noticed',?,?,?,?,?)",
                (name, kind, why[:300], status, float(strength), now, now, now)).lastrowid
            self._log(tid, "noticed", f"You showed interest in {name}" + (f": {why}" if why else ""))
            return self.topic(tid)
        interest = self.priority(t) + strength
        status = t["status"]
        if status == "watching" and interest >= ACTIVE_AT:
            status = "active"
            self._log(t["id"], "started", f"You keep coming back to {t['name']}, so research started")
        self.db.execute("UPDATE research_topics SET interest=?, interest_at=?, status=?, updated_at=?, "
                        "why=CASE WHEN why='' THEN ? ELSE why END WHERE id=?",
                        (interest, now, status, now, why[:300], t["id"]))
        return self.topic(t["id"])

    # ---- researching ------------------------------------------------------------------------
    def next_topic(self) -> Optional[dict]:
        now = self.clock()
        due = [t for t in self.topics(("active",)) if (t["next_run_at"] or 0) <= now]
        return max(due, key=lambda t: (t["priority"], -(t["last_run_at"] or 0)), default=None)

    def step(self, which=None) -> Optional[dict]:
        """One research round on the given topic, or the one most worth it now. None when nothing is due."""
        with self._lock:
            t = self.topic(which) if which not in (None, "") else self.next_topic()
            if t is None:
                return None
            now = self.clock()
            done = [dict(r) for r in self.db.query(
                "SELECT question, title, url FROM research_findings WHERE topic_id=? ORDER BY id DESC LIMIT 40",
                (t["id"],))]
            asked = [r[0] for r in self.db.query(
                "SELECT detail FROM research_log WHERE topic_id=? AND action IN ('found','nothing new') "
                "ORDER BY id DESC LIMIT 15", (t["id"],))]
            ask = (f"Topic: {t['name']} ({t['kind']})\nWhy the author cares: {t['why'] or 'not said'}\n"
                   f"Focus: {t['focus'] or 'anything useful'}\nEarlier rounds: " + ("; ".join(asked) or "none yet")
                   + "\nNotes so far: " + ("; ".join(d["title"] for d in done[:25]) or "none"))
            question = str(self._think(QUESTION, ask, QUESTION_SCHEMA, "researcher question").get("question")
                           or "").strip()[:200] or f"{t['name']} {t['focus']}".strip()
            seen = {r[0] for r in self.db.query("SELECT url FROM research_findings WHERE topic_id=?", (t["id"],))}
            why, blocked = "", False
            try:
                found = self.web.research(question, PAGES, SOURCE_CHARS)
                pages = found.get("sources") or []
                why = "" if pages else found.get("note") or "the search found nothing"
                blocked = bool(found.get("blocked")) and not pages
            except Exception as exc:
                log.info("research search failed: %s", exc)
                pages, why = [], f"the web search failed ({exc})"[:200]
            fresh = [p for p in pages if p.get("url") and p["url"] not in seen]
            if pages and not fresh:
                why = "every page found was read before"
            added: list[dict] = []
            if fresh:
                listing = "\n\n".join(f"[{i + 1}] {p.get('title') or ''}\nurl: {p['url']}\n{p['text'][:SOURCE_CHARS]}"
                                      for i, p in enumerate(fresh))
                data = self._think(DIGEST, f"Topic: {t['name']}\nFocus: {t['focus'] or 'anything'}\n"
                                   f"Search: {question}\n\nPages:\n{listing}", DIGEST_SCHEMA, "researcher notes")
                urls = {_same_url(p["url"]): p["url"] for p in fresh}
                given = [f for f in (data.get("findings") or []) if isinstance(f, dict)]
                for f in given[:PAGES]:
                    url = urls.pop(_same_url(str(f.get("url") or "")), None)
                    if url is None:
                        continue  # a link that isn't one of the pages read is made up
                    title, summary = str(f.get("title") or "").strip()[:200], str(f.get("summary") or "").strip()
                    if not title or not summary:
                        continue
                    self.db.execute(
                        "INSERT INTO research_findings (topic_id, question, title, summary, url, created_at)"
                        " VALUES (?,?,?,?,?,?)", (t["id"], question, title, summary[:1500], url, now))
                    added.append({"title": title, "summary": summary[:1500], "url": url})
                if not added:
                    read = ", ".join(p["url"] for p in fresh)[:300]
                    why = (f"read {len(fresh)} page(s), but the notes the model wrote didn't match them ({read})"
                           if given else f"read {len(fresh)} page(s) with nothing new or useful ({read})")
            if blocked:  # the search engines' limit, not the topic running dry: try again later, no strike
                dry, wait = t["dry_rounds"], max(self.pace_s(), BLOCKED_WAIT_S)
            else:
                dry = 0 if added else t["dry_rounds"] + 1
                wait = 0.0 if added else (DAY if dry >= DRY_LIMIT else min(DAY, self.pace_s() * 2 ** dry))
            self.db.execute("UPDATE research_topics SET rounds=rounds+1, dry_rounds=?, last_run_at=?, next_run_at=?, "
                            "updated_at=? WHERE id=?", (dry, now, now + wait, now, t["id"]))
            rest = ", resting for a day" if dry >= DRY_LIMIT else ""
            self._log(t["id"], "found" if added else "nothing new", f"searched '{question}': " + (
                f"saved {len(added)} note(s)" if added else f"nothing new ({why}){rest}"))
            self.last_round = {"topic": t["name"], "question": question, "added": added, "at": now,
                               "why": "" if added else why}
            if added:
                self._write_notes(t["id"])
            return self.last_round

    def _write_notes(self, topic_id: int) -> Optional[Path]:
        """The topic's notebook as a Markdown file the author can open."""
        if self.notes_dir is None:
            return None
        t = self.topic(topic_id)
        lines = [f"# {t['name']}", "", f"{t['kind'].title()}. {t['why']}".strip()]
        if t["focus"]:
            lines.append(f"Focus: {t['focus']}")
        lines.append("")
        for f in reversed(self.findings(topic_id, limit=500)):
            when = time.strftime("%Y-%m-%d", time.localtime(f["created_at"]))
            lines += [f"## {f['title']}", f["summary"], "", f"Source: {f['url']} ({when})", ""]
        try:
            self.notes_dir.mkdir(parents=True, exist_ok=True)
            path = self.notes_dir / f"{_slug(t['name'])}.md"
            path.write_text("\n".join(lines), encoding="utf-8")
            return path
        except OSError:
            log.warning("could not write research notes", exc_info=True)
            return None

    def notes_file(self, which) -> Optional[Path]:
        t = self.topic(which)
        return self._write_notes(t["id"]) if t["findings"] else None

    # ---- what Jarvis sees -----------------------------------------------------------------
    def prompt(self, query: str = "") -> str:
        """The researcher's section of Jarvis's instructions for one answer (empty when it has nothing)."""
        try:
            topics = self.topics(("active", "watching", "paused"))
            if not topics:
                return ""
            hits = self.findings(query=query, limit=6) if _words(query) else []
        except Exception:
            log.exception("research notes unavailable")
            return ""
        lines = ["Your researcher works in the background (research_notes shows all it found). It is following: "
                 + ", ".join(f"{t['name']} ({t['status']}, {t['findings']} notes)" for t in topics[:10]) + "."]
        used = len(lines[0])
        if hits:
            lines.append("Its notes that match this message (give the links when you use them):")
            for f in hits:
                line = f"- [{f['topic']}] {f['title']}: {f['summary'][:400]} ({f['url']})"
                if used + len(line) > NOTE_CHARS:
                    break
                lines.append(line)
                used += len(line)
        return "\n".join(lines)

    # ---- the background loop ----------------------------------------------------------------
    def enabled(self) -> bool:
        return self.settings().get("researcher", "1") == "1"

    def pace_s(self) -> float:
        try:
            minutes = float(self.settings().get("research_pace", "10"))
        except ValueError:
            minutes = 10.0
        return max(1.0, minutes) * 60

    def tick(self) -> Optional[dict]:
        """What the background loop does each time: notice new interests, then research one round."""
        self.observe()
        return self.step()

    def _thinks_with(self) -> str:
        if self.uses_gemini():
            return "Gemini"
        if self.choice() == "groq" and self.cloud is not None:
            return f"Groq {self.cloud.model} (free online plan)"
        return f"{self.local.model} on this computer (free)" if self.local is not None else "nothing set up"

    def status(self) -> dict:
        last = self.activity(1)
        return {"on": self.enabled(), "minutes_between_rounds": round(self.pace_s() / 60, 1),
                "thinks_with": self._thinks_with(),
                "problem": self.problem,
                "topics": [{k: t[k] for k in ("id", "name", "kind", "status", "focus", "findings", "rounds")}
                           | {"interest": t["priority"]} for t in self.topics()[:30]],
                "last_activity": last[0]["detail"] if last else "nothing yet",
                "notes_folder": str(self.notes_dir) if self.notes_dir else ""}


class ResearchClock:
    """Runs the researcher on its own thread: a round every few minutes while it's on, waiting longer when no
    model can answer, until the app closes."""

    def __init__(self, researcher: Researcher, start_delay: float = START_DELAY_S):
        self.researcher = researcher
        self.start_delay = start_delay
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.failures = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="researcher", daemon=True)
        self._thread.start()

    def poke(self) -> None:
        """Run a round now (the author turned it on or gave it something new)."""
        self._wake.set()

    def _wait(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _run(self) -> None:
        self._wait(self.start_delay)
        while not self._stop.is_set():
            delay = self.researcher.pace_s()
            if self.researcher.enabled():
                try:
                    self.researcher.tick()
                    self.failures = 0
                except LLMError as exc:
                    delay = WAIT_STEPS[min(self.failures, len(WAIT_STEPS) - 1)]
                    self.failures += 1
                    self.researcher.problem = str(exc)[:300]
                    log.info("researcher waiting %ss: %s", delay, exc)
                except Exception:
                    log.exception("research round failed")
            if self._stop.is_set():
                break
            self._wait(delay)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
