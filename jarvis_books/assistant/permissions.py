"""The yes/no gate in front of everything Jarvis can do.

Each tool has a permission level. Reading runs straight away; anything that changes a book, a setting, the
outside world or the app itself waits for the author's yes. The check lives here in code, not in Jarvis's
instructions, so Gemini cannot skip it: when a guarded tool is called the app stores the exact action, asks
the author, and runs that stored action only when the author's own words (typed or spoken) or a button say yes.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

READ, SAFE, CHANGE, OUTSIDE, SYSTEM = "read", "safe", "change", "outside", "system"
LEVELS = (READ, SAFE, CHANGE, OUTSIDE, SYSTEM)
NEEDS_YES = (CHANGE, OUTSIDE, SYSTEM)
LEVEL_LABELS = {READ: "Read", SAFE: "Safe change", CHANGE: "Change", OUTSIDE: "Outside world", SYSTEM: "System"}

TOOL_LEVELS: dict[str, str] = {
    "list_books": READ, "open_book": READ, "read_chapter": READ, "search_books": READ, "story_bible": READ,
    "writing_advice": READ, "job_status": READ, "war_briefing": READ, "find_gaps": READ, "list_proposals": READ,
    "list_posts": READ, "app_status": READ, "get_settings": READ,
    "add_note": SAFE, "play_audiobook": SAFE, "stop_audiobook": SAFE, "reading_speed": SAFE, "cancel_job": SAFE, "propose_idea": SAFE,
    "open_tab": SAFE, "app_action": SAFE,
    "create_book": CHANGE, "write_chapters": CHANGE, "rewrite_chapter": CHANGE, "update_story_bible": CHANGE,
    "change_outline": CHANGE, "export_book": CHANGE, "decide_proposal": CHANGE,
    "change_setting": CHANGE, "undo_last_change": CHANGE,
    "publish_audiobook": OUTSIDE,
    "list_upgrades": READ,
    "voice_cast": READ, "cast_voices": CHANGE, "set_voice": CHANGE,
    "build_upgrade": SYSTEM, "install_upgrade": SYSTEM, "undo_upgrade": SYSTEM,
}

ASKING_NOTE = ("Not done yet: the app is asking the author yes or no. In one short sentence tell them what you "
               "want to do and ask yes or no. Do not call this tool again; the app hears their answer itself "
               "and tells you what happened.")

_YES = re.compile(r"\b(yes|yeah|yep|yup|yea|sure|ok|okay|koay|approved?|confirm(ed)?|go ahead|do it|"
                  r"sounds good|that'?s fine|please do|absolutely|of course)\b", re.I)
_NO = re.compile(r"\b(no|nope|nah|don'?t|do not|cancel|deny|denied|never ?mind|stop|wait|hold on|not yet)\b", re.I)


def heard_answer(text: str) -> Optional[bool]:
    """True for a clear yes, False for a clear no, None when it is unclear (or both)."""
    yes, no = bool(_YES.search(text or "")), bool(_NO.search(text or ""))
    return None if yes == no else yes


def level_of(tool: str) -> str:
    return TOOL_LEVELS.get(tool, SYSTEM)  # anything unlisted is treated as the most sensitive


@dataclass
class Request:
    id: int
    tool: str
    args: dict
    level: str
    summary: str
    source: str
    created_at: float
    execute: Callable[[], dict] = field(repr=False)
    armed: bool = True
    heard: str = ""


class PermissionGate:
    """One open question at a time; an answer applies only to the request it was given for."""

    def __init__(self, library=None, ttl_s: float = 120.0, clock: Callable[[], float] = time.time):
        self.library = library
        self.ttl_s = ttl_s
        self.clock = clock
        self.pending: Optional[Request] = None
        self.allowed: set[str] = set()  # Change-level tools allowed for the rest of this app session
        self._lock = threading.RLock()
        self._listeners: list[Callable[[str, dict], None]] = []
        self._next_id = 1

    # ---- listeners --------------------------------------------------------------------
    def subscribe(self, listener: Callable[[str, dict], None]) -> None:
        self._listeners.append(listener)

    def _notify(self, event: str, info: dict) -> None:
        for fn in list(self._listeners):
            try:
                fn(event, info)
            except Exception:
                log.exception("permission listener failed")

    # ---- asking -----------------------------------------------------------------------
    def decide(self, tool: str) -> str:
        """'free' (no question), 'auto' (allowed for this session) or 'ask'."""
        level = level_of(tool)
        if level not in NEEDS_YES:
            return "free"
        return "auto" if level == CHANGE and tool in self.allowed else "ask"

    def record_auto(self, tool: str, args: dict, summary: str, source: str) -> None:
        self._log_new(tool, level_of(tool), summary, args, source, "auto")

    def request(self, tool: str, args: dict, summary: str, execute: Callable[[], dict], source: str = "ai") -> Request:
        with self._lock:
            old = self.pending
            if old is not None:
                self._settle_log(old, "replaced", "", None)
            args = copy.deepcopy(args)
            level = level_of(tool)
            rid = self._log_new(tool, level, summary, args, source, "pending")
            # A spoken yes only counts once Jarvis has finished asking (after his turn ends).
            req = Request(rid, tool, args, level, summary, source, self.clock(), execute, armed=source != "voice")
            self.pending = req
        if old is not None:
            self._notify("closed", {"request": old, "status": "replaced"})
        self._notify("asking", {"request": req})
        return req

    def arm(self) -> None:
        with self._lock:
            if self.pending is not None and not self.pending.armed:
                self.pending.armed, self.pending.heard = True, ""

    # ---- answering --------------------------------------------------------------------
    def answer(self, yes: bool, request_id: Optional[int] = None, allow_session: bool = False,
               via: str = "button") -> Optional[dict]:
        """Settle the open request. Returns None when there is nothing (or a different request) to answer."""
        with self._lock:
            req = self.pending
            if req is None or (request_id is not None and request_id != req.id):
                return None
            self.pending = None
            if self.clock() - req.created_at > self.ttl_s:
                self._settle_log(req, "expired", via, None)
                outcome = {"request": req, "status": "expired", "approved": False, "result": None}
                self._notify("closed", outcome)
                return outcome
            if yes and allow_session and req.level == CHANGE:
                self.allowed.add(req.tool)
        result = None
        if yes:
            try:
                result = req.execute()
            except Exception as exc:  # the tool's own errors are already turned into dicts; this is a safety net
                log.exception("approved action %s failed", req.tool)
                result = {"error": f"{type(exc).__name__}: {exc}"}
        status = "approved" if yes else "denied"
        self._settle_log(req, status, via, result)
        outcome = {"request": req, "status": status, "approved": yes, "result": result}
        self._notify("closed", outcome)
        return outcome

    def hear(self, text: str) -> Optional[dict]:
        """The author's own words (typed, or the transcript of their voice). A clear yes/no settles the request."""
        with self._lock:
            req = self.pending
            if req is None or not req.armed or not (text or "").strip():
                return None
            req.heard += text if req.source == "voice" else " " + text
            decision = heard_answer(req.heard)
            if decision is None:
                return None
            rid = req.id
        return self.answer(decision, rid, via="voice" if req.source == "voice" else "text")

    def expire_stale(self) -> None:
        with self._lock:
            req = self.pending
            if req is None or self.clock() - req.created_at <= self.ttl_s:
                return
            self.pending = None
            self._settle_log(req, "expired", "", None)
        self._notify("closed", {"request": req, "status": "expired", "approved": False, "result": None})

    def forget_allowances(self) -> None:
        self.allowed.clear()

    # ---- log --------------------------------------------------------------------------
    def _log_new(self, tool, level, summary, args, source, status) -> int:
        if self.library is not None:
            return self.library.log_permission(tool, level, summary, args, source, status)
        with self._lock:
            rid, self._next_id = self._next_id, self._next_id + 1
            return rid

    def _settle_log(self, req: Request, status: str, via: str, result: Optional[dict]) -> None:
        if self.library is not None:
            self.library.settle_permission(req.id, status, via, json.dumps(result, ensure_ascii=False)[:2000]
                                           if result is not None else "")


def outcome_note(outcome: dict) -> str:
    """What to tell Gemini after the author answered."""
    req: Request = outcome["request"]
    if outcome["status"] == "expired":
        return f"(App note: the question '{req.summary}' timed out with no answer, so nothing was done.)"
    if not outcome["approved"]:
        return f"(App note: the author said NO to '{req.summary}'. It was not done.)"
    result = json.dumps(outcome["result"], ensure_ascii=False)[:1500]
    return f"(App note: the author said YES to '{req.summary}'. It has been done. Result: {result})"
