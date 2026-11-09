"""Tools that Jarvis (text chat and live voice) can call to read and change the user's books.

The same declarations and dispatcher serve both the REST chat path and the Gemini Live voice session.
Long-running operations are started as background jobs and return immediately.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from jarvis_books.craft import get_structure, match_genre
from jarvis_books.errors import NotFound
from jarvis_books.jobs import JobManager
from jarvis_books.llm.schema import validate
from jarvis_books.memory.index import StoryIndex
from jarvis_books.storage.library import Library
from jarvis_books.text import paragraphs, truncate_words
from jarvis_books.writer.pipeline import BookWriter

log = logging.getLogger(__name__)

S = {"type": "STRING"}
I = {"type": "INTEGER"}


def _decl(name: str, description: str, props: Optional[dict] = None, required: Optional[list[str]] = None) -> dict:
    params: dict = {"type": "OBJECT", "properties": props or {}}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


BOOK = {"type": "STRING", "description": "Book title (partial titles are fine). Omit for the open book."}

DECLARATIONS = [
    _decl("list_books", "List all of the user's books with status and progress."),
    _decl("open_book", "Open a book and get its overview: logline, chapters, status and word counts.",
          {"book": BOOK}, ["book"]),
    _decl("create_book", "Start a new book from the user's story idea. Designs characters, world and a full "
          "chapter outline in the background.",
          {"idea": S, "title": S, "genre": S, "chapters": {"type": "INTEGER", "description": "0 = recommended"}},
          ["idea"]),
    _decl("write_chapters", "Write the next unwritten chapter(s) of a book in the background (plan, draft, "
          "edit, continuity check).",
          {"book": BOOK, "count": I, "instructions": {"type": "STRING", "description": "Author's direction"}}),
    _decl("rewrite_chapter", "Revise a chapter following the author's instructions (keeps the old version).",
          {"book": BOOK, "chapter": I, "instructions": S}, ["chapter", "instructions"]),
    _decl("read_chapter", "Get the text of a chapter to read aloud or discuss.",
          {"book": BOOK, "chapter": I, "start_paragraph": I, "max_words": I}, ["chapter"]),
    _decl("search_books", "Search the text of the user's books for a moment, character or detail.",
          {"query": S, "book": BOOK}, ["query"]),
    _decl("story_bible", "Get the story bible (characters, places, items, plot threads) of a book.",
          {"book": BOOK, "kind": {"type": "STRING", "enum": ["character", "location", "item", "rule", "thread",
                                                           "theme", "event", "style", "all"]}}),
    _decl("update_story_bible", "Add or change a story-bible entry (a character, place, rule, etc.).",
          {"book": BOOK, "kind": S, "name": S, "summary": S}, ["kind", "name", "summary"]),
    _decl("add_note", "Save a brainstorming idea or note for a book.", {"book": BOOK, "text": S}, ["text"]),
    _decl("change_outline", "Change a chapter's title or outline, or add a new chapter.",
          {"book": BOOK, "chapter": {"type": "INTEGER", "description": "Chapter number; 0 adds a new chapter"},
           "title": S, "outline": S}, ["chapter"]),
    _decl("writing_advice", "Professional guidance for a genre: length, chapter length, POV, structure, "
          "conventions.", {"genre": S}, ["genre"]),
    _decl("job_status", "What background jobs (writing, audiobooks) are running or finished."),
    _decl("cancel_job", "Stop a running background job.", {"job_id": S}, ["job_id"]),
    _decl("play_audiobook", "Play the audiobook of a book (works for unfinished books).",
          {"book": BOOK, "chapter": I}),
    _decl("stop_audiobook", "Pause the audiobook."),
    _decl("export_book", "Export a book as a manuscript file.",
          {"book": BOOK, "format": {"type": "STRING", "enum": ["docx", "epub", "markdown", "mp3", "m4b"]}}),
]

_BY_NAME = {d["name"]: d for d in DECLARATIONS}


class ToolBox:
    def __init__(self, library: Library, writer: BookWriter, index: StoryIndex, jobs: JobManager):
        self.library = library
        self.writer = writer
        self.index = index
        self.jobs = jobs
        self.current_book_id: Optional[str] = None
        self.audio_handler: Optional[Callable[[str, dict], dict]] = None  # set by the UI (play/stop/export audio)
        self.export_handler: Optional[Callable[[str, str], dict]] = None

    def declarations(self, names: Optional[list[str]] = None) -> list[dict]:
        return [d for d in DECLARATIONS if names is None or d["name"] in names]

    def select(self, user_text: str) -> list[dict]:
        """Adaptive tool selection: always the core tools, plus the rest only when the request needs them."""
        core = ["list_books", "open_book", "read_chapter", "search_books", "add_note", "write_chapters",
                "create_book", "job_status"]
        t = (user_text or "").lower()
        extra = {
            "rewrite_chapter": ("rewrite", "revise", "change", "fix", "edit", "redo"),
            "story_bible": ("character", "bible", "who is", "place", "world", "thread"),
            "update_story_bible": ("character", "bible", "add", "rename", "change"),
            "change_outline": ("outline", "chapter", "plot", "add a chapter"),
            "writing_advice": ("how long", "advice", "genre", "length", "structure", "format"),
            "cancel_job": ("stop", "cancel"),
            "play_audiobook": ("play", "listen", "audio", "read me", "narrat"),
            "stop_audiobook": ("stop", "pause", "audio"),
            "export_book": ("export", "docx", "epub", "word", "file", "mp3", "m4b", "audiobook"),
        }
        names = core + [n for n, words in extra.items() if any(w in t for w in words)]
        return self.declarations(names)

    # ---- dispatch -----------------------------------------------------------------
    def call(self, name: str, args: Optional[dict] = None) -> dict:
        args = dict(args or {})
        decl = _BY_NAME.get(name)
        if decl is None:
            return {"error": f"unknown tool {name}"}
        errors = validate(args, decl["parameters"])
        if errors:
            return {"error": "invalid arguments: " + "; ".join(errors)}
        try:
            return getattr(self, f"_t_{name}")(**args)
        except NotFound as exc:
            return {"error": str(exc)}
        except Exception as exc:
            log.exception("tool %s failed", name)
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _book(self, book: Optional[str]):
        if book:
            found = self.library.find_book(book)
            if found is None:
                titles = ", ".join(b.title for b in self.library.list_books()) or "none yet"
                raise NotFound(f"no book matching '{book}'. Books: {titles}")
            self.current_book_id = found.id
            return found
        if self.current_book_id:
            return self.library.get_book(self.current_book_id)
        books = self.library.list_books()
        if len(books) == 1:
            self.current_book_id = books[0].id
            return books[0]
        raise NotFound("which book? " + (", ".join(b.title for b in books) or "there are no books yet"))

    def _t_list_books(self) -> dict:
        out = []
        for b in self.library.list_books():
            stats = self.library.book_stats(b.id)
            out.append({"title": b.title, "genre": b.genre, "status": b.status, **stats})
        return {"books": out}

    def _t_open_book(self, book: str) -> dict:
        b = self._book(book)
        chapters = [{"number": c.number, "title": c.title, "status": c.status, "words": c.word_count,
                     "outline": truncate_words(c.outline, 40)} for c in self.library.list_chapters(b.id)]
        return {"title": b.title, "genre": b.genre, "logline": b.logline, "status": b.status, "pov": b.pov,
                "tense": b.tense, "structure": get_structure(b.structure).name, "chapters": chapters,
                **self.library.book_stats(b.id)}

    def _t_create_book(self, idea: str, title: str = "", genre: str = "", chapters: int = 0) -> dict:
        def run(progress, cancel):
            book = self.writer.create_book(idea, title, genre, chapters, progress=progress, cancel=cancel)
            self.current_book_id = book.id
            return book.id
        job = self.jobs.submit("create_book", f"Planning '{title or idea[:40]}'", run)
        return {"started": True, "job_id": job.id, "note": "Planning runs in the background (about a minute)."}

    def _t_write_chapters(self, book: str = "", count: int = 1, instructions: str = "") -> dict:
        b = self._book(book)
        nxt = self.writer.next_unwritten(b.id)
        if nxt is None:
            return {"error": f"every chapter of {b.title} is already written"}
        count = max(1, min(10, count))
        job = self.jobs.submit("write", f"Writing {b.title}: {count} chapter(s) from {nxt.heading}",
                               lambda p, c: [ch.number for ch in self.writer.write_next_chapters(
                                   b.id, count, instructions, progress=p, cancel=c)], book_id=b.id)
        return {"started": True, "job_id": job.id, "first_chapter": nxt.number, "count": count}

    def _t_rewrite_chapter(self, chapter: int, instructions: str, book: str = "") -> dict:
        b = self._book(book)
        self.library.get_chapter(b.id, chapter)
        job = self.jobs.submit("revise", f"Revising {b.title} chapter {chapter}",
                               lambda p, c: self.writer.revise_chapter(b.id, chapter, instructions, "ai-revision").number,
                               book_id=b.id)
        return {"started": True, "job_id": job.id}

    def _t_read_chapter(self, chapter: int, book: str = "", start_paragraph: int = 0, max_words: int = 900) -> dict:
        b = self._book(book)
        ch = self.library.get_chapter(b.id, chapter)
        paras = paragraphs(ch.text)
        if not paras:
            return {"chapter": chapter, "title": ch.title, "text": "", "note": "not written yet",
                    "outline": ch.outline}
        out, words, i = [], 0, max(0, start_paragraph)
        while i < len(paras) and words < max(100, min(3000, max_words)):
            out.append(paras[i])
            words += len(paras[i].split())
            i += 1
        return {"chapter": chapter, "title": ch.title, "text": "\n\n".join(out),
                "next_paragraph": i if i < len(paras) else None, "total_paragraphs": len(paras)}

    def _t_search_books(self, query: str, book: str = "") -> dict:
        book_id = self._book(book).id if book else None
        hits = self.index.search(query, book_id, k=6)
        return {"results": [{"book": h.book_title, "chapter": h.chapter_number, "chapter_title": h.chapter_title,
                             "passage": truncate_words(h.text, 120)} for h in hits]}

    def _t_story_bible(self, book: str = "", kind: str = "all") -> dict:
        b = self._book(book)
        entries = self.library.list_bible(b.id, None if kind in ("", "all") else kind)
        return {"entries": [{"kind": e.kind, "name": e.name, "summary": e.summary,
                             "facts": {k: v for k, v in e.data.items() if k != "history"}} for e in entries[:60]]}

    def _t_update_story_bible(self, kind: str, name: str, summary: str, book: str = "") -> dict:
        b = self._book(book)
        e = self.library.upsert_bible(b.id, kind, name, summary)
        return {"saved": True, "kind": e.kind, "name": e.name}

    def _t_add_note(self, text: str, book: str = "") -> dict:
        try:
            book_id = self._book(book).id
        except NotFound:
            book_id = None
        note = self.library.add_note(book_id, text, source="jarvis")
        return {"saved": True, "note_id": note.id}

    def _t_change_outline(self, chapter: int, title: str = "", outline: str = "", book: str = "") -> dict:
        b = self._book(book)
        if chapter <= 0:
            ch = self.library.add_chapter(b.id, title=title, outline=outline,
                                          target_words=(b.chapter_words_min + b.chapter_words_max) // 2)
        else:
            ch = self.library.get_chapter(b.id, chapter)
            fields = {k: v for k, v in (("title", title), ("outline", outline)) if v}
            ch = self.library.update_chapter(ch.id, **fields) if fields else ch
        return {"saved": True, "chapter": ch.number, "title": ch.title}

    def _t_writing_advice(self, genre: str) -> dict:
        g = match_genre(genre)
        s = get_structure(g.structure)
        return {"genre": g.name, "guidance": g.guidance(), "recommended_chapters": g.suggested_chapters,
                "structure": s.name, "beats": [f"{b.name} (~{int(b.position * 100)}%): {b.purpose}"
                                               for b in s.beats]}

    def _t_job_status(self) -> dict:
        return {"jobs": [{"id": j.id, "title": j.title, "status": j.status, "progress": round(j.progress * 100),
                          "message": j.message} for j in self.jobs.recent(8)]}

    def _t_cancel_job(self, job_id: str) -> dict:
        return {"cancelled": self.jobs.cancel(job_id)}

    def _t_play_audiobook(self, book: str = "", chapter: int = 0) -> dict:
        b = self._book(book)
        if self.audio_handler is None:
            return {"error": "audio player is not available"}
        return self.audio_handler("play", {"book_id": b.id, "chapter": chapter})

    def _t_stop_audiobook(self) -> dict:
        if self.audio_handler is None:
            return {"error": "audio player is not available"}
        return self.audio_handler("stop", {})

    def _t_export_book(self, book: str = "", format: str = "docx") -> dict:
        b = self._book(book)
        if self.export_handler is None:
            return {"error": "export is not available"}
        return self.export_handler(b.id, format)
