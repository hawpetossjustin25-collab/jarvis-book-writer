"""Jarvis's tools for his background researcher (mixed into the ToolBox)."""
from __future__ import annotations

from typing import Optional

from jarvis_books.errors import NotFound
from jarvis_books.research import KINDS, STATUSES

S = {"type": "STRING"}
I = {"type": "INTEGER"}
B = {"type": "BOOLEAN"}
TOPIC = {"type": "STRING", "description": "the topic's name (partial names are fine) or its id"}


def _decl(name: str, description: str, props: Optional[dict] = None, required: Optional[list[str]] = None) -> dict:
    params: dict = {"type": "OBJECT", "properties": props or {}}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


RESEARCH_DECLARATIONS = [
    _decl("research_status", "Your background researcher: whether it's on, how often it researches, which model "
          "it thinks with, every topic and project it follows (status, interest, notes) and what it did last."),
    _decl("research_notes", "What the researcher found, newest first, each with its link: for one topic, or "
          "across all topics matching query.", {"topic": TOPIC, "query": S, "limit": I}),
    _decl("research_topic", "Give the researcher a topic to keep researching around the clock, or a project the "
          "author wants to build (kind 'project': it researches how to build it). focus: what to dig into.",
          {"name": S, "focus": S, "kind": {"type": "STRING", "enum": list(KINDS)}, "why": S}, ["name"]),
    _decl("change_research", "Change a research topic: status active (research it), paused, done or watching; a "
          "new focus; or kind topic/project.",
          {"topic": TOPIC, "status": {"type": "STRING", "enum": list(STATUSES)}, "focus": S,
           "kind": {"type": "STRING", "enum": list(KINDS)}}, ["topic"]),
    _decl("drop_research", "Delete a research topic and everything found for it.", {"topic": TOPIC}, ["topic"]),
    _decl("research_now", "Run a research round right now in the background (on one topic, or the one most "
          "worth it), instead of waiting for the next scheduled round.", {"topic": TOPIC}),
    _decl("researcher_switch", "Turn the background researcher on or off, set the minutes between rounds, or "
          "pick its brain: 'local' (free model on this computer, no daily limit), 'groq' (free online, "
          "about 1,000 requests a day, separate from Jarvis's) or 'gemini' (uses Gemini's daily requests).",
          {"on": B, "minutes": I, "brain": {"type": "STRING", "enum": ["local", "groq", "gemini"]}}),
    _decl("brain_lab_status", "RSI mode (the Brain Lab, where Jarvis keeps improving his own brain on this "
          "computer without a graphics card): this computer's measured specs, the targets set from them, the best "
          "setup so far and its scores (tool calls, coding tests, long project, reply speed, RAM), which targets "
          "aren't met yet, the last rounds and the research papers it read."),
    _decl("brain_lab_switch", "Turn RSI mode (the Brain Lab) on or off. While on it runs rounds whenever Jarvis "
          "is idle, nonstop until every target is met and then every few hours.", {"on": B}, ["on"]),
    _decl("brain_lab_round", "Start a Brain Lab round right now (when Jarvis is idle) instead of waiting."),
]
RESEARCH_TOOLS = tuple(d["name"] for d in RESEARCH_DECLARATIONS)
RESEARCH_WORDS = ("research", "look into", "dig into", "keep digging", "find out more", "notebook", "notes",
                  "interested in", "learn about", "keep an eye", "follow up", "project", "topic", "build",
                  "what did you find", "what have you found", "rsi", "brain lab", "self-improv", "improve your",
                  "improve yourself", "your brain", "specs")


class ResearchTools:
    """Mixed into ToolBox; `self.researcher` and `self.research_clock` are set by the Studio."""

    researcher = None
    research_clock = None

    def _res(self):
        if self.researcher is None:
            raise NotFound("the researcher is not available here")
        return self.researcher

    def _poke(self) -> None:
        if self.research_clock is not None:
            self.research_clock.poke()

    lab = None
    lab_clock = None

    def _lab(self):
        if self.lab is None:
            raise NotFound("the Brain Lab is not available here")
        return self.lab

    def _t_brain_lab_status(self) -> dict:
        return self._lab().status()

    def _t_brain_lab_switch(self, on: bool) -> dict:
        store = self.settings_store
        if store is None:
            raise NotFound("settings are not available here")
        self._lab()
        store.set_setting("rsi", "1" if on else "0")
        if self.lab_clock is not None:
            self.lab_clock.poke()
        return {"on": bool(on), "note": "rounds run while Jarvis is idle" if on else "stopped; the best setup "
                "found so far stays in use"}

    def _t_brain_lab_round(self) -> dict:
        lab = self._lab()
        if lab.running:
            return {"started": False, "note": f"a round is already running: {lab.step}"}
        if not lab.enabled():
            return {"started": False, "note": "RSI mode is off; turn it on first"}
        if self.lab_clock is not None:
            self.lab_clock.poke()
        return {"started": True, "note": "it starts once Jarvis is idle; brain_lab_status shows progress"}

    def _t_research_status(self) -> dict:
        return self._res().status()

    def _t_research_notes(self, topic: str = "", query: str = "", limit: int = 10) -> dict:
        rows = self._res().findings(topic or None, query, min(max(1, limit or 10), 40))
        out: dict = {"notes": [{k: r[k] for k in ("topic", "title", "summary", "url")} for r in rows]}
        if topic:
            path = self._res().notes_file(topic)
            if path:
                out["notebook_file"] = str(path)
        if not rows:
            out["note"] = "nothing found yet" + (" for that" if topic or query else "; give it a topic")
        return out

    def _t_research_topic(self, name: str, focus: str = "", kind: str = "topic", why: str = "") -> dict:
        t = self._res().add_topic(name, focus, kind or "topic", why, source="Jarvis")
        if not self._res().enabled():
            t["note"] = "the researcher is switched off; researcher_switch on=true starts it"
        self._poke()
        return t

    def _t_change_research(self, topic: str, status: Optional[str] = None, focus: Optional[str] = None,
                           kind: Optional[str] = None) -> dict:
        t = self._res().update(topic, status, focus, kind)
        self._poke()
        return t

    def _t_drop_research(self, topic: str) -> dict:
        t = self._res().delete(topic)
        return {"dropped": t["name"], "notes_deleted": t["findings"]}

    def _t_research_now(self, topic: str = "") -> dict:
        res = self._res()
        name = res.topic(topic)["name"] if topic else "the topic most worth it"
        job = self.jobs.submit("research", f"Researching {name}", lambda progress, cancel: res.step(topic or None))
        return {"started": True, "job_id": job.id, "note": "job_status shows when it's done; research_notes has "
                "what it found"}

    def _t_researcher_switch(self, on: Optional[bool] = None, minutes: Optional[int] = None,
                             brain: Optional[str] = None) -> dict:
        store = self.settings_store
        if store is None:
            raise NotFound("settings are not available here")
        if on is not None:
            store.set_setting("researcher", "1" if on else "0")
        if minutes is not None:
            store.set_setting("research_pace", str(max(1, min(int(minutes), 24 * 60))))
        if brain in ("local", "groq", "gemini"):
            store.set_setting("research_brain", brain)
        self._poke()
        return self._res().status()
