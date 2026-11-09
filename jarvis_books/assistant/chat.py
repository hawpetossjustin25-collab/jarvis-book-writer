"""Text chat with Jarvis: Gemini function calling over the ToolBox, with persistent history."""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from jarvis_books.assistant.permissions import outcome_note
from jarvis_books.assistant.tools import ToolBox
from jarvis_books.llm.models import LLMRequest, PromptSection
from jarvis_books.storage.library import Library
from jarvis_books.writer import prompts

log = logging.getLogger(__name__)
MAX_TOOL_ROUNDS = 6


class Assistant:
    def __init__(self, engine, library: Library, tools: ToolBox, channel: str = "chat", guide: str = "",
                 per_book: bool = False):
        self.engine = engine
        self.library = library
        self.tools = tools
        self.channel = channel
        self.guide = guide  # extra instructions for this room (e.g. the War Room)
        self.per_book = per_book  # keep a separate conversation per book

    def _history(self) -> list[dict]:
        config = self.engine.get_runtime_configuration()
        book_id = self.tools.current_book_id if self.per_book else None
        msgs = self.library.recent_messages(self.channel, limit=config.history_turns, book_id=book_id)
        return [{"role": "user" if m.role == "user" else "model", "parts": [{"text": m.text}]} for m in msgs]

    def _situation(self) -> str:
        books = self.library.list_books()
        lines = [f"The user has {len(books)} book(s): " + ", ".join(f"'{b.title}' ({b.status})" for b in books[:20])]
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
        history = self._history()
        self.library.add_message(self.channel, "user", text, self.tools.current_book_id)
        tools = self.tools.select(text)
        answered = self.tools.gate.hear(text)  # "yes"/"no" to an open question is settled by the app itself
        model_text = text + ("\n\n" + outcome_note(answered) if answered else "")
        contents = history + [{"role": "user", "parts": [{"text": model_text}]}]
        system = prompts.ASSISTANT + ("\n\n" + self.guide if self.guide else "") + "\n\n" + self._situation()
        reply = ""
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.engine.generate(LLMRequest(role="chat", system=system, history=contents, tools=tools,
                                                   temperature=0.7, purpose="chat"))
            if not resp.function_calls:
                reply = resp.text.strip()
                break
            contents.append(resp.content or {"role": "model", "parts": [{"functionCall": c} for c in resp.function_calls]})
            parts = []
            for call in resp.function_calls:
                result = self.tools.call(call.get("name", ""), call.get("args") or {}, source="chat")
                if on_tool:
                    on_tool(call.get("name", ""), call.get("args") or {}, result)
                parts.append({"functionResponse": {"name": call.get("name", ""), "response": _clip(result)}})
            contents.append({"role": "user", "parts": parts})
        else:
            reply = "I ran out of steps working on that. Could you say it a different way?"
        self.library.add_message(self.channel, "assistant", reply or "(done)", self.tools.current_book_id)
        return reply


def _clip(result: dict, limit: int = 20000) -> dict:
    raw = json.dumps(result, ensure_ascii=False)
    if len(raw) <= limit:
        return result
    return {"truncated": True, "content": raw[:limit]}
