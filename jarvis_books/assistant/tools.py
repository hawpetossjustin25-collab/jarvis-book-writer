"""Tools that Jarvis (text chat and live voice) can call to read and change the user's books.

The same declarations and dispatcher serve both the REST chat path and the Gemini Live voice session.
Long-running operations are started as background jobs and return immediately.
"""
from __future__ import annotations

import itertools
import logging
from typing import Any, Callable, Optional

from jarvis_books.assistant.permissions import ASKING_NOTE, PermissionGate
from jarvis_books.audio.narrator import EDGE_VOICES, GEMINI_VOICES
from jarvis_books.audio.voices import AGES, GEMINI_CATALOG, GENDERS, NARRATOR, default_style, describe_voice, \
    edge_matching, who
from jarvis_books.craft import get_structure, match_genre
from jarvis_books.errors import NotFound
from jarvis_books.jobs import JobManager
from jarvis_books.llm.schema import validate
from jarvis_books.memory.index import StoryIndex
from jarvis_books.storage.library import Library
from jarvis_books.text import paragraphs, truncate_words
from jarvis_books.upgrade import describe as describe_upgrade
from jarvis_books.writer.pipeline import BookWriter

log = logging.getLogger(__name__)

# In the War Room canon changes go through proposals, so the direct-edit tools are withheld.
WAR_ROOM_BLOCKED = ("update_story_bible", "change_outline")
S = {"type": "STRING"}
I = {"type": "INTEGER"}


def _decl(name: str, description: str, props: Optional[dict] = None, required: Optional[list[str]] = None) -> dict:
    params: dict = {"type": "OBJECT", "properties": props or {}}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


