"""The multi-agent writing pipeline.

architect (book plan + bible) -> outliner (chapter outline on a beat sheet) -> scene planner ->
writer (scene groups sized to the model's output budget) -> continuity checks + editor -> reviser
-> curator (canon/story-state update). Only accepted chapters update the story bible.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from jarvis_books.craft import get_structure, match_genre
from jarvis_books.craft.structures import STRUCTURES
from jarvis_books.craft.manuscript import SCENE_BREAK
from jarvis_books.llm.models import LLMRequest, PromptSection
from jarvis_books.memory.context import ContextBuilder
from jarvis_books.memory.index import StoryIndex
from jarvis_books.storage.library import Library
from jarvis_books.storage.models import Book, Chapter
from jarvis_books.text import truncate_words, word_count
from jarvis_books.writer import prompts, schemas
from jarvis_books.writer.continuity import check_chapter

log = logging.getLogger(__name__)
Progress = Callable[[float, str], None]


class Cancelled(Exception):
    pass


class BookWriter:
    def __init__(self, library: Library, engine, index: StoryIndex, context: Optional[ContextBuilder] = None):
        self.library = library
        self.engine = engine
        self.index = index
        self.context = context or ContextBuilder(library, index)
        self.guidance: Optional[Callable[[Optional[str]], str]] = None  # Jarvis's lessons about the author's taste

    def _system(self, base: str, book_id: Optional[str]) -> str:
        if self.guidance is None:
            return base
        try:
            extra = self.guidance(book_id)
        except Exception:
            log.exception("writing guidance unavailable")
            extra = ""
        return base + ("\n\n" + extra if extra else "")

    # ---- helpers ----------------------------------------------------------------
    @staticmethod
    def _check(cancel: Optional[threading.Event]) -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    def _config(self):
        return self.engine.get_runtime_configuration()

    # ---- book planning ------------------------------------------------------------
    def create_book(self, idea: str, title: str = "", genre: str = "", chapters: int = 0,
                    progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None) -> Book:
        progress = progress or (lambda p, m: None)
        profile = match_genre(f"{genre} {idea}")
        progress(0.05, "Designing the story, characters and world")
        brief = [f"The author's idea: {idea.strip()}"]
        if title:
            brief.append(f"Working title: {title}")
        brief.append(f"Genre guidance: {profile.guidance()}")
        brief.append("Structures to choose from: " + ", ".join(f"{k} ({s.name})" for k, s in STRUCTURES.items()))
        plan = self.engine.generate(LLMRequest(
            role="planner", system=self._system(prompts.ARCHITECT, None), json_schema=schemas.BOOK_PLAN, purpose="book plan",
            instruction="Design this book. Return the full plan as JSON. Use 4-8 main characters, 3-6 key "
                        "locations, and 3-6 plot threads.", sections=[PromptSection("Brief", "\n".join(brief), 100, True)],
        )).data
        self._check(cancel)
        n = chapters or profile.suggested_chapters
        ch_words = profile.target_chapter_words
        book = self.library.create_book(
            title or plan["title"], plan.get("genre") or profile.name, idea.strip(), logline=plan["logline"],
            audience=plan.get("audience") or "adult", pov=plan.get("pov") or profile.default_pov,
            tense=plan.get("tense") or profile.default_tense, structure=plan.get("structure") or profile.structure,
            style_notes=plan.get("style_notes", ""), synopsis=plan.get("synopsis", ""),
            target_words=n * ch_words, chapter_words_min=profile.chapter_words[0],
            chapter_words_max=profile.chapter_words[1], status="planned",
            settings={"themes": plan.get("themes", []), "genre_key": profile.key})
        for c in plan.get("characters", []):
            self.library.upsert_bible(book.id, "character", c["name"], c.get("summary", ""),
                                      {k: c.get(k, "") for k in ("role", "want", "need", "flaw", "arc", "voice")})
        for loc in plan.get("locations", []):
            self.library.upsert_bible(book.id, "location", loc["name"], loc.get("summary", ""))
        for t in plan.get("threads", []):
            self.library.upsert_bible(book.id, "thread", t["name"], t.get("summary", ""), {"status": "open"})
        for theme in plan.get("themes", [])[:5]:
            self.library.upsert_bible(book.id, "theme", str(theme)[:80], str(theme))
        progress(0.5, f"Outlining {n} chapters")
        self.outline_book(book.id, n, cancel=cancel)
        progress(1.0, "Book planned")
        return self.library.get_book(book.id)

    def outline_book(self, book_id: str, chapters: int, cancel: Optional[threading.Event] = None) -> list[Chapter]:
        book = self.library.get_book(book_id)
        structure = get_structure(book.structure)
        target = (book.chapter_words_min + book.chapter_words_max) // 2 or 2500
        result = self.engine.generate(LLMRequest(
            role="planner", system=self._system(prompts.OUTLINER, book_id), json_schema=schemas.OUTLINE, purpose="outline",
            sections=[PromptSection("Book", self.context.book_brief(book_id), 100, True),
                      PromptSection("Story bible", self.context.bible_text(book_id), 80),
                      PromptSection("Structure", structure.outline_guide(chapters), 90, True)],
            instruction=f"Write a chapter-by-chapter outline with exactly {chapters} chapters, numbered 1-{chapters}. "
                        f"Each outline is 3-5 sentences naming who, what happens, what changes and the ending hook. "
                        f"target_words about {target} (shorter for fast chapters, longer for big set pieces).",
        )).data
        self._check(cancel)
        items = sorted(result["chapters"], key=lambda c: c["number"])
        for i, item in enumerate(items, start=1):
            item["number"] = i
            lo, hi = book.chapter_words_min or 1000, book.chapter_words_max or 6000
            item["target_words"] = max(lo, min(hi, int(item.get("target_words") or target)))
            if item.get("beat"):
                item["outline"] = f"{item['outline']} [Beat: {item['beat']}]"
        return self.library.set_outline(book_id, items)

    # ---- chapters -------------------------------------------------------------------
    def plan_chapter(self, book_id: str, number: int) -> Chapter:
        chapter = self.library.get_chapter(book_id, number)
        target = chapter.target_words or 2500
        data = self.engine.generate(LLMRequest(
            role="planner", system=self._system(prompts.SCENE_PLANNER, book_id), json_schema=schemas.CHAPTER_PLAN, purpose="scene plan",
            sections=self.context.chapter_sections(book_id, number),
            instruction=f"Plan the scenes for {chapter.heading}. Outline: {chapter.outline}\n"
                        f"Use 2-5 scenes whose target_words add up to about {target}. Give the closing hook.",
        )).data
        plan = data["scenes"]
        for scene in plan:
            scene["target_words"] = max(300, int(scene.get("target_words") or target // max(1, len(plan))))
        plan.append({"hook": data.get("hook", "")})
        return self.library.update_chapter(chapter.id, plan=plan, status="planned")

    @staticmethod
    def group_scenes(scenes: list[dict], words_per_call: int) -> list[list[dict]]:
        """Group consecutive scenes so each writer call stays within the adaptive output budget."""
        groups: list[list[dict]] = []
        current: list[dict] = []
        total = 0
        for scene in scenes:
            words = int(scene.get("target_words") or 800)
            if current and total + words > words_per_call:
                groups.append(current)
                current, total = [], 0
            current.append(scene)
            total += words
        if current:
            groups.append(current)
        return groups

    def draft_chapter(self, book_id: str, number: int, instructions: str = "",
                      progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None) -> Chapter:
        progress = progress or (lambda p, m: None)
        chapter = self.library.get_chapter(book_id, number)
        if not [s for s in chapter.plan if "goal" in s]:
            progress(0.05, f"Planning scenes for {chapter.heading}")
            chapter = self.plan_chapter(book_id, number)
        self._check(cancel)
        scenes = [s for s in chapter.plan if "goal" in s]
        hook = next((s["hook"] for s in chapter.plan if "hook" in s), "")
        config = self._config()
        groups = self.group_scenes(scenes, config.words_per_call)
        parts: list[str] = []
        base_sections = self.context.chapter_sections(book_id, number)
        for gi, group in enumerate(groups):
            self._check(cancel)
            progress(0.1 + 0.8 * gi / len(groups), f"Writing {chapter.heading} (part {gi + 1} of {len(groups)})")
            target = sum(int(s.get("target_words") or 800) for s in group)
            scene_text = "\n".join(
                f"Scene {scenes.index(s) + 1}: POV {s['pov']}; setting {s['setting']}; goal {s['goal']}; conflict "
                f"{s['conflict']}; outcome {s['outcome']}; beats: {s['beats']} (~{s['target_words']} words)" for s in group)
            sections = list(base_sections) + [PromptSection("Scenes to write now", scene_text, 98, True)]
            if parts:
                sections.append(PromptSection("This chapter so far (continue directly after it)",
                                              truncate_words("\n\n".join(parts), 900, from_end=True), 97, True))
            last = gi == len(groups) - 1
            ask = (f"Write these scenes of {chapter.heading} as finished prose, about {target} words. "
                   + (f"End the chapter on this hook: {hook}. " if last and hook else "")
                   + ("" if last else "Stop at the end of the last listed scene. ")
                   + (f"Author's instructions: {instructions}" if instructions else ""))
            tokens = int(target * 1.35 * 1.6) + 512
            resp = self.engine.generate(LLMRequest(role="writer", system=self._system(prompts.WRITER, book_id), sections=sections,
                                                   instruction=ask, max_output_tokens=tokens, temperature=0.9,
                                                   purpose="draft"))
            parts.append(resp.text.strip())
        text = f"\n\n{SCENE_BREAK}\n\n".join(p for p in parts if p)
        progress(0.95, f"Saving {chapter.heading}")
        return self.library.save_chapter_text(chapter.id, text, source="ai-draft",
                                              note=f"drafted in {len(groups)} part(s)", status="drafted")

    def review_chapter(self, book_id: str, number: int, revise: bool = True,
                       cancel: Optional[threading.Event] = None) -> tuple[Chapter, list[dict]]:
        book = self.library.get_book(book_id)
        chapter = self.library.get_chapter(book_id, number)
        bible = self.library.list_bible(book_id)
        findings = [f.__dict__ for f in check_chapter(chapter.text, bible, chapter.target_words, book.tense)]
        review = self.engine.generate(LLMRequest(
            role="editor", system=self._system(prompts.EDITOR, book_id), json_schema=schemas.REVIEW, purpose="review",
            sections=[PromptSection("Book", self.context.book_brief(book_id), 100, True),
                      PromptSection("Story bible", self.context.bible_text(book_id, chapter.text[:2000]), 70),
                      PromptSection("Story so far", self.context.story_so_far(book_id, number), 60, trim_from="start"),
                      PromptSection("Chapter plan", str(chapter.plan), 80),
                      PromptSection("Automatic checks", "\n".join(f"- {f['severity']}: {f['description']}"
                                                                   for f in findings) or "none", 85),
                      PromptSection(f"{chapter.heading} text", chapter.text, 99, True)],
            instruction="Review this chapter. Return a verdict and the issues as JSON.",
        )).data
        issues = findings + [dict(i) for i in review.get("issues", [])]
        major = [i for i in issues if i.get("severity") == "major"]
        self._check(cancel)
        if revise and (review.get("verdict") == "revise" or major):
            notes = "\n".join(f"- [{i.get('severity')}] {i.get('description')} Fix: {i.get('fix', '')}" for i in issues)
            chapter = self.revise_chapter(book_id, number, notes, source="ai-revision")
        return chapter, issues

    def revise_chapter(self, book_id: str, number: int, instructions: str, source: str = "ai-revision") -> Chapter:
        chapter = self.library.get_chapter(book_id, number)
        if not chapter.text.strip():
            return self.draft_chapter(book_id, number, instructions)
        sections = self.context.chapter_sections(book_id, number) + [
            PromptSection(f"Current {chapter.heading}", chapter.text, 99, True)]
        tokens = int(max(word_count(chapter.text), chapter.target_words) * 1.35 * 1.5) + 512
        resp = self.engine.generate(LLMRequest(role="writer", system=self._system(prompts.REVISER, book_id), sections=sections,
                                               instruction=f"Revise the chapter. Notes:\n{instructions}",
                                               max_output_tokens=tokens, temperature=0.8, purpose="revision"))
        text = resp.text.strip()
        if word_count(text) < word_count(chapter.text) * 0.5:  # a truncated revision must not replace the draft
            log.warning("revision of %s was too short; keeping the previous text", chapter.heading)
            return chapter
        return self.library.save_chapter_text(chapter.id, text, source=source, note=instructions[:200])

    def accept_chapter(self, book_id: str, number: int) -> Chapter:
        """Promote a chapter to canon: summarize it and update the story bible from what actually happened."""
        chapter = self.library.get_chapter(book_id, number)
        data = self.engine.generate(LLMRequest(
            role="summarizer", system=prompts.CURATOR, json_schema=schemas.STATE_UPDATE, purpose="canon update",
            sections=[PromptSection("Story bible", self.context.bible_text(book_id, chapter.text[:2000]), 70),
                      PromptSection(f"{chapter.heading} text", chapter.text, 99, True)],
            instruction="Record what is now canon after this chapter. The summary is 80-150 words.",
        )).data
        for c in data.get("character_updates", []):
            existing = next((e for e in self.library.list_bible(book_id, "character")
                             if e.name.lower() == c["name"].lower()), None)
            summary = c.get("summary") or (existing.summary if existing else "")
            history = (existing.data.get("history", []) if existing else []) + [f"Ch {number}: {c.get('summary', '')}"]
            self.library.upsert_bible(book_id, "character", c["name"], existing.summary if existing else summary,
                                      {"status": c.get("status", ""), "history": history[-12:]})
        for e in data.get("new_entities", []):
            self.library.upsert_bible(book_id, e["kind"], e["name"], e.get("summary", ""), {"introduced": number})
        for t in data.get("open_threads", []):
            self.library.upsert_bible(book_id, "thread", t["name"], t.get("summary", ""), {"status": "open"})
        for name in data.get("resolved_threads", []):
            self.library.upsert_bible(book_id, "thread", name, "", {"status": "resolved", "resolved_in": number})
        chapter = self.library.update_chapter(chapter.id, summary=data["summary"], status="accepted")
        self.index.index_chapter(chapter.id)
        self.library.update_book(book_id, status="drafting")
        return chapter

    def write_chapter(self, book_id: str, number: int, instructions: str = "", progress: Optional[Progress] = None,
                      cancel: Optional[threading.Event] = None) -> Chapter:
        progress = progress or (lambda p, m: None)
        self.draft_chapter(book_id, number, instructions,
                           progress=lambda p, m: progress(p * 0.7, m), cancel=cancel)
        progress(0.75, "Editing and checking continuity")
        self.review_chapter(book_id, number, cancel=cancel)
        progress(0.9, "Updating the story bible")
        chapter = self.accept_chapter(book_id, number)
        progress(1.0, f"{chapter.heading} done ({chapter.word_count:,} words)")
        return chapter

    def next_unwritten(self, book_id: str) -> Optional[Chapter]:
        return next((c for c in self.library.list_chapters(book_id) if c.status != "accepted"), None)

    def write_next_chapters(self, book_id: str, count: int = 1, instructions: str = "",
                            progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None) -> list[Chapter]:
        progress = progress or (lambda p, m: None)
        done = []
        for i in range(max(1, count)):
            chapter = self.next_unwritten(book_id)
            if chapter is None:
                self.library.update_book(book_id, status="complete")
                break
            done.append(self.write_chapter(book_id, chapter.number, instructions, cancel=cancel,
                                           progress=lambda p, m, i=i: progress((i + p) / count, m)))
        if self.next_unwritten(book_id) is None:
            self.library.update_book(book_id, status="complete")
        return done
