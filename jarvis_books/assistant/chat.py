"""Text chat with Jarvis: Gemini function calling over the ToolBox, with persistent history."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Callable, Optional

from jarvis_books.assistant.permissions import outcome_note
from jarvis_books.assistant.tools import ToolBox
from jarvis_books.llm.models import LLMRequest, PromptSection
from jarvis_books.storage.library import Library
from jarvis_books.writer import prompts

log = logging.getLogger(__name__)
MAX_TOOL_ROUNDS = 10  # research can take a few searches and pages
SEARCH_TOOLS = ("research_web", "web_search")
MAX_SEARCHES = 3  # per question: more searches mostly repeat the same pages and make the author wait
SEARCH_LIMIT_NOTE = ("search limit reached for this question: answer now from what you already found, "
                     "and say plainly anything you couldn't confirm")


def now_line() -> str:
    """Jarvis needs today's date and the time for reminders and the planner ("in 20 minutes", "friday")."""
    return "Right now it is " + datetime.now().strftime("%A %B %d %Y, %I:%M %p") + " (the author's local time)."


class Assistant:
    def __init__(self, engine, library: Library, tools: ToolBox, channel: str = "chat", guide: str = "",
                 per_book: bool = False, history_channels: tuple = ()):
        self.engine = engine
        self.library = library
        self.tools = tools
        self.channel = channel
        self.guide = guide  # extra instructions for this room (e.g. the War Room)
        self.per_book = per_book  # keep a separate conversation per book
        self.history_channels = tuple(history_channels) or (channel,)  # e.g. chat and voice are one conversation
        self.memory = None  # Jarvis's long-term memory (set by the Studio)

    def _history(self) -> list[dict]:
        config = self.engine.get_runtime_configuration()
        book_id = self.tools.current_book_id if self.per_book else None
        msgs = self.library.recent_messages(self.history_channels, limit=config.history_turns, book_id=book_id)
        return [{"role": "user" if m.role == "user" else "model", "parts": [{"text": m.text}]} for m in msgs]

    def _situation(self) -> str:
        books = self.library.list_books()
        lines = [now_line(), f"The user has {len(books)} book(s): " + ", ".join(f"'{b.title}' ({b.status})" for b in books[:20])]
        if self.tools.current_book_id:
            try:
                b = self.library.get_book(self.tools.current_book_id)
                lines.append(f"Currently open book: '{b.title}' - {b.logline}")
            except Exception:
                self.tools.current_book_id = None
        active = self.tools.jobs.active()
        if active:
            lines.append("Running jobs: " + "; ".join(f"{j.title} {round(j.progress * 100)}%" for j in active))
        return "\n".join(lines)

    def send(self, text: str, on_tool: Optional[Callable[[str, dict, dict], None]] = None) -> str:
        """Send one user message; runs tool calls until Gemini answers in text. Returns the reply."""
        key_book = self.tools.current_book_id if self.per_book else None
        if self.memory is not None:  # a conversation that went quiet is learned from before this one goes on
            self.memory.schedule(self.channel, key_book)
        history = self._history()
        self.library.add_message(self.channel, "user", text, self.tools.current_book_id)
        tools = self.tools.declarations()
        last_asked = next((part.get("text", "") for turn in reversed(history) if turn.get("role") == "user"
                           for part in turn.get("parts", []) if isinstance(part, dict)), "")
        local_tools = self.tools.select(text + " " + last_asked)
        answered = self.tools.gate.hear(text)  # "yes"/"no" to an open question is settled by the app itself
        model_text = text + ("\n\n" + outcome_note(answered) if answered else "")
        contents = history + [{"role": "user", "parts": [{"text": model_text}]}]
        system = prompts.ASSISTANT + ("\n\n" + self.guide if self.guide else "") + "\n\n" + self._situation()
        remembered = self.memory.prompt(text, self.tools.current_book_id) if self.memory is not None else ""
        if remembered:
            system += "\n\n" + remembered
        reply = ""
        searches = 0
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.engine.generate(LLMRequest(role="chat", system=system, history=contents, tools=tools,
                                                   local_tools=local_tools,
                                                   temperature=0.7, purpose="chat"))
            if not resp.function_calls:
                reply = resp.text.strip()
                break
            contents.append(resp.content or {"role": "model", "parts": [{"functionCall": c} for c in resp.function_calls]})
            parts = []
            for call in resp.function_calls:
                if call.get("name", "") in SEARCH_TOOLS:
                    searches += 1
                if call.get("name", "") in SEARCH_TOOLS and searches > MAX_SEARCHES:
                    result = {"error": SEARCH_LIMIT_NOTE}
                else:
                    result = self.tools.call(call.get("name", ""), call.get("args") or {}, source="chat")
                if on_tool:
                    on_tool(call.get("name", ""), call.get("args") or {}, result)
                parts.append({"functionResponse": {"name": call.get("name", ""), "response": _clip(result)}})
            contents.append({"role": "user", "parts": parts})
            if searches >= MAX_SEARCHES:
                tools = [d for d in tools if d["name"] not in SEARCH_TOOLS]
                local_tools = [d for d in local_tools if d["name"] not in SEARCH_TOOLS]
        else:
            reply = "I ran out of steps working on that. Could you say it a different way?"
        self.library.add_message(self.channel, "assistant", reply or "(done)", self.tools.current_book_id)
        if self.memory is not None:
            self.memory.schedule(self.channel, key_book)
        return reply


def _clip(result: dict, limit: int = 20000) -> dict:
    raw = json.dumps(result, ensure_ascii=False)
    if len(raw) <= limit:
        return result
    return {"truncated": True, "content": raw[:limit]}
