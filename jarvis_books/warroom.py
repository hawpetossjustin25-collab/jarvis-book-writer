"""The War Room: the author and Jarvis go over a book, fill gaps, and decide what becomes canon.

Anything Jarvis invents is a proposal until the author accepts it. Accepting applies it to the story
bible, outline or notes and records what it replaced, so every decision can be undone.
"""
from __future__ import annotations

import logging
from typing import Optional

from jarvis_books.errors import ValidationError
from jarvis_books.llm.models import LLMRequest, PromptSection
from jarvis_books.storage.library import Library
from jarvis_books.storage.models import Proposal

log = logging.getLogger(__name__)

STR = {"type": "STRING"}
LIST = {"type": "ARRAY", "items": STR}
GAPS_SCHEMA = {"type": "OBJECT", "properties": {"gaps": {"type": "ARRAY", "items": {
    "type": "OBJECT", "properties": {"question": STR, "why_it_matters": STR, "options": LIST},
    "required": ["question", "why_it_matters", "options"]}}}, "required": ["gaps"]}
RECAP_SCHEMA = {"type": "OBJECT", "properties": {"summary": STR, "decisions": LIST, "open_questions": LIST,
                                                 "next_steps": LIST},
                "required": ["summary", "decisions", "open_questions", "next_steps"]}

ANALYST = ("You are Jarvis in the War Room, a story development partner. Ground every statement in the "
           "material given. Label each detail as WRITTEN (only text in written chapters, cite the chapter), "
           "PLANNED (premise, synopsis, outline or story bible: decided but not written yet) or IDEA (your own "
           "suggestion, not canon).")

WAR_ROOM_GUIDE = (
    "You are in the War Room with the author, working through the book together like a writers' room. You can "
    "explain any detail of any book, including parts that are not written yet. Always say where a detail comes "
    "from: WRITTEN (check with read_chapter or search_books and cite the chapter), PLANNED (outline or story "
    "bible) or your IDEA. Never change canon directly: pitch every change with propose_idea, and call "
    "decide_proposal only when the author clearly accepts, rejects or undoes one. Use war_briefing when asked to "
    "be filled in and find_gaps to surface what is undecided. Offer concrete options, ask one question at a "
    "time, and keep the momentum going.")


