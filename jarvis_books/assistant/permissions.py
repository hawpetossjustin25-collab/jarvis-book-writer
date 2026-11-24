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
from types import MappingProxyType
from typing import Callable, Optional

log = logging.getLogger(__name__)

READ, SAFE, CHANGE, OUTSIDE, SYSTEM = "read", "safe", "change", "outside", "system"
LEVELS = (READ, SAFE, CHANGE, OUTSIDE, SYSTEM)
NEEDS_YES = (CHANGE, OUTSIDE, SYSTEM)
LEVEL_LABELS = {READ: "Read", SAFE: "Safe change", CHANGE: "Change", OUTSIDE: "Outside world", SYSTEM: "System"}

# Read-only, so no other module can relabel a tool at run time.
TOOL_LEVELS = MappingProxyType({
    "list_books": READ, "open_book": READ, "read_chapter": READ, "search_books": READ, "story_bible": READ,
    "writing_advice": READ, "job_status": READ, "war_briefing": READ, "find_gaps": READ, "list_proposals": READ,
    "list_posts": READ, "app_status": READ, "get_settings": READ, "recall_memories": READ,
    "remember": SAFE, "forget_memory": CHANGE,
    "add_note": SAFE, "play_audiobook": SAFE, "stop_audiobook": SAFE, "reading_speed": SAFE, "cancel_job": SAFE, "propose_idea": SAFE,
    "open_tab": SAFE, "app_action": SAFE,
    "create_book": CHANGE, "write_chapters": CHANGE, "set_book_length": CHANGE, "stop_writing": SAFE, "rewrite_chapter": CHANGE, "update_story_bible": CHANGE,
    "change_outline": CHANGE, "export_book": CHANGE, "decide_proposal": CHANGE,
    "change_setting": CHANGE, "undo_last_change": CHANGE,
    "publish_audiobook": OUTSIDE, "send_api": OUTSIDE,
    "list_upgrades": READ, "test_app": CHANGE,
    "brain_status": READ, "switch_brain": SAFE,
    "list_folder": READ, "read_file": READ, "computer_log": READ, "command_output": READ,
    "web_search": READ, "read_web_page": READ, "scrape_web_page": READ, "research_web": READ, "tax_lien_listings": READ, "online_tax_sales": READ, "best_tax_deals": READ, "explore_website": READ, "call_api": READ, "list_api_keys": READ, "hf_search": READ, "hf_model_info": READ,
    "github_search": READ, "github_read": READ,
    "write_file": SYSTEM, "edit_file": SYSTEM, "make_folder": SYSTEM, "move_path": SYSTEM,
    "delete_path": SYSTEM, "run_command": SYSTEM, "download_file": SYSTEM, "github_download": SYSTEM,
    "voice_cast": READ, "cast_voices": CHANGE, "set_voice": CHANGE, "swap_voices": CHANGE, "hear_voice": SAFE,
    "show_planner": READ, "list_reminders": READ,
    "add_to_planner": SAFE, "update_planner_item": SAFE, "set_reminder": SAFE, "change_reminder": SAFE,
    "cancel_reminder": SAFE, "snooze_reminder": SAFE, "stop_alarm": SAFE,
    "delete_planner_item": CHANGE, "plan_writing_schedule": CHANGE,
    "build_upgrade": SYSTEM, "install_upgrade": SYSTEM, "undo_upgrade": SYSTEM,
    "list_programs": READ, "system_info": READ, "read_clipboard": READ, "look_at_screen": READ,
    "open_item": SAFE, "copy_to_clipboard": SAFE, "media_control": SAFE,
    "research_status": READ, "research_notes": READ, "research_topic": SAFE, "change_research": SAFE,
    "research_now": SAFE, "researcher_switch": SAFE, "drop_research": CHANGE,
    # the Atlanta game only changes the game, so moves need no yes; starting over does
    "game_status": READ, "game_market": READ, "game_inspect": READ, "game_renovation_quotes": READ,
    "game_tax_sale": READ, "game_tax_deeds": READ, "game_lesson": READ,
    "game_offer": SAFE, "game_renovate": SAFE, "game_list_property": SAFE, "game_rent_out": SAFE,
    "game_go_to_tax_sale": SAFE, "game_start_auction": SAFE, "game_bid": SAFE, "game_pass": SAFE,
    "game_foreclose": SAFE, "game_start_company": SAFE, "game_staff": SAFE, "game_close_company": SAFE,
    "game_calls": SAFE, "game_answer_call": SAFE, "game_advance": SAFE, "game_dev": SAFE,
    "game_new": CHANGE, "game_goal": READ, "game_transit": READ, "game_car_lot": READ,
    "game_take_loan": SAFE, "game_repay_loan": SAFE, "game_buy_transit": SAFE, "game_upgrade_transit": SAFE,
    "game_stock_cars": SAFE, "game_trading": READ, "game_open_trading": SAFE, "game_trading_funds": SAFE,
    "game_trading_agents": SAFE,
    "close_program": CHANGE, "type_text": CHANGE, "press_keys": CHANGE, "click_screen": CHANGE,
})
# Replacing the app's own code always waits for a yes, even in Full control.
ALWAYS_ASK = ("install_upgrade", "undo_upgrade")
FULL_CONTROL_SETTING = "full_control"

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
    """The built-in level, else the one a self-upgrade gave its new tool; anything unlisted (or with an unknown
    level) is treated as the most sensitive."""
    if tool in TOOL_LEVELS:
        return TOOL_LEVELS[tool]
    from jarvis_books.assistant.upgrade_levels import ADDED_TOOL_LEVELS
    level = ADDED_TOOL_LEVELS.get(tool, SYSTEM)
    return level if level in LEVELS else SYSTEM


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
        self.full_control: Callable[[], bool] = lambda: False  # set by the Studio from Settings
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
        if tool not in ALWAYS_ASK and self._full_control():
            return "auto"
        return "auto" if level == CHANGE and tool in self.allowed else "ask"

    def _full_control(self) -> bool:
        try:
            return bool(self.full_control())
        except Exception:
            log.exception("full control check failed")
            return False

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
