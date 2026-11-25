"""Text chat with Jarvis: Gemini function calling over the ToolBox, with persistent history."""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as ToolTimeout
from datetime import datetime
from typing import Callable, Optional

from jarvis_books.assistant.game_tools import asks_for_game_move
from jarvis_books.assistant.permissions import READ, level_of, outcome_note
from jarvis_books.assistant.tools import ToolBox
from jarvis_books.llm.history import steady_count
from jarvis_books.llm.models import LLMRequest, PromptSection
from jarvis_books.storage.library import Library
from jarvis_books.writer import prompts

log = logging.getLogger(__name__)
MAX_TOOL_ROUNDS = 10  # research can take a few searches and pages
SEARCH_TOOLS = ("research_web", "web_search")
MAX_SEARCHES = 3  # per question: more searches mostly repeat the same pages and make the author wait
NO_TOOL_NOTE = ("(Check from the app: you answered without calling any tool, so nothing in the game changed. If "
                "the author asked for a game move, make it now with the game tools and report only what they "
                "return. If they only asked a question, give your answer again.)")
READ_TIMEOUT_S = 90  # a lookup that takes longer is reported as slow instead of holding up the answer
SLOW_READS = {"research_web": 240, "explore_website": 240, "best_tax_deals": 300, "online_tax_sales": 240,
              "tax_lien_listings": 240, "look_at_screen": 150}
MAX_PARALLEL = 4
FAST_NOTE = ("(Note from the app: Jarvis's quick tool brain already did this for the request above: {name} with "
             "{args}. It returned: {result}\nTell the author what happened using only that result. Don't call "
             "{name} again for this request; call another tool only if the request still needs one.)")
SEARCH_LIMIT_NOTE = ("search limit reached for this question: answer now from what you already found, "
                     "and say plainly anything you couldn't confirm")


CONTEXT_NOTE = "For you, not from the author: what you know right now"


def now_line() -> str:
    """Jarvis needs today's date and the time for reminders and the planner ("in 20 minutes", "friday")."""
    return "Right now it is " + datetime.now().strftime("%A %B %d %Y, %I:%M %p") + " (the author's local time)."