class WarRoom:
    def __init__(self, library: Library, engine, context):
        self.library = library
        self.engine = engine
        self.context = context
        self.memory = None  # Jarvis learns from what the author accepts, edits and rejects

    def _feedback(self, kind: str, detail: str, book_id: str) -> None:
        if self.memory is not None:
            try:
                self.memory.note(kind, detail, book_id)
            except Exception:
                log.exception("could not note War Room feedback")

    # ---- material -------------------------------------------------------------------
    def _sections(self, book_id: str) -> list[PromptSection]:
        last = self.library.list_chapters(book_id)
        before = (max((c.number for c in last), default=0)) + 1
        return [PromptSection("Book", self.context.book_brief(book_id), 100, True),
                PromptSection("Story bible", self.context.bible_text(book_id), 80),
                PromptSection("Outline", self.context.outline_text(book_id), 90),
                PromptSection("Story so far (written chapters)", self.context.story_so_far(book_id, before), 70,
                              trim_from="start")]

    def facts(self, book_id: str) -> dict:
        """Deterministic state of the book: what is written, what is only planned, what is pending."""
        chapters = self.library.list_chapters(book_id)
        threads = self.library.list_bible(book_id, "thread")
        sessions = self.library.war_sessions(book_id, 1)
        last = sessions[0] if sessions and sessions[0].ended_at else None
        return {
            "written": [{"chapter": c.number, "title": c.title, "words": c.word_count, "status": c.status}
                        for c in chapters if c.word_count > 0],
            "planned": [{"chapter": c.number, "title": c.title, "outline": c.outline}
                        for c in chapters if c.word_count == 0],
            "plot_threads": [{"name": t.name, "summary": t.summary} for t in threads],
            "pending_proposals": [{"id": p.id, "title": p.title, "kind": p.kind}
                                  for p in self.library.list_proposals(book_id, "proposed")],
            "last_session": {"summary": last.summary, "decisions": last.decisions,
                             "open_questions": last.open_questions, "next_steps": last.next_steps} if last else None,
        }

    # ---- talking it through -------------------------------------------------------------
    def briefing(self, book_id: str, focus: str = "") -> dict:
        facts = self.facts(book_id)
        ask = ("Give the author a spoken-style briefing on this book: what it is about, where it stands, what "
               "is written versus only planned, open plot threads, and what is still undecided.")
        written = [str(c["chapter"]) for c in facts["written"]]
        ask += (" Chapters actually written: " + ", ".join(written) + "." if written else
                " No chapters are written yet, so nothing is WRITTEN; everything decided so far is PLANNED.")
        if focus:
            ask += f" Focus on: {focus}."
        if facts["last_session"]:
            ask += " Start by reminding them what was decided last time."
        resp = self.engine.generate(LLMRequest(role="chat", system=ANALYST, instruction=ask,
                                               sections=self._sections(book_id), temperature=0.4,
                                               purpose="war room briefing"))
        return {"briefing": resp.text.strip(), **facts}

    def find_gaps(self, book_id: str, focus: str = "") -> list[dict]:
        ask = ("List what this story has not decided yet or leaves unexplained: missing motives, unexplained "
               "events, characters without an arc, unresolved threads, the ending. For each give 2-3 concrete "
               "options that fit the established canon.")
        if focus:
            ask += f" Focus on: {focus}."
        resp = self.engine.generate(LLMRequest(role="editor", system=ANALYST, instruction=ask,
                                               sections=self._sections(book_id), json_schema=GAPS_SCHEMA,
                                               temperature=0.6, purpose="war room gaps"))
        return list((resp.data or {}).get("gaps", []))

    # ---- proposals ------------------------------------------------------------------------
    def propose(self, book_id: str, kind: str, title: str, content: str, target: str = "",
                rationale: str = "", session_id: Optional[int] = None) -> Proposal:
        if kind == "bible":
            _split_target(target)
        if kind == "outline":
            _chapter_target(target)
        return self.library.add_proposal(book_id, kind, title, content, target, rationale, session_id)

    def accept(self, proposal_id: int, content: Optional[str] = None) -> Proposal:
        prop = self.library.get_proposal(proposal_id)
        if prop.status != "proposed":
            raise ValidationError(f"proposal {proposal_id} is already {prop.status}")
        text = (content or prop.content).strip()
        if prop.kind == "bible":
            kind, name = _split_target(prop.target)
            old = next((e for e in self.library.list_bible(prop.book_id, kind) if e.name.lower() == name.lower()), None)
            entry = self.library.upsert_bible(prop.book_id, kind, old.name if old else name, text)
            applied = {"kind": kind, "name": entry.name, "previous": old.summary if old else None}
        elif prop.kind == "outline":
            number = _chapter_target(prop.target)
            if number == 0:
                ch = self.library.add_chapter(prop.book_id, title=prop.title, outline=text)
                applied = {"added_chapter_id": ch.id}
            else:
                ch = self.library.get_chapter(prop.book_id, number)
                applied = {"chapter_id": ch.id, "previous": ch.outline}
                self.library.update_chapter(ch.id, outline=text)
        else:
            note = self.library.add_note(prop.book_id, f"{prop.title}: {text}", source="war-room")
            applied = {"note_id": note.id}
        applied["content"] = text
        accepted = self.library.set_proposal_status(proposal_id, "accepted", applied)
        if content is not None and content.strip() != prop.content.strip():
            self._feedback("idea_edited", f"The author accepted Jarvis's {prop.kind} idea '{prop.title}' after "
                           f"editing it. Jarvis pitched: {prop.content[:400]} | The author's version: {text[:400]}",
                           prop.book_id)
        else:
            self._feedback("idea_accepted", f"The author accepted Jarvis's {prop.kind} idea '{prop.title}': "
                           f"{text[:400]}", prop.book_id)
        return accepted

    def reject(self, proposal_id: int) -> Proposal:
        prop = self.library.get_proposal(proposal_id)
        if prop.status != "proposed":
            raise ValidationError(f"proposal {proposal_id} is already {prop.status}")
        rejected = self.library.set_proposal_status(proposal_id, "rejected")
        self._feedback("idea_rejected", f"The author rejected Jarvis's {prop.kind} idea '{prop.title}': "
                       f"{prop.content[:400]}" + (f" (Jarvis's reason: {prop.rationale[:200]})" if prop.rationale else ""),
                       prop.book_id)
        return rejected

    def undo(self, proposal_id: int) -> Proposal:
        """Roll back an accepted proposal to exactly what was there before."""
        prop = self.library.get_proposal(proposal_id)
        if prop.status != "accepted":
            raise ValidationError(f"only accepted proposals can be undone (this one is {prop.status})")
        a = prop.applied
        if prop.kind == "bible":
            entry = next((e for e in self.library.list_bible(prop.book_id, a["kind"]) if e.name == a["name"]), None)
            if a.get("previous") is None and entry:
                self.library.delete_bible_entry(entry.id)
            elif entry:
                self.library.upsert_bible(prop.book_id, a["kind"], a["name"], a["previous"])
        elif prop.kind == "outline":
            if a.get("added_chapter_id"):
                ch = self.library.get_chapter_by_id(a["added_chapter_id"])
                if ch.word_count:
                    raise ValidationError("that chapter has been written since; restore it from its history instead")
                self.library.delete_chapter(ch.id)
            else:
                self.library.update_chapter(a["chapter_id"], outline=a.get("previous") or "")
        # Accepted notes stay in the notebook; the decision is just marked as undone.
        return self.library.set_proposal_status(proposal_id, "undone", a)

    # ---- sessions ---------------------------------------------------------------------------
    def recap(self, session_id: int, transcript: str):
        session = self.library.get_war_session(session_id)
        decided = [p.title for p in self.library.list_proposals(session.book_id)
                   if p.session_id == session_id and p.status == "accepted"]
        ask = ("Write the minutes of this War Room session: a short summary, the decisions made, open "
               "questions, and next steps.\n\nDecisions accepted: " + ("; ".join(decided) or "none") +
               "\n\nConversation:\n" + transcript[-20000:])
        resp = self.engine.generate(LLMRequest(role="summarizer", system=ANALYST, instruction=ask,
                                               json_schema=RECAP_SCHEMA, temperature=0.2, purpose="war room recap"))
        d = resp.data or {}
        return self.library.end_war_session(session_id, d.get("summary", ""), d.get("decisions") or decided,
                                            d.get("open_questions", []), d.get("next_steps", []))


def _split_target(target: str) -> tuple[str, str]:
    kind, _, name = (target or "").partition(":")
    if not name.strip():
        raise ValidationError("a story-bible proposal needs a target like 'character:Elias'")
    return kind.strip().lower(), name.strip()


def _chapter_target(target: str) -> int:
    try:
        return int(str(target).strip() or "0")
    except ValueError:
        raise ValidationError("an outline proposal needs a chapter number (0 adds a new chapter)") from None