BOOK = {"type": "STRING", "description": "Book title (partial titles are fine). Omit for the open book."}
TABS = ("jarvis", "chat", "library", "write", "listen", "settings", "war room", "publish")
SETTING_KEYS = ("voice", "edge_voice", "jarvis_voice", "author", "wake_word", "narration_speed", "auto_mute")
SETTING_CHOICES = {"voice": GEMINI_VOICES, "jarvis_voice": GEMINI_VOICES, "edge_voice": EDGE_VOICES}
APP_ACTIONS = ("mute_mic", "unmute_mic", "end_voice", "open_book_folder")
_ON = {"1", "on", "true", "yes", "enable", "enabled"}
_OFF = {"0", "off", "false", "no", "disable", "disabled"}

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
    _decl("war_briefing", "War Room: brief the author on a book - what is written vs only planned, open threads, "
          "undecided points and last session's decisions.", {"book": BOOK, "focus": S}),
    _decl("find_gaps", "War Room: list what the story has not decided yet, with options for each.",
          {"book": BOOK, "focus": S}),
    _decl("propose_idea", "War Room: pitch an idea. It is NOT canon until the author accepts it. kind=bible needs "
          "target 'character:Name' (or location/item/rule/thread/theme/event); kind=outline needs target chapter "
          "number (0 = new chapter); kind=note for anything else.",
          {"book": BOOK, "kind": {"type": "STRING", "enum": ["bible", "outline", "note"]}, "title": S, "content": S,
           "target": S, "rationale": S}, ["kind", "title", "content"]),
    _decl("list_proposals", "War Room: list ideas and decisions (proposed, accepted, rejected, undone).",
          {"book": BOOK, "status": {"type": "STRING", "enum": ["proposed", "accepted", "rejected", "undone", "all"]}}),
    _decl("decide_proposal", "War Room: apply the author's decision on a proposal. Only when the author says so.",
          {"proposal_id": I, "decision": {"type": "STRING", "enum": ["accept", "reject", "undo"]},
           "content": {"type": "STRING", "description": "Edited wording, if the author changed it"}},
          ["proposal_id", "decision"]),
    _decl("publish_audiobook", "Post a book (or chosen chapters) to YouTube as an audiobook video in the background. "
          "The app asks the author yes or no before anything is posted.",
          {"book": BOOK, "chapters": {"type": "ARRAY", "items": I}, "title": S, "description": S,
           "privacy": {"type": "STRING", "enum": ["private", "unlisted", "public"]},
           "per_chapter": {"type": "BOOLEAN"}}, ["privacy"]),
    _decl("list_posts", "List the book's YouTube posts with their status and links.", {"book": BOOK}),
    _decl("app_status", "What the app is doing right now: open tab, voice chat, mic, audiobook, background jobs, "
          "storage space and settings."),
    _decl("open_tab", "Switch the app to a tab, optionally showing a book there.",
          {"tab": {"type": "STRING", "enum": list(TABS)}, "book": BOOK}, ["tab"]),
    _decl("get_settings", "The app's settings: audiobook narrator voice, backup narrator, Jarvis voice, author "
          "name, Hey Jarvis wake word, narration speed, auto-mute while Jarvis talks."),
    _decl("change_setting", "Change one app setting. voice/jarvis_voice: a Gemini voice (" + ", ".join(GEMINI_VOICES)
          + "); edge_voice: a backup narrator; narration_speed: 0.5 to 2.0; wake_word and auto_mute: on/off; "
          "author: the author's name.",
          {"key": {"type": "STRING", "enum": list(SETTING_KEYS)}, "value": S}, ["key", "value"]),
    _decl("app_action", "Press a button in the app: mute_mic, unmute_mic, end_voice (hang up this voice chat), "
          "open_book_folder (show the book's files on the computer).",
          {"action": {"type": "STRING", "enum": list(APP_ACTIONS)}, "book": BOOK}, ["action"]),
    _decl("build_upgrade", "Upgrade the app itself (new feature, fix, change in how it works). Gemini changes a "
          "separate test copy, never the running app, and every test runs on it in the background. Nothing is "
          "installed; when it is ready ask the author, then install_upgrade.", {"request": S}, ["request"]),
    _decl("list_upgrades", "App upgrades: what each changes, whether its tests passed, and which are installed."),
    _decl("install_upgrade", "Install a tested app upgrade (status ready). The app restarts.",
          {"upgrade_id": I}, ["upgrade_id"]),
    _decl("undo_upgrade", "Put the app back the way it was before an installed upgrade (latest if omitted). The "
          "app restarts.", {"upgrade_id": I}),
    _decl("undo_last_change", "Undo the most recent change: a chapter rewrite, outline or title change, new "
          "chapter, story bible entry, accepted War Room idea, setting or voice."),
    _decl("voice_cast", "Audiobook voices of a book: the narrator and the voice each character speaks in "
          "(man, woman, boy, girl, older...).", {"book": BOOK}),
    _decl("cast_voices", "Find every character in the story bible and give each a fitting voice for the "
          "audiobook (voices the author picked are kept).", {"book": BOOK}),
    _decl("set_voice", "Change how the narrator or a character sounds in the audiobook. character: a name, or "
          "'narrator'. voice: one of " + ", ".join(describe_voice(v) for v in GEMINI_CATALOG) + ". gender: male, "
          "female or unknown. age: child, teen, adult or elder. style: a short direction like 'deep and slow' or "
          "'a shy girl about ten'.",
          {"book": BOOK, "character": S, "voice": S, "gender": {"type": "STRING", "enum": list(GENDERS)},
           "age": {"type": "STRING", "enum": list(AGES)}, "style": S}, ["character"]),
]

_BY_NAME = {d["name"]: d for d in DECLARATIONS}


def _check(decl: Optional[dict], name: str, args: dict) -> str:
    if decl is None:
        return f"unknown tool {name}"
    unknown = sorted(set(args) - set(decl["parameters"].get("properties", {})))
    errors = validate(args, decl["parameters"]) + [f"unknown argument {k!r}" for k in unknown]
    return "invalid arguments: " + "; ".join(errors) if errors else ""