class Assistant:
    last_used = 0.0  # when the author last spoke to him; background work waits for quiet

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
        self.researcher = None  # the background researcher, whose notes Jarvis reads (set by the Studio)
        self.fast = None  # the quick tool brain (FastRouter), set by the Studio

    def _history(self) -> list[dict]:
        config = self.engine.get_runtime_configuration()
        book_id = self.tools.current_book_id if self.per_book else None
        limit = config.history_turns
        total = self.library.count_messages(self.history_channels, book_id=book_id)
        # The window's oldest turn only moves every few messages, so a local brain can reuse the prompt it read.
        msgs = self.library.recent_messages(self.history_channels, book_id=book_id,
                                            limit=steady_count(total, limit, max(1, limit // 2)))
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

    def send(self, text: str, on_tool: Optional[Callable[[str, dict, dict], None]] = None,
             on_text: Optional[Callable[[str], None]] = None) -> str:
        """Send one user message; runs tool calls until Gemini answers in text. Returns the reply. on_text gets the
        reply so far while it is being written (it may start over when another brain takes over)."""
        self.last_used = time.time()
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
        done = self._fast_call(text, tools, answered, on_tool)
        if done is not None:
            model_text += "\n\n" + FAST_NOTE.format(name=done[0]["name"], args=json.dumps(done[0]["args"]),
                                                     result=json.dumps(_clip(done[1], 6000), ensure_ascii=False))
        # The system prompt stays the same from message to message and what changes (the time, open book, memories
        # and research for this message) goes after the author's words: brains reuse a prompt's unchanged start.
        system = prompts.ASSISTANT + ("\n\n" + self.guide if self.guide else "")
        context = [self._situation()]
        remembered = self.memory.prompt(text, self.tools.current_book_id) if self.memory is not None else ""
        if remembered:
            context.append(remembered)
        researched = self.researcher.prompt(text) if self.researcher is not None else ""
        if researched:
            context.append(researched)
        model_text += "\n\n(" + CONTEXT_NOTE + ")\n" + "\n\n".join(context)
        contents = history + [{"role": "user", "parts": [{"text": model_text}]}]
        reply = ""
        searches = 0
        called, checked = done is not None, False
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.engine.generate(LLMRequest(role="chat", system=system, history=contents, tools=tools,
                                                   local_tools=local_tools, on_text=on_text,
                                                   temperature=0.7, purpose="chat"))
            if not resp.function_calls:
                reply = resp.text.strip()
                if not called and not checked and getattr(self.tools, "game", None) is not None \
                        and asks_for_game_move(text):
                    checked = True  # a game move with no tool call didn't happen: ask once more
                    log.info("game move answered without a tool call by %s; asking again", resp.model)
                    contents.append({"role": "model", "parts": [{"text": reply or "(no answer)"}]})
                    contents.append({"role": "user", "parts": [{"text": NO_TOOL_NOTE}]})
                    continue
                break
            called = True
            contents.append(resp.content or {"role": "model", "parts": [{"functionCall": c} for c in resp.function_calls]})
            parts = []
            allowed = []
            for call in resp.function_calls:
                if call.get("name", "") in SEARCH_TOOLS:
                    searches += 1
                allowed.append(not (call.get("name", "") in SEARCH_TOOLS and searches > MAX_SEARCHES))
            again = [done is not None and c.get("name") == done[0]["name"] and (c.get("args") or {}) == done[0]["args"]
                     for c in resp.function_calls]
            fresh = [c for c, a in zip(resp.function_calls, again) if not a]
            ran = iter(self._run_calls(fresh, [ok for ok, a in zip(allowed, again) if not a]))
            results = [done[1] if a else next(ran) for a in again]  # the same move is never made twice
            for call, result in zip(resp.function_calls, results):
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

    def _fast_call(self, text: str, tools: list[dict], answered, on_tool) -> Optional[tuple[dict, dict]]:
        """Short, clear commands are done by the quick tool brain at once. Returns (call, result) or None."""
        if self.fast is None or answered or getattr(self.tools.gate, "pending", None) is not None:
            return None
        try:
            call = self.fast.route(text, tools, level_of)
        except Exception:
            log.exception("quick tool brain failed; the main brain will pick the tool")
            return None
        if call is None:
            return None
        log.info("quick tool brain: %s (%.2f)", call["name"], call["confidence"])
        call = {"name": call["name"], "args": call["args"]}
        result = self._run_calls([call], [True])[0]
        if on_tool:
            on_tool(call["name"], call["args"], result)
        return call, result

    def _run_calls(self, calls: list[dict], allowed: list[bool]) -> list[dict]:
        """Lookups that only read run side by side, each with a time limit; anything that changes something
        runs one at a time, in the order Jarvis asked, with no time limit (stopping it halfway would mislead)."""
        results: list = [None] * len(calls)
        reads = []
        for i, (call, ok) in enumerate(zip(calls, allowed)):
            if not ok:
                results[i] = {"error": SEARCH_LIMIT_NOTE}
            elif level_of(call.get("name", "")) == READ:
                reads.append(i)
        if reads:
            pool = ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(reads)), thread_name_prefix="jarvis-read")
            try:
                futures = {i: pool.submit(self.tools.call, calls[i].get("name", ""), calls[i].get("args") or {},
                                          source="chat") for i in reads}
                for i, future in futures.items():
                    name = calls[i].get("name", "")
                    limit = SLOW_READS.get(name, READ_TIMEOUT_S)
                    try:
                        results[i] = future.result(timeout=limit)
                    except ToolTimeout:
                        log.warning("tool %s took longer than %s s", name, limit)
                        results[i] = {"error": f"{name} took longer than {limit} seconds, so it was left out. Tell "
                                               "the author it was too slow and offer to try again."}
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        for i, call in enumerate(calls):
            if results[i] is None:
                results[i] = self.tools.call(call.get("name", ""), call.get("args") or {}, source="chat")
        return results


def _clip(result: dict, limit: int = 20000) -> dict:
    raw = json.dumps(result, ensure_ascii=False)
    if len(raw) <= limit:
        return result
    return {"truncated": True, "content": raw[:limit]}
