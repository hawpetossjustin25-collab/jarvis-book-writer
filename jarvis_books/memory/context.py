"""Assemble prioritized prompt context for a chapter: the engine trims it to fit the model."""
from __future__ import annotations

from jarvis_books.craft import get_structure, match_genre
from jarvis_books.craft.manuscript import PROSE_RULES
from jarvis_books.llm.models import PromptSection
from jarvis_books.memory.index import StoryIndex
from jarvis_books.storage.library import Library
from jarvis_books.text import truncate_words


class ContextBuilder:
    def __init__(self, library: Library, index: StoryIndex):
        self.library = library
        self.index = index

    def book_brief(self, book_id: str) -> str:
        book = self.library.get_book(book_id)
        genre = match_genre(book.genre)
        lines = [f"Title: {book.title}", f"Genre: {book.genre}", f"Audience: {book.audience}"]
        for label, value in (("Logline", book.logline), ("Premise", book.premise), ("Point of view", book.pov),
                             ("Tense", book.tense), ("Synopsis", book.synopsis)):
            if value:
                lines.append(f"{label}: {value}")
        lines.append(f"Genre guidance: {genre.guidance()}")
        return "\n".join(lines)

    def style_guide(self, book_id: str) -> str:
        book = self.library.get_book(book_id)
        rules = "\n".join(f"- {r}" for r in PROSE_RULES)
        extra = f"\nAuthor's style notes: {book.style_notes}" if book.style_notes else ""
        styles = [e for e in self.library.list_bible(book_id, "style")]
        extra += "".join(f"\n- {e.name}: {e.summary}" for e in styles)
        return rules + extra

    def bible_text(self, book_id: str, focus: str = "") -> str:
        entries = self.library.list_bible(book_id)
        focus_l = focus.lower()
        # Characters/places named in the focus text come first so trimming keeps them.
        entries.sort(key=lambda e: (e.name.lower() not in focus_l, e.kind != "character", e.kind, e.name))
        lines = []
        for e in entries:
            if e.kind == "style":
                continue
            facts = "; ".join(f"{k}: {v}" for k, v in e.data.items() if v not in (None, "", [], {}))
            lines.append(f"- [{e.kind}] {e.name}: {e.summary}" + (f" ({facts})" if facts else ""))
        return "\n".join(lines)

    def outline_text(self, book_id: str) -> str:
        chapters = self.library.list_chapters(book_id)
        book = self.library.get_book(book_id)
        structure = get_structure(book.structure)
        lines = [structure.name]
        for c in chapters:
            lines.append(f"Ch {c.number} {c.title}: {c.outline}")
        return "\n".join(lines)

    def story_so_far(self, book_id: str, before: int) -> str:
        parts = []
        for c in self.library.list_chapters(book_id):
            if c.number >= before:
                break
            summary = c.summary or (truncate_words(c.text, 120) if c.text else c.outline)
            if summary:
                parts.append(f"Ch {c.number} {c.title}: {summary}")
        return "\n".join(parts)

    def chapter_sections(self, book_id: str, number: int) -> list[PromptSection]:
        chapter = self.library.get_chapter(book_id, number)
        sections = [
            PromptSection("Book", self.book_brief(book_id), priority=100, required=True),
            PromptSection("Style rules", self.style_guide(book_id), priority=95, required=True),
            PromptSection("Story bible", self.bible_text(book_id, chapter.outline), priority=75),
            PromptSection("Story so far (chapter summaries)", self.story_so_far(book_id, number), priority=70,
                          trim_from="start"),
            PromptSection("Full outline", self.outline_text(book_id), priority=55),
        ]
        if number > 1:
            try:
                prev = self.library.get_chapter(book_id, number - 1)
                if prev.text.strip():
                    sections.append(PromptSection("End of the previous chapter (continue seamlessly from here)",
                                                  truncate_words(prev.text, 700, from_end=True), priority=85,
                                                  trim_from="start"))
            except Exception:
                pass
        notes = self.library.list_notes(book_id)[:15]
        if notes:
            sections.append(PromptSection("Author's notes and ideas", "\n".join(f"- {n.text}" for n in notes), priority=80))
        if chapter.outline:
            hits = [h for h in self.index.search(chapter.outline, book_id, k=4) if h.chapter_number < number]
            if hits:
                sections.append(PromptSection("Relevant earlier passages",
                                              "\n\n".join(f"(Ch {h.chapter_number}) {h.text}" for h in hits), priority=40))
        return sections