class ToolBox:
    def __init__(self, library: Library, writer: BookWriter, index: StoryIndex, jobs: JobManager,
                 gate: Optional[PermissionGate] = None, undo: Optional[list] = None):
        self.library = library
        self.gate = gate or PermissionGate()
        self.writer = writer
        self.index = index
        self.jobs = jobs
        self.current_book_id: Optional[str] = None
        self.audio_handler: Optional[Callable[[str, dict], dict]] = None  # set by the UI (play/stop/export audio)
        self.export_handler: Optional[Callable[[str, str], dict]] = None
        self.warroom = None  # set by the Studio
        self.publisher = None  # set by the Studio
        self.war_session_id: Optional[int] = None  # set while a War Room session is open
        self.app_handler: Optional[Callable[[str, dict], dict]] = None  # set by the UI (tabs, buttons, status)
        self.settings_store = None  # set by the Studio (settings() / set_setting() / capacity)
        self.upgrader = None  # set by the Studio
        self.director = None  # set by the Studio (voice cast)
        self.undo = undo if undo is not None else []  # shared by every Jarvis, newest last
        self._undo_ids = itertools.count(1)

    def declarations(self, names: Optional[list[str]] = None) -> list[dict]:
        blocked = WAR_ROOM_BLOCKED if self.war_session_id is not None else ()
        return [d for d in DECLARATIONS if (names is None or d["name"] in names) and d["name"] not in blocked]

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
            "war_briefing": ("brief", "fill me in", "catch me up", "where are we", "war room", "status"),
            "find_gaps": ("gap", "undecided", "missing", "hole", "not decided", "what if", "unexplained"),
            "propose_idea": ("idea", "what if", "suggest", "pitch", "propose", "should"),
            "list_proposals": ("proposal", "decision", "idea", "decided", "pending"),
            "decide_proposal": ("accept", "reject", "undo", "yes", "approve", "keep", "go with"),
            "publish_audiobook": ("youtube", "post", "upload", "publish", "unlisted"),
            "list_posts": ("youtube", "post", "upload", "publish", "link", "views"),
            "app_status": ("status", "what's playing", "doing", "app", "storage", "space", "running"),
            "open_tab": ("open", "go to", "show", "tab", "switch", "take me"),
            "get_settings": ("setting", "voice", "speed", "wake", "mute", "author name"),
            "change_setting": ("setting", "voice", "speed", "faster", "slower", "wake", "auto-mute", "auto mute",
                               "author name", "my name", "turn on", "turn off"),
            "app_action": ("mute", "unmute", "hang up", "end the call", "goodbye", "bye", "folder", "files"),
            "undo_last_change": ("undo", "take that back", "revert", "put it back", "change it back"),
            "build_upgrade": ("upgrade", "feature", "improve the app", "change the app", "update the app",
                              "add a button", "add an option", "code", "fix the app", "bug"),
            "list_upgrades": ("upgrade", "feature", "install", "tests pass", "code"),
            "install_upgrade": ("upgrade", "install"),
            "undo_upgrade": ("upgrade", "undo", "put the app back", "old version"),
            "voice_cast": ("voice", "cast", "narrator", "sound", "who reads"),
            "cast_voices": ("voice", "cast", "characters sound", "give them voices"),
            "set_voice": ("voice", "narrator", "sound", "deeper", "higher", "older", "younger", "accent"),
        }
        if self.war_session_id is not None:  # in the War Room every War Room tool is always available
            core = core + ["war_briefing", "find_gaps", "propose_idea", "list_proposals", "decide_proposal",
                           "story_bible"]
        names = list(dict.fromkeys(core + [n for n, words in extra.items() if any(w in t for w in words)]))
        if self.war_session_id is not None:
            names = [n for n in names if n not in WAR_ROOM_BLOCKED]
        return self.declarations(names)

    # ---- dispatch -----------------------------------------------------------------
    def call(self, name: str, args: Optional[dict] = None, source: str = "ai") -> dict:
        """A call from Jarvis (Gemini). Anything that changes something waits for the author's yes."""
        args = dict(args or {})
        decl = _BY_NAME.get(name)
        error = _check(decl, name, args)
        if error:
            return {"error": error}
        mode = self.gate.decide(name)
        if mode == "free":
            return self._dispatch(name, args)
        try:
            args = self._pin(decl, args)
            summary = self.describe(name, args)
        except (NotFound, ValueError) as exc:
            return {"error": str(exc)}
        if mode == "auto":
            self.gate.record_auto(name, args, summary, source)
            return self._dispatch(name, args)
        req = self.gate.request(name, args, summary, lambda: self._dispatch(name, args), source=source)
        return {"needs_approval": True, "request_id": req.id, "asking": summary, "note": ASKING_NOTE}

    def run(self, name: str, args: Optional[dict] = None) -> dict:
        """A call the author made directly (a button in the app): no question needed."""
        args = dict(args or {})
        decl = _BY_NAME.get(name)
        error = _check(decl, name, args)
        if error:
            return {"error": error}
        return self._dispatch(name, args)

    def _dispatch(self, name: str, args: dict) -> dict:
        try:
            return getattr(self, f"_t_{name}")(**args)
        except NotFound as exc:
            return {"error": str(exc)}
        except Exception as exc:
            log.exception("tool %s failed", name)
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _pin(self, decl: dict, args: dict) -> dict:
        """Fix 'the open book' to a named book now, so a later yes runs exactly what was asked about."""
        if "book" in decl["parameters"].get("properties", {}):
            args["book"] = self._book(args.get("book")).title
        if decl["name"] == "change_setting":
            args["value"] = self._setting_value(args["key"], args["value"])
        if decl["name"] == "undo_last_change":  # a yes undoes the change it was asked about, nothing newer
            if not self.undo:
                raise NotFound("there is nothing to undo")
            args["expect"] = self.undo[-1]["id"]
        if decl["name"] == "set_voice":
            args.update(self._voice_change(args))
        if decl["name"] == "undo_upgrade" and not args.get("upgrade_id"):
            args["upgrade_id"] = self._upgrader().last_installed()["id"]
        return args

    def describe(self, name: str, args: dict) -> str:
        """The plain-words question the author is asked."""
        book = args.get("book", "")
        extra = (args.get("instructions") or "").strip()
        extra = f" ({truncate_words(extra, 25)})" if extra else ""
        if name == "create_book":
            return f"Start a new book: {truncate_words(args.get('idea', ''), 25)}"
        if name == "write_chapters":
            n = max(1, int(args.get("count") or 1))
            return f"Write the next {n} chapter{'s' if n > 1 else ''} of '{book}'{extra}"
        if name == "rewrite_chapter":
            return f"Rewrite chapter {args.get('chapter')} of '{book}'{extra}"
        if name == "update_story_bible":
            return f"Change the story bible of '{book}': {args.get('kind')} '{args.get('name')}'"
        if name == "change_outline":
            if not args.get("chapter"):
                return f"Add a new chapter to '{book}'" + (f": {args['title']}" if args.get("title") else "")
            to = f" to '{args['title']}'" if args.get("title") else ""
            return f"Change the title/outline of chapter {args.get('chapter')} of '{book}'{to}"
        if name == "export_book":
            return f"Export '{book}' as {args.get('format') or 'docx'}"
        if name == "decide_proposal":
            p = self.library.get_proposal(int(args["proposal_id"]))
            return f"{args['decision'].capitalize()} the idea '{p.title}'"
        if name == "publish_audiobook":
            chapters = args.get("chapters") or []
            which = f"chapters {', '.join(str(c) for c in chapters)}" if chapters else "all written chapters"
            return f"Post '{book}' ({which}) to YouTube as {args.get('privacy')}"
        if name == "change_setting":
            old = self._store().settings()[args["key"]]
            return f"Change the setting '{args['key']}' from '{old}' to '{args['value']}'"
        if name == "undo_last_change":
            return f"Undo the last change: {self.undo[-1]['summary']}"
        if name == "cast_voices":
            return f"Find the characters of '{book}' and give each one an audiobook voice"
        if name == "set_voice":
            bits = [describe_voice(args["voice"]) if args.get("voice") else "",
                    who(args["gender"], args["age"]).lower() if args.get("gender") and args.get("age") else "",
                    f"'{args['style']}'" if args.get("style") else ""]
            return f"Change the voice of {args['character']} in '{book}' to " + ", ".join(b for b in bits if b)
        if name == "build_upgrade":
            return ("Build an app upgrade in a separate test copy (the running app is not touched): "
                    + truncate_words(args.get("request", ""), 30))
        if name == "install_upgrade":
            up = self.library.get_upgrade(int(args["upgrade_id"]))
            if up["status"] != "ready":
                raise NotFound(f"upgrade #{up['id']} is {up['status']}, not ready to install")
            return f"Install app upgrade {describe_upgrade(up)} and restart the app"
        if name == "undo_upgrade":
            up = self.library.get_upgrade(int(args["upgrade_id"]))
            if up["status"] != "installed":
                raise NotFound(f"upgrade #{up['id']} is {up['status']}, not installed")
            return f"Undo app upgrade {describe_upgrade(up)} and restart the app"
        return f"{name.replace('_', ' ')} {args}"

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
        ch = self.library.get_chapter(b.id, chapter)
        before = (self.library.revisions(ch.id) or [None])[0]

        def revise(progress, cancel):
            number = self.writer.revise_chapter(b.id, chapter, instructions, "ai-revision").number
            if before is not None:
                self._push_undo(f"the rewrite of chapter {chapter} of '{b.title}'",
                                lambda: self.library.restore_revision(before.id))
            return number
        job = self.jobs.submit("revise", f"Revising {b.title} chapter {chapter}", revise, book_id=b.id)
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
        old = next((e for e in self.library.list_bible(b.id, (kind or "").strip().lower())
                    if e.name.lower() == (name or "").strip().lower()), None)
        e = self.library.upsert_bible(b.id, kind, name, summary)
        if old is not None:
            self._push_undo(f"the story bible entry {e.kind} '{e.name}' in '{b.title}'",
                            lambda: self.library.upsert_bible(b.id, old.kind, old.name, old.summary, old.data))
        else:
            self._push_undo(f"adding {e.kind} '{e.name}' to the story bible of '{b.title}'",
                            lambda: self.library.delete_bible_entry(e.id))
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
            new_id = ch.id
            self._push_undo(f"adding chapter {ch.number} to '{b.title}'", lambda: self.library.delete_chapter(new_id))
        else:
            ch = self.library.get_chapter(b.id, chapter)
            fields = {k: v for k, v in (("title", title), ("outline", outline)) if v}
            if fields:
                old = {"title": ch.title, "outline": ch.outline}
                ch_id, ch_title = ch.id, ch.title
                ch = self.library.update_chapter(ch.id, **fields)
                self._push_undo(f"chapter {ch.number} of '{b.title}' back to '{ch_title}'",
                                lambda: self.library.update_chapter(ch_id, **old))
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

    # ---- publishing -----------------------------------------------------------------
    def _pub(self):
        if self.publisher is None:
            raise NotFound("publishing is not available")
        return self.publisher

    def _t_publish_audiobook(self, privacy: str, book: str = "", chapters: Optional[list] = None, title: str = "",
                             description: str = "", per_chapter: bool = False) -> dict:
        b = self._book(book)
        posts = self._pub().publish_youtube(b.id, [int(c) for c in chapters or []] or None, title, description,
                                            privacy=privacy, per_chapter=per_chapter)
        return {"started": True, "posts": [{"id": p.id, "title": p.title, "privacy": p.privacy} for p in posts],
                "note": "Uploading in the background; the Publish tab shows progress and the link."}

    def _t_list_posts(self, book: str = "") -> dict:
        b = self._book(book)
        pub = self._pub()
        return {"posts": [{"id": p.id, "title": p.title, "privacy": p.privacy, "status": p.status, "url": p.url,
                           "progress": round(p.progress, 2), "error": p.error, "edited_since": pub.is_stale(p)}
                          for p in self.library.list_posts(b.id, limit=20)]}

    # ---- war room -------------------------------------------------------------------
    def _wr(self):
        if self.warroom is None:
            raise NotFound("the War Room is not available")
        return self.warroom

    def _t_war_briefing(self, book: str = "", focus: str = "") -> dict:
        return self._wr().briefing(self._book(book).id, focus)

    def _t_find_gaps(self, book: str = "", focus: str = "") -> dict:
        return {"gaps": self._wr().find_gaps(self._book(book).id, focus)}

    def _t_propose_idea(self, kind: str, title: str, content: str, target: str = "", rationale: str = "",
                        book: str = "") -> dict:
        p = self._wr().propose(self._book(book).id, kind, title, content, target, rationale, self.war_session_id)
        return {"proposal_id": p.id, "status": p.status, "note": "Shown to the author to accept, edit or reject."}

    def _t_list_proposals(self, book: str = "", status: str = "all") -> dict:
        props = self.library.list_proposals(self._book(book).id, None if status == "all" else status)
        return {"proposals": [{"id": p.id, "kind": p.kind, "target": p.target, "title": p.title,
                               "content": truncate_words(p.content, 120), "status": p.status} for p in props[:40]]}

    def _t_decide_proposal(self, proposal_id: int, decision: str, content: str = "") -> dict:
        wr = self._wr()
        p = {"accept": lambda: wr.accept(proposal_id, content or None), "reject": lambda: wr.reject(proposal_id),
             "undo": lambda: wr.undo(proposal_id)}[decision]()
        if decision == "accept":
            self._push_undo(f"accepting the idea '{p.title}'", lambda: wr.undo(proposal_id))
        return {"proposal_id": p.id, "status": p.status}

    # ---- app control ----------------------------------------------------------------
    def _push_undo(self, summary: str, fn: Callable[[], Any]) -> None:
        self.undo.append({"id": next(self._undo_ids), "summary": summary, "fn": fn})
        del self.undo[:-20]

    def _t_undo_last_change(self, expect: int = 0) -> dict:
        if not self.undo:
            raise NotFound("there is nothing to undo")
        if expect and self.undo[-1]["id"] != expect:
            raise NotFound("something else changed after you asked; ask again what to undo")
        entry = self.undo.pop()
        entry["fn"]()
        return {"undone": entry["summary"]}

    def _store(self):
        if self.settings_store is None:
            raise NotFound("settings are not available here")
        return self.settings_store

    def _ui(self, action: str, args: Optional[dict] = None) -> dict:
        if self.app_handler is None:
            raise NotFound("app controls only work while the app window is open")
        return self.app_handler(action, args or {})

    @staticmethod
    def _setting_value(key: str, value: Any) -> str:
        v = str(value).strip()
        if key in SETTING_CHOICES:
            choices = SETTING_CHOICES[key]
            exact = [c for c in choices if c.lower() == v.lower()]
            near = exact or [c for c in choices if v and v.lower() in c.lower()]
            if len(near) != 1:
                raise ValueError(f"{key} must be one of: {', '.join(choices)}")
            return near[0]
        if key in ("wake_word", "auto_mute"):
            if v.lower() in _ON:
                return "1"
            if v.lower() in _OFF:
                return "0"
            raise ValueError(f"{key} must be on or off")
        if key == "narration_speed":
            try:
                rate = float(v.lower().rstrip("x"))
            except ValueError:
                raise ValueError("narration_speed must be a number like 1.25") from None
            if not 0.5 <= rate <= 2.0:
                raise ValueError("narration_speed must be between 0.5 and 2.0")
            return str(round(rate, 2))
        if key == "author":
            if len(v) > 100:
                raise ValueError("author name is too long")
            return v
        raise ValueError(f"unknown setting {key}")

    def _t_get_settings(self) -> dict:
        return {"settings": self._store().settings()}

    def _t_change_setting(self, key: str, value: str) -> dict:
        store = self._store()
        new = self._setting_value(key, value)
        old = store.settings()[key]
        self._apply_setting(key, new)
        self._push_undo(f"setting '{key}' back to '{old}'", lambda: self._apply_setting(key, old))
        return {"saved": True, "key": key, "value": new, "was": old}

    def _apply_setting(self, key: str, value: str) -> None:
        self._store().set_setting(key, value)
        if self.app_handler is not None:
            self.app_handler("setting", {"key": key, "value": value})

    def _upgrader(self):
        if self.upgrader is None:
            raise NotFound("app upgrades are not available here")
        return self.upgrader

    def _t_build_upgrade(self, request: str) -> dict:
        up = self._upgrader().start(request)
        job = self.jobs.submit("upgrade", f"Building app upgrade #{up['id']}",
                               lambda p, c: self.upgrader.build(up["id"], p, c)["status"])
        return {"started": True, "upgrade_id": up["id"], "job_id": job.id,
                "next": "When the job is done, check list_upgrades. Only a ready upgrade can be installed, and "
                        "only after the author says yes."}

    def _t_list_upgrades(self) -> dict:
        return {"upgrades": [{"id": u["id"], "asked": u["request"], "status": u["status"], "summary": u["summary"],
                              "files": u["files"], "tests": u["test_output"].strip().splitlines()[-1:]}
                             for u in self.library.list_upgrades(10)]}

    def _t_install_upgrade(self, upgrade_id: int) -> dict:
        up = self._upgrader().install(upgrade_id)
        restarting = self.app_handler is not None and "error" not in self._ui("restart")
        return {"installed": up["id"], "files": up["files"], "restarting": restarting}

    def _t_undo_upgrade(self, upgrade_id: int = 0) -> dict:
        up = self._upgrader().undo(upgrade_id)
        restarting = self.app_handler is not None and "error" not in self._ui("restart")
        return {"undone": up["id"], "restarting": restarting}

    # ---- audiobook voices ---------------------------------------------------------------
    def _director(self):
        if self.director is None:
            raise NotFound("audiobook voices are not available")
        return self.director

    @staticmethod
    def _cast_view(m: dict) -> dict:
        role = "narrator" if m["name"] == NARRATOR else who(m["gender"], m["age"])
        return {"name": m["name"], "who": role, "voice": describe_voice(m["voice"]), "backup_voice": m["edge_voice"],
                "style": m["style"], "chosen_by_author": m["locked"], "description": m["description"]}

    def _t_voice_cast(self, book: str = "") -> dict:
        b = self._book(book)
        cast = self._director().cast(b.id)
        note = "" if len(cast) > 1 else "No character voices yet: use cast_voices to give each character a voice."
        return {"book": b.title, "cast": [self._cast_view(m) for m in cast], **({"note": note} if note else {})}

    def _t_cast_voices(self, book: str = "") -> dict:
        b = self._book(book)
        before = self.library.list_cast(b.id)
        cast = self._director().build_cast(b.id)
        self._push_undo(f"the audiobook voices of '{b.title}'", lambda: self.library.replace_cast(b.id, before))
        return {"book": b.title, "cast": [self._cast_view(m) for m in cast]}

    def _voice_change(self, args: dict) -> dict:
        """Check a voice change and fill in what follows from it (the matching backup voice, the age style)."""
        b = self._book(args.get("book"))
        d = self._director()
        name = args.get("character", "").strip()
        resolved = NARRATOR if name.lower() in ("narrator", "the narrator") else d._resolve(b.id, name)
        if resolved is None:
            names = ", ".join(m["name"] for m in d.characters(b.id)) or "none yet (use cast_voices)"
            raise NotFound(f"no character called '{name}' in '{b.title}'. Characters: {names}")
        out = {"character": resolved}
        if args.get("voice"):
            match = {v.lower(): v for v in GEMINI_CATALOG}.get(str(args["voice"]).strip().lower())
            if match is None:
                raise ValueError("voice must be one of " + ", ".join(GEMINI_CATALOG))
            out["voice"] = match
        if not any(args.get(k) for k in ("voice", "gender", "age", "style")):
            raise ValueError("say what to change: voice, gender, age or style")
        return out

    def _t_set_voice(self, character: str, book: str = "", voice: str = "", gender: str = "", age: str = "",
                     style: str = "") -> dict:
        b = self._book(book)
        d = self._director()
        current = d.narrator(b.id) if character == NARRATOR else self.library.get_cast_member(b.id, character)
        if current is None:
            raise NotFound(f"no character called '{character}' in '{b.title}'")
        before = self.library.list_cast(b.id)
        fields: dict = {"locked": True}
        g, a = gender or current["gender"], age or current["age"]
        if gender or age:
            fields.update(gender=g, age=a)
            if not style:
                fields["style"] = default_style(g, a)
        if style:
            fields["style"] = style.strip()[:80]
        if voice:
            fields["voice"] = voice
            fields["edge_voice"] = current["edge_voice"] if character == NARRATOR else edge_matching(voice, g, a)
        elif gender or age:
            fields.update(voice="", edge_voice="")
        self.library.upsert_cast_member(b.id, character, **fields)
        if not self.library.get_cast_member(b.id, character)["voice"]:
            d._assign(b.id)
        if character == NARRATOR and voice:
            self.library.upsert_cast_member(b.id, NARRATOR, edge_voice=current["edge_voice"])
        self._push_undo(f"the voice of {character} in '{b.title}'", lambda: self.library.replace_cast(b.id, before))
        return {"book": b.title, "changed": self._cast_view(self.library.get_cast_member(b.id, character))}

    def _t_app_status(self) -> dict:
        out: dict = {"books": len(self.library.list_books()), **self._t_job_status()}
        try:
            out["open_book"] = self._book("").title if self.current_book_id else None
        except NotFound:
            out["open_book"] = None
        if self.settings_store is not None:
            out["settings"] = self.settings_store.settings()
            capacity = getattr(self.settings_store, "capacity", None)
            if capacity is not None:
                out["storage"] = capacity.report().describe()
        if self.app_handler is not None:
            out.update(self._ui("status"))
        out["undo_available"] = self.undo[-1]["summary"] if self.undo else None
        return out

    def _t_open_tab(self, tab: str, book: str = "") -> dict:
        args: dict = {"tab": tab}
        if book:
            args["book_id"] = self._book(book).id
        return self._ui("tab", args)

    def _t_app_action(self, action: str, book: str = "") -> dict:
        args: dict = {"action": action}
        if action == "open_book_folder":
            args["book_id"] = self._book(book).id
        return self._ui("action", args)
