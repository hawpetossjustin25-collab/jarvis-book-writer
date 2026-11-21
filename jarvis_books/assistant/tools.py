"""Tools that Jarvis (text chat and live voice) can call to read and change the user's books.

The same declarations and dispatcher serve both the REST chat path and the Gemini Live voice session.
Long-running operations are started as background jobs and return immediately.
"""
from __future__ import annotations

import itertools
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from jarvis_books.assistant.permissions import ASKING_NOTE, PermissionGate
from jarvis_books.assistant.desktop_tools import DESKTOP_DECLARATIONS, DESKTOP_WORDS, DesktopTools
from jarvis_books.assistant.planner_tools import PLANNER_DECLARATIONS, PLANNER_WORDS, PlannerTools
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
from jarvis_books.upgrade import Stopped, describe as describe_upgrade
from jarvis_books.writer.pipeline import MAX_CHAPTERS, BookWriter, Cancelled

log = logging.getLogger(__name__)

# In the War Room canon changes go through proposals, so the direct-edit tools are withheld; looking at the
# computer and the web is fine there, changing it is done from the Jarvis tab.
COMPUTER_TOOLS = ("list_folder", "read_file", "write_file", "edit_file", "make_folder", "move_path", "delete_path",
                  "computer_log", "run_command", "command_output", "download_file", "github_download")
COMPUTER_CHANGES = ("write_file", "edit_file", "make_folder", "move_path", "delete_path", "run_command",
                    "download_file", "github_download", "close_program", "type_text", "press_keys", "click_screen")
WAR_ROOM_BLOCKED = ("update_story_bible", "change_outline", "send_api") + COMPUTER_CHANGES
COMPUTER_PINS = ("write_file", "edit_file", "make_folder", "move_path", "delete_path", "run_command",
                 "download_file", "github_download")
API_WORDS = (" api ", " api.", " api,", " api?", " apis", "api key", "endpoint", "json", "webhook", "{key:",
             "post to", "send to", "request to")
# Asking to find out about the world brings in the internet tools.
WEB_WORDS = ("internet", "online", "web", "search", "look up", "google", "website", "page", "http", "www", ".com",
             "news", "find", "research", "information", "info", "look into", "learn about", "tell me about",
             "what is", "what's", "what are", "who is", "who was", "who are", "when did", "when is", "where is",
             "where can", "how do", "how does", "how to", "how much", "how many", "latest", "current", "today",
             "price", "cost", "weather", "review", "facts", "source", "check if", "is it true")
S = {"type": "STRING"}
I = {"type": "INTEGER"}


def _decl(name: str, description: str, props: Optional[dict] = None, required: Optional[list[str]] = None) -> dict:
    params: dict = {"type": "OBJECT", "properties": props or {}}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


BOOK = {"type": "STRING", "description": "Book title (partial titles are fine). Omit for the open book."}
TABS = ("jarvis", "chat", "library", "write", "listen", "settings", "war room", "publish", "workshop", "planner")
SETTING_KEYS = ("voice", "edge_voice", "jarvis_voice", "author", "wake_word", "narration_speed", "auto_mute",
                "dialogue_tags", "memory", "brain", "brain_auto", "full_control")
SETTING_CHOICES = {"voice": GEMINI_VOICES, "jarvis_voice": GEMINI_VOICES, "edge_voice": EDGE_VOICES,
                   "brain": ("gemini", "local")}
APP_ACTIONS = ("mute_mic", "unmute_mic", "end_voice", "open_book_folder")
SPEEDS = (0.5, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)


def step_speed(rate: float, steps: int) -> float:
    """The next reading speed up (steps > 0) or down from `rate`, on the same steps as the Listen tab."""
    if steps > 0:
        return next((v for v in SPEEDS if v > rate + 1e-9), SPEEDS[-1])
    return next((v for v in reversed(SPEEDS) if v < rate - 1e-9), SPEEDS[0])
_ON = {"1", "on", "true", "yes", "enable", "enabled"}
_OFF = {"0", "off", "false", "no", "disable", "disabled"}

PATH = {"type": "STRING", "description": "A file or folder on the author's computer. Full paths like "
        "C:\\Users\\hawpe\\Documents\\notes.txt, or relative to the author's user folder; %VARS% and ~ work."}
COMPUTER_DECLARATIONS = [
    _decl("list_folder", "List what is in a folder on the author's computer (default: their user folder).",
          {"path": PATH}),
    _decl("read_file", "Read a text file on the author's computer, with line numbers. Long files come in parts: "
          "pass start_line to read further.", {"path": PATH, "start_line": I}, ["path"]),
    _decl("write_file", "Create a file or replace its whole content on the author's computer (or add to the end "
          "with mode=append). Backed up first so 'undo that' works. Needs the author's yes.",
          {"path": PATH, "content": S, "mode": {"type": "STRING", "enum": ["replace", "append"]}},
          ["path", "content"]),
    _decl("edit_file", "Change part of a text file: replace the exact text 'find' (copied from read_file, without "
          "line numbers) with 'replace'. It must match once unless all_matches is 'yes'. Backed up first. Needs "
          "the author's yes.", {"path": PATH, "find": S, "replace": S,
                                "all_matches": {"type": "STRING", "enum": ["yes", "no"]}},
          ["path", "find", "replace"]),
    _decl("make_folder", "Make a folder (and any missing parents). Needs the author's yes.", {"path": PATH}, ["path"]),
    _decl("move_path", "Move or rename a file or folder. Never overwrites. Needs the author's yes.",
          {"source": PATH, "destination": PATH}, ["source", "destination"]),
    _decl("delete_path", "Delete a file or folder. It is kept in Jarvis's backups so 'undo that' brings it back. "
          "Needs the author's yes.", {"path": PATH}, ["path"]),
    _decl("computer_log", "The latest changes Jarvis made to files on the computer and the commands he ran."),
    _decl("run_command", "Run a command in the author's terminal (PowerShell on Windows) and get its output and "
          "exit code. Use it for anything the terminal can do: install programs (winget), run code and tests, git, "
          "pip, Windows settings, files, networks, scheduled tasks. as_admin 'yes' runs it as administrator "
          "(Windows shows the author one allow box). Commands that could wipe a drive or break Windows are "
          "refused. If it is still running after a few seconds you get a job_id: use command_output.",
          {"command": S, "folder": {"type": "STRING", "description": "Folder to run it in (default: user folder)."},
           "timeout_seconds": {"type": "INTEGER", "description": "Stop it after this long (default 120, max 1800)."},
           "as_admin": {"type": "STRING", "enum": ["yes", "no"]}},
          ["command"]),
    _decl("command_output", "The output of a terminal command that was still running (by job_id).",
          {"job_id": S}, ["job_id"]),
    _decl("web_search", "Search the internet. Returns titles, links and snippets.", {"query": S}, ["query"]),
    _decl("read_web_page", "Read a web page (or text/JSON file) on the internet as plain text.", {"url": S}, ["url"]),
    _decl("research_web", "Find information on the internet about anything outside the author's books: searches, "
          "reads the best few pages and returns their text with links, so you can answer with facts and sources.",
          {"question": {"type": "STRING", "description": "what to find, in search words"},
           "pages": {"type": "INTEGER", "description": "how many pages to read, 1-8 (default 4)"}}, ["question"]),
    _decl("explore_website", "Dig through one website: opens the page and follows its links on the same site "
          "(those matching look_for first), returning each page's text.",
          {"url": S, "look_for": {"type": "STRING", "description": "what you're after, to pick which links to follow"},
           "pages": {"type": "INTEGER", "description": "how many pages to read, 1-10 (default 5)"}}, ["url"]),
    _decl("tax_lien_listings", "Current tax lien / tax sale property listings with prices, straight from the "
          "county offices: Atlanta (Fulton and DeKalb counties) and Phoenix (Maricopa County). Each listing has "
          "the parcel, address, owner, price (and whether it's exact or estimated), value, sale date and link.",
          {"area": {"type": "STRING", "description": "atlanta, fulton, dekalb, phoenix or maricopa"},
           "max_price": {"type": "NUMBER", "description": "highest price in dollars"},
           "min_price": {"type": "NUMBER", "description": "lowest price in dollars"},
           "city": {"type": "STRING", "description": "only this city or street (e.g. Phoenix, Mesa, Tolleson)"},
           "kind": {"type": "STRING", "enum": ["unsold", "delinquent"],
                    "description": "Phoenix only: unsold state-held liens you can buy now (default) or all "
                                   "parcels with delinquent taxes"},
           "limit": {"type": "INTEGER", "description": "how many to list, 1-200 (default 25)"},
           "sort": {"type": "STRING", "enum": ["price", "value"]}}, ["area"]),
    _decl("scrape_web_page", "Scrape a web page for its structure: title, links, headings, tables and list "
          "items, plus its plain text.", {"url": S}, ["url"]),
    _decl("call_api", "Get data from a web API (REST/JSON): weather, dictionaries, book databases like Open "
          "Library or Google Books, any online service. Put the query in the address. For a service that needs "
          "a key, write {key:NAME} where it goes (names from list_api_keys); the app puts the real key in. "
          "Never ask the author to tell you a key.",
          {"url": {"type": "STRING", "description": "full https address, with ?query=parameters"},
           "headers": {"type": "STRING", "description": "optional headers, one 'Name: value' per line, "
                                                        "e.g. Authorization: Bearer {key:openlibrary}"}},
          ["url"]),
    _decl("send_api", "Send to a web API: POST, PUT, PATCH or DELETE, to create, change or delete something on "
          "an online service. Needs the author's yes. Keys go in as {key:NAME}, like call_api.",
          {"url": S, "method": {"type": "STRING", "enum": ["POST", "PUT", "PATCH", "DELETE"]},
           "headers": {"type": "STRING", "description": "optional, one 'Name: value' per line"},
           "body": {"type": "STRING", "description": "what to send: JSON (sent as JSON) or plain text"}},
          ["url", "method"]),
    _decl("list_api_keys", "The names of the API keys the author saved for call_api and send_api (never the "
          "keys themselves)."),
    _decl("hf_search", "Search Hugging Face for models, datasets or spaces.",
          {"query": S, "kind": {"type": "STRING", "enum": ["models", "datasets", "spaces"]}}, ["query"]),
    _decl("hf_model_info", "A Hugging Face model's details: task, license, size of each file and its model card.",
          {"repo": {"type": "STRING", "description": "owner/name, e.g. LiquidAI/LFM3-1.7B"}}, ["repo"]),
    _decl("github_search", "Search GitHub repositories.", {"query": S}, ["query"]),
    _decl("github_read", "List a folder or read a file in a GitHub repository.",
          {"repo": {"type": "STRING", "description": "owner/name or a github.com link"}, "path": S, "ref": S},
          ["repo"]),
    _decl("download_file", "Download a file from the internet to the author's computer (default folder: "
          "Downloads). For a Hugging Face file use hf://owner/repo/path. Needs the author's yes.",
          {"url": S, "folder": PATH, "name": S}, ["url"]),
    _decl("github_download", "Download a whole GitHub repository into a folder (default: Downloads). Needs the "
          "author's yes.", {"repo": S, "folder": PATH, "ref": S}, ["repo"]),
]

DECLARATIONS = [
    _decl("list_books", "List all of the user's books with status and progress."),
    _decl("open_book", "Open a book and get its overview: logline, chapters, status and word counts.",
          {"book": BOOK}, ["book"]),
    _decl("create_book", "Start a new book from the user's story idea. Designs characters, world and a full "
          "chapter outline in the background.",
          {"idea": S, "title": S, "genre": S, "chapters": {"type": "INTEGER", "description": "0 = recommended; "
                                                                             "up to 8000 for a long serial"}},
          ["idea"]),
    _decl("write_chapters", "Write the next unwritten chapter(s) of a book in the background (plan, draft, "
          "edit, continuity check), one after another without stopping: up to 8000 in one run. Outlines ahead "
          "as it goes, waits out Gemini limits, and carries on after the app restarts.",
          {"book": BOOK, "count": {"type": "INTEGER", "description": "how many chapters (1-8000); 0 = keep going "
                                                                       "to the end of the book"},
           "instructions": {"type": "STRING", "description": "Author's direction"}}),
    _decl("set_book_length", "Make a book longer: set how many chapters it will have in total (up to 8000). "
          "The new chapters are outlined as the writing reaches them, following the story so far.",
          {"book": BOOK, "chapters": I}, ["chapters"]),
    _decl("stop_writing", "Stop a book's running chapter-writing run (finished chapters are kept).", {"book": BOOK}),
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
    _decl("remember", "Save something to your long-term memory when the author asks you to remember it (about "
          "them, how they like to work, a decision). scope: 'this book' for something about one book, else "
          "'all books'.", {"text": S, "scope": {"type": "STRING", "enum": ["all books", "this book"]}, "book": BOOK},
          ["text"]),
    _decl("recall_memories", "What you remember long-term about the author and their books, with ids. about: an "
          "optional topic to look for.", {"about": S}),
    _decl("forget_memory", "Delete one long-term memory by its id (from recall_memories or LONG-TERM MEMORY).",
          {"memory_id": I}, ["memory_id"]),
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
    _decl("reading_speed", "Make the audiobook (and Jarvis reading a book) faster or slower right away. change: "
          "'faster', 'slower', 'normal', or a speed like 1.5 (0.5 to 3).", {"change": S}, ["change"]),
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
          + "); edge_voice: a backup narrator; narration_speed: 0.5 to 3.0; dialogue_tags: how many times per "
          "chapter the narrator says 'said Tom' for each character before the voices alone show who is talking "
          "(all = every time); wake_word, auto_mute and memory (learn from conversations): on/off; "
          "author: the author's name.",
          {"key": {"type": "STRING", "enum": list(SETTING_KEYS)}, "value": S}, ["key", "value"]),
    _decl("app_action", "Press a button in the app: mute_mic, unmute_mic, end_voice (hang up this voice chat), "
          "open_book_folder (show the book's files on the computer).",
          {"action": {"type": "STRING", "enum": list(APP_ACTIONS)}, "book": BOOK}, ["action"]),
    _decl("build_upgrade", "Upgrade the app itself (new feature, fix, change in how it works). Gemini changes a "
          "separate test copy, never the running app, and every test runs on it in the background. Nothing is "
          "installed; when it is ready ask the author, then install_upgrade.", {"request": S}, ["request"]),
    _decl("list_upgrades", "App upgrades: what each changes, whether its tests passed, and which are installed."),
    _decl("test_app", "Run the app's own full test suite on the installed code. If any test fails, a repair "
          "upgrade starts by itself and keeps fixing the code until every test passes; when it is ready ask "
          "the author, then install_upgrade."),
    _decl("install_upgrade", "Install a tested app upgrade (status ready). The app restarts.",
          {"upgrade_id": I}, ["upgrade_id"]),
    _decl("undo_upgrade", "Put the app back the way it was before an installed upgrade (latest if omitted). The "
          "app restarts.", {"upgrade_id": I}),
    _decl("undo_last_change", "Undo the most recent change: a chapter rewrite, outline or title change, new "
          "chapter, story bible entry, accepted War Room idea, setting or voice."),
    _decl("brain_status", "Which brain Jarvis is thinking with: Gemini (online) or LFM3 (on this computer), "
          "which one comes first, whether the automatic switch is on, and how fast LFM3 runs here."),
    _decl("switch_brain", "Switch Jarvis's brain. use: 'gemini' (online, best writing) or 'lfm3' (runs on this "
          "computer, works offline, weaker at long writing). automatic: 'on' to switch by itself when one isn't "
          "working, 'off' to never switch.", {"use": {"type": "STRING", "enum": ["gemini", "lfm3"]},
                                              "automatic": {"type": "STRING", "enum": ["on", "off"]}}),
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

DECLARATIONS += COMPUTER_DECLARATIONS + PLANNER_DECLARATIONS + DESKTOP_DECLARATIONS
_BY_NAME = {d["name"]: d for d in DECLARATIONS}


def _check(decl: Optional[dict], name: str, args: dict) -> str:
    if decl is None:
        return f"unknown tool {name}"
    unknown = sorted(set(args) - set(decl["parameters"].get("properties", {})))
    errors = validate(args, decl["parameters"]) + [f"unknown argument {k!r}" for k in unknown]
    return "invalid arguments: " + "; ".join(errors) if errors else ""


class ToolBox(PlannerTools, DesktopTools):
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
        self.memory = None  # set by the Studio (Jarvis's long-term memory)
        self.computer = None  # set by the Studio (files on the author's computer)
        self.terminal = None  # set by the Studio
        self.web = None  # set by the Studio
        self.api_keys = None
        self.undo = undo if undo is not None else []  # shared by every Jarvis, newest last
        self._undo_ids = itertools.count(1)

    def declarations(self, names: Optional[list[str]] = None) -> list[dict]:
        blocked = WAR_ROOM_BLOCKED if self.war_session_id is not None else ()
        return [d for d in DECLARATIONS if (names is None or d["name"] in names) and d["name"] not in blocked]

    def select(self, user_text: str) -> list[dict]:
        """A short tool list for the small local model, which can't read all of them at once: the core tools,
        plus the rest when the request needs them. Gemini always gets every tool (declarations())."""
        core = ["list_books", "open_book", "read_chapter", "search_books", "add_note", "write_chapters",
                "create_book", "job_status", "set_book_length", "stop_writing", "research_web", "run_command",
                "open_item"]
        t = f" {(user_text or '').lower()} "  # padded so whole-word keywords match at either end
        extra = {
            "rewrite_chapter": ("rewrite", "revise", "change", "fix", "edit", "redo"),
            "story_bible": ("character", "bible", "who is", "place", "world", "thread"),
            "update_story_bible": ("character", "bible", "add", "rename", "change"),
            "change_outline": ("outline", "chapter", "plot", "add a chapter"),
            "writing_advice": ("how long", "advice", "genre", "length", "structure", "format"),
            "cancel_job": ("stop", "cancel"),
            "play_audiobook": ("play", "listen", "audio", "read me", "narrat"),
            "stop_audiobook": ("stop", "pause", "audio"),
            "remember": ("remember", "don't forget", "keep in mind", "memor"),
            "recall_memories": ("remember", "memor", "know about me", "forget", "recall"),
            "forget_memory": ("forget", "memor"),
            "reading_speed": ("faster", "slower", "speed", "slow down", "speed up", "quicker", "too fast",
                              "too slow", "normal speed"),
            "export_book": ("export", "docx", "epub", "word", "file", "mp3", "m4b", "audiobook"),
            "war_briefing": ("brief", "fill me in", "catch me up", "where are we", "war room", "status"),
            "find_gaps": ("gap", "undecided", "missing", "hole", "not decided", "what if", "unexplained"),
            "propose_idea": ("idea", "what if", "suggest", "pitch", "propose", "should"),
            "list_proposals": ("proposal", "decision", "idea", "decided", "pending"),
            "decide_proposal": ("accept", "reject", "undo", "yes", "approve", "keep", "go with"),
            "publish_audiobook": ("youtube", "post", "upload", "publish", "unlisted"),
            "list_posts": ("youtube", "post", "upload", "publish", "link", "views"),
            "app_status": ("status", "what's playing", "doing", "app", "storage", "space", "running"),
            "open_tab": ("open", "go to", "show", "tab", "switch", "take me", "workshop", "editor", "planner"),
            "get_settings": ("setting", "voice", "speed", "wake", "mute", "author name"),
            "change_setting": ("setting", "voice", "speed", "faster", "slower", "wake", "auto-mute", "auto mute",
                               "author name", "my name", "turn on", "turn off"),
            "app_action": ("mute", "unmute", "hang up", "end the call", "goodbye", "bye", "folder", "files"),
            "undo_last_change": ("undo", "take that back", "revert", "put it back", "change it back"),
            "build_upgrade": ("upgrade", "feature", "improve the app", "change the app", "update the app",
                              "add a button", "add an option", "code", "fix the app", "bug"),
            "list_upgrades": ("upgrade", "feature", "install", "tests pass", "code"),
            "test_app": ("test yourself", "run your tests", "test suite", "self test", "self-test",
                         "check yourself", "tests pass", "fix yourself", "repair yourself"),
            "install_upgrade": ("upgrade", "install"),
            "undo_upgrade": ("upgrade", "undo", "put the app back", "old version"),
            "brain_status": ("brain", "gemini", "lfm", "local model", "offline", "which model", "switch"),
            "switch_brain": ("brain", "gemini", "lfm", "local model", "offline", "switch", "use the"),
            "voice_cast": ("voice", "cast", "narrator", "sound", "who reads"),
            "list_folder": ("file", "folder", "document", "desktop", "downloads", "my computer", "on my pc", "laptop", "code", ".py", ".txt", "save it to", "directory"),
            "read_file": ("file", "folder", "document", "desktop", "downloads", "my computer", "on my pc", "laptop", "code", ".py", ".txt", "save it to", "directory"),
            "write_file": ("file", "folder", "document", "desktop", "downloads", "my computer", "on my pc", "laptop", "code", ".py", ".txt", "save it to", "directory"),
            "edit_file": ("file", "folder", "document", "desktop", "downloads", "my computer", "on my pc", "laptop", "code", ".py", ".txt", "save it to", "directory"),
            "make_folder": ("folder", "directory", "make a", "create a"),
            "move_path": ("move", "rename", "file", "folder"),
            "delete_path": ("delete", "remove", "trash", "get rid of", "clean up"),
            "computer_log": ("what did you change", "what did you run", "log", "history", "undo"),
            "run_command": ("terminal", "command", "powershell", "cmd", "run ", "install", "winget", "pip", "git ", "script", "program", "test", "code", "my computer", "laptop"),
            "command_output": ("output", "result", "finished", "done", "command", "terminal", "install"),
            "web_search": WEB_WORDS,
            "research_web": WEB_WORDS,
            "tax_lien_listings": ("tax lien", "tax-lien", "taxlien", "tax sale", "tax deed", "lien", "delinquent",
                                  "foreclos", "sheriff sale", "levy sale"),
            "read_web_page": WEB_WORDS + ("read", "link", "article"),
            "explore_website": WEB_WORDS + ("site", "explore", "dig", "more pages", "follow", "link"),
            "scrape_web_page": WEB_WORDS + ("scrape", "get data", "collect", "extract", "grab data", "table", "links", "list"),
            "call_api": API_WORDS,
            "send_api": API_WORDS,
            "list_api_keys": API_WORDS,
            "hf_search": ("hugging", "huggingface", "model", "hf://", "gguf", "dataset"),
            "hf_model_info": ("hugging", "huggingface", "model", "hf://", "gguf", "dataset"),
            "github_search": ("github", "repo", "repository", "git ", "source code"),
            "github_read": ("github", "repo", "repository", "git ", "source code"),
            "download_file": ("download", "get the file", "save the file", "grab", "hugging", "model"),
            "github_download": ("github", "repo", "clone", "download"),
            "cast_voices": ("voice", "cast", "characters sound", "give them voices"),
            "set_voice": ("voice", "narrator", "sound", "deeper", "higher", "older", "younger", "accent"),
            **{d["name"]: PLANNER_WORDS for d in PLANNER_DECLARATIONS},
            **{d["name"]: DESKTOP_WORDS for d in DESKTOP_DECLARATIONS},
        }
        if self.war_session_id is not None:  # in the War Room every War Room tool is always available
            core = core + ["war_briefing", "find_gaps", "propose_idea", "list_proposals", "decide_proposal",
                           "story_bible"]
        # what the request matched comes first: LFM3 drops tools from the end when its context is small
        names = list(dict.fromkeys([n for n, words in extra.items() if any(w in t for w in words)] + core))
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
        if decl["name"] == "forget_memory":
            self.library.get_memory(args["memory_id"])
        if decl["name"] in COMPUTER_PINS:
            args.update(self._pin_computer(decl["name"], args))
        if decl["name"] == "send_api":  # a bad address or missing key is said now, not after the yes
            from jarvis_books.computer.web import api_headers
            api_headers(args.get("headers", ""))
            filled = self.api_keys.fill(args["url"]) if self.api_keys is not None else args["url"]
            self._web().check(filled)
        if decl["name"] == "undo_upgrade" and not args.get("upgrade_id"):
            args["upgrade_id"] = self._upgrader().last_installed()["id"]
        return args

    def describe(self, name: str, args: dict) -> str:
        """The plain-words question the author is asked."""
        book = args.get("book", "")
        extra = (args.get("instructions") or "").strip()
        extra = f" ({truncate_words(extra, 25)})" if extra else ""
        if name in COMPUTER_PINS:
            return self._describe_computer(name, args)
        desk = self._describe_desktop(name, args)
        if desk:
            return desk
        planned = self.describe_planner(name, args)
        if planned:
            return planned
        if name == "send_api":
            body = (args.get("body") or "").strip()
            sent = f" with: {body[:400]}{'...' if len(body) > 400 else ''}" if body else ""
            return f"Send a {args['method']} request to {args['url']}{sent} (saved keys stay hidden)"
        if name == "forget_memory":
            text = self.library.get_memory(args["memory_id"])["text"]
            return f"Forget this memory: '{truncate_words(text, 30)}'"
        if name == "create_book":
            return f"Start a new book: {truncate_words(args.get('idea', ''), 25)}"
        if name == "write_chapters":
            n = int(args.get("count") if args.get("count") is not None else 1)
            if n <= 0:
                return f"Keep writing '{book}' chapter after chapter until it is finished{extra}"
            return f"Write the next {n:,} chapter{'s' if n > 1 else ''} of '{book}'{extra}"
        if name == "set_book_length":
            return f"Make '{book}' {int(args.get('chapters') or 0):,} chapters long"
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
        if name == "test_app":
            return ("Run the app's full test suite, and if anything fails, build a repair upgrade in a separate "
                    "test copy that keeps fixing it until every test passes")
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
        left = self.writer.remaining(b.id)
        if left == 0:
            return {"error": f"every chapter of {b.title} is already written; set_book_length makes it longer"}
        running = self._writing_job(b.id)
        if running is not None:
            return {"error": f"{b.title} is already being written ({running.message})", "job_id": running.id}
        count = left if int(count) <= 0 else min(int(count), MAX_CHAPTERS)
        nxt = self.writer.next_unwritten(b.id)
        first = nxt.number if nxt else self.library.chapter_count(b.id) + 1
        job = self._start_writing(b.id, first, count, instructions)
        out = {"started": True, "job_id": job.id, "first_chapter": first, "count": count,
               "chapters_left_in_book": left}
        if count > left:
            out["note"] = (f"the book has only {left} chapter(s) left to write; it stops at the end. "
                           "Use set_book_length to make it longer.")
        if count > 1:
            out["continuous"] = ("runs chapter after chapter in the background; it waits out Gemini limits and "
                                 "carries on if the app restarts")
        return out

    def _writing_job(self, book_id: str):
        return next((j for j in self.jobs.active() if j.kind == "write" and j.book_id == book_id), None)

    def _start_writing(self, book_id: str, first: int, count: int, instructions: str):
        """A writing run is saved with the book, so it picks up where it was if the app closes."""
        until = first + count - 1
        self.library.update_book(book_id, settings={"writing_run": {"until": until, "instructions": instructions}})
        title = self.library.get_book(book_id).title

        def run(progress, cancel):
            try:
                done = self.writer.write_next_chapters(book_id, count, instructions, progress=progress, cancel=cancel)
            except Cancelled:
                if not self.jobs.closing:  # the author stopped it; an app restart resumes it
                    self.library.update_book(book_id, settings={"writing_run": None})
                raise
            except Exception:
                self.library.update_book(book_id, settings={"writing_run": None})
                raise
            self.library.update_book(book_id, settings={"writing_run": None})
            return [ch.number for ch in done]

        label = f"chapter {first}" if count == 1 else f"chapters {first:,}-{until:,}"
        return self.jobs.submit("write", f"Writing {title}: {label}", run, book_id=book_id)

    def resume_writing(self) -> list[str]:
        """Restart the writing runs that were going when the app closed. Returns their job ids."""
        started = []
        for b in self.library.list_books():
            run = b.settings.get("writing_run") or {}
            if not run or self._writing_job(b.id) is not None:
                continue
            nxt = self.writer.next_unwritten(b.id)
            first = nxt.number if nxt else self.library.chapter_count(b.id) + 1
            count = min(int(run.get("until") or 0) - first + 1, self.writer.remaining(b.id))
            if count <= 0:
                self.library.update_book(b.id, settings={"writing_run": None})
                continue
            started.append(self._start_writing(b.id, first, count, run.get("instructions") or "").id)
        return started

    def _t_set_book_length(self, chapters: int, book: str = "") -> dict:
        b = self._book(book)
        try:
            total = self.writer.set_length(b.id, chapters)
        except ValueError as exc:
            return {"error": str(exc)}
        return {"book": b.title, "chapters": total, "outlined": self.library.chapter_count(b.id),
                "left_to_write": self.writer.remaining(b.id)}

    def _t_stop_writing(self, book: str = "") -> dict:
        b = self._book(book)
        job = self._writing_job(b.id)
        if job is None:
            return {"error": f"{b.title} isn't being written right now"}
        self.jobs.cancel(job.id)
        return {"stopping": True, "job_id": job.id, "note": "it stops after the current step; finished chapters stay"}

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

    def _memory(self):
        if self.memory is None:
            raise NotFound("long-term memory is not available here")
        return self.memory

    def _t_remember(self, text: str, scope: str = "", book: str = "") -> dict:
        memory = self._memory()
        book_id = None
        if scope == "this book" or book:
            try:
                book_id = self._book(book).id
            except NotFound:
                if book:
                    raise
        m = memory.remember(text, book_id)
        if m["new"]:
            self._push_undo(f"remembering '{truncate_words(m['text'], 10)}'", lambda: memory.forget(m["id"]))
        return {"remembered": m["text"], "memory_id": m["id"], "scope": m["scope"]}

    def _t_recall_memories(self, about: str = "") -> dict:
        memory = self._memory()
        found = memory.recall(about, None, k=25) if about.strip() else memory.all()[:40]
        out = []
        for m in found:
            item = {"id": m["id"], "text": m["text"], "scope": m["scope"], "pinned": m["pinned"], "kind": m["kind"]}
            if m["kind"] == "lesson":
                item["because"] = [e["text"] for e in memory.explain(m["id"])["evidence"][:6]]
            out.append(item)
        return {"memories": out, "total": len(self.library.list_memories())}

    def _t_forget_memory(self, memory_id: int) -> dict:
        memory = self._memory()
        row = memory.forget(memory_id)
        self._push_undo(f"forgetting '{truncate_words(row['text'], 10)}'", lambda: memory.restore(row))
        return {"forgot": row["text"]}

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
        if key in ("wake_word", "auto_mute", "memory", "brain_auto", "full_control"):
            if v.lower() in _ON:
                return "1"
            if v.lower() in _OFF:
                return "0"
            raise ValueError(f"{key} must be on or off")
        if key == "dialogue_tags":
            if v.lower() in ("all", "always", "every time", "unlimited"):
                return "99"
            if not v.isdigit() or int(v) > 99:
                raise ValueError("dialogue_tags must be a number from 0 to 99, or 'all'")
            return str(int(v))
        if key == "narration_speed":
            try:
                rate = float(v.lower().rstrip("x"))
            except ValueError:
                raise ValueError("narration_speed must be a number like 1.25") from None
            if not 0.5 <= rate <= 3.0:
                raise ValueError("narration_speed must be between 0.5 and 3.0")
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

    def _t_reading_speed(self, change: str) -> dict:
        store = self._store()
        old = store.settings()["narration_speed"]
        rate, c = float(old), str(change).strip().lower()
        if c in ("faster", "speed up", "up", "quicker", "a bit faster"):
            target = step_speed(rate, 1)
        elif c in ("slower", "slow down", "down", "a bit slower"):
            target = step_speed(rate, -1)
        elif c in ("normal", "regular", "reset", "default", "normal speed"):
            target = 1.0
        else:
            target = float(self._setting_value("narration_speed", change))
        new = self._setting_value("narration_speed", str(target))
        if float(new) == rate:
            limit = "fastest" if rate >= SPEEDS[-1] else "slowest" if rate <= SPEEDS[0] else "current"
            return {"saved": False, "speed": f"{rate:g}x", "note": f"already at the {limit} speed"}
        self._apply_setting("narration_speed", new)
        self._push_undo(f"reading speed back to {float(old):g}x", lambda: self._apply_setting("narration_speed", old))
        return {"saved": True, "speed": f"{float(new):g}x", "was": f"{rate:g}x"}

    def _apply_setting(self, key: str, value: str) -> None:
        self._store().set_setting(key, value)
        if self.app_handler is not None:
            self.app_handler("setting", {"key": key, "value": value})

    def _upgrader(self):
        if self.upgrader is None:
            raise NotFound("app upgrades are not available here")
        return self.upgrader

    def _start_upgrade(self, upgrade_id: int):
        def build(progress, cancel):
            try:
                return self.upgrader.build(upgrade_id, progress, cancel)["status"]
            except Stopped as exc:
                raise Cancelled(str(exc)) from exc
        return self.jobs.submit("upgrade", f"Building app upgrade #{upgrade_id}", build)

    def _t_build_upgrade(self, request: str) -> dict:
        up = self._upgrader().start(request)
        job = self._start_upgrade(up["id"])
        return {"started": True, "upgrade_id": up["id"], "job_id": job.id,
                "keeps_going": "It keeps fixing failing tests until they all pass, or until the author says stop "
                               "(cancel_job).",
                "next": "When the job is done, check list_upgrades. Only a ready upgrade can be installed, and "
                        "only after the author says yes."}

    def resume_upgrades(self) -> list[str]:
        """Carry on with the upgrade builds that were going when the app closed. Returns their job ids."""
        if self.upgrader is None:
            return []
        running = {j.title for j in self.jobs.active() if j.kind == "upgrade"}
        return [self._start_upgrade(u["id"]).id for u in self.upgrader.unfinished()
                if f"Building app upgrade #{u['id']}" not in running]

    def _t_test_app(self) -> dict:
        self._upgrader()
        job = self.jobs.submit("selftest", "Testing the whole app", self._self_test)
        return {"started": True, "job_id": job.id,
                "next": "If any test fails, a repair upgrade starts by itself; check job_status and list_upgrades."}

    def _self_test(self, progress, cancel) -> str:
        progress(0.1, "Running every test on the app")
        ok, output = self.upgrader.check()
        if ok:
            return "every test passes"
        if cancel.is_set():
            return "tests failed; stopped before repairing"
        fix = self.upgrader.start("The app's own test suite fails. Fix the app's code so every test passes again, "
                                  "without weakening or deleting tests. Test output:\n" + output[-3000:])
        self._start_upgrade(fix["id"])
        return f"tests failed; repair upgrade #{fix['id']} started and keeps going until every test passes"

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

    def _brain(self):
        brain = getattr(self.settings_store, "brain", None)
        if brain is None:
            raise NotFound("the brain switch is not available here")
        return brain

    def _t_brain_status(self) -> dict:
        return self._brain().status()

    def _t_switch_brain(self, use: str = "", automatic: str = "") -> dict:
        store = self._store()
        old = store.settings()
        changes = {}
        if use:
            changes["brain"] = "local" if use.lower() in ("lfm3", "local", "lfm") else "gemini"
        if automatic:
            changes["brain_auto"] = "1" if automatic.lower() in _ON else "0"
        if not changes:
            return {"error": "say which brain to use or whether to switch automatically"}
        for key, value in changes.items():
            store.set_setting(key, value)

        def back():
            for key in changes:
                store.set_setting(key, old[key])
        self._push_undo("brain back to " + ("LFM3" if old["brain"] == "local" else "Gemini"), back)
        out = {"saved": True, **self._brain().status()}
        brain = self._brain()
        if changes.get("brain") == "gemini" and out["gemini"].startswith("resting"):
            out["warning"] = f"Gemini failed a moment ago ({out['gemini'][9:][:150]}); LFM3 answers until it works again"

        if changes.get("brain") == "local" and not brain.local.available and not brain.local.initialize():
            out["warning"] = brain.local.problem + "; Gemini answers until it is ready"
        return out

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
        out.update(self.planner_status())
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


    # ---- the author's computer: files, terminal, internet ----------------------------------------------
    def _computer(self):
        if self.computer is None:
            raise NotFound("Jarvis can't touch files from here")
        return self.computer

    def _terminal(self):
        if self.terminal is None:
            raise NotFound("the terminal isn't available here")
        return self.terminal

    def _web(self):
        if self.web is None:
            raise NotFound("the internet isn't available here")
        return self.web

    def _download_folder(self, folder: str) -> str:
        pc = self._computer()
        target = pc.resolve(folder) if folder else pc.home / "Downloads"
        why = pc.protected(target)
        if why:
            raise NotFound(f"{target} is {why}; pick another folder")
        return str(target)

    def _pin_computer(self, name: str, args: dict) -> dict:
        """Turn every path into a full path now, so the yes runs exactly what was shown."""
        if name == "run_command":
            from jarvis_books.computer.terminal import assess
            blocked, _ = assess(args["command"])
            if blocked:
                raise ValueError(f"Jarvis won't run that command: it {blocked}")
            return {"folder": str(self._terminal().folder(args.get("folder", "")))}
        if name in ("download_file", "github_download"):
            return {"folder": self._download_folder(args.get("folder", ""))}
        pc = self._computer()
        if name == "move_path":
            return {"source": str(pc.resolve(args["source"])), "destination": str(pc.resolve(args["destination"]))}
        return {"path": str(pc.resolve(args["path"]))}

    def _describe_computer(self, name: str, args: dict) -> str:
        pc = self.computer
        if name == "write_file":
            plan = pc.plan_write(args["path"], args["content"], args.get("mode") == "append")
            verb = "Add to" if args.get("mode") == "append" else ("Replace" if plan["old"] is not None else "Create")
            lines = plan["new"].count("\n") + 1
            return f"{verb} the file {plan['path']} ({lines} lines; a backup is kept)" + _diff_tail(plan["diff"])
        if name == "edit_file":
            plan = pc.plan_edit(args["path"], args["find"], args["replace"], args.get("all_matches") == "yes")
            return f"Edit the file {plan['path']} (a backup is kept)" + _diff_tail(plan["diff"])
        if name == "make_folder":
            pc._changeable(args["path"])
            return f"Make the folder {args['path']}"
        if name == "move_path":
            pc._changeable(args["source"]), pc._changeable(args["destination"])
            return f"Move {args['source']} to {args['destination']}"
        if name == "delete_path":
            target = pc._changeable(args["path"])
            if not target.exists():
                raise NotFound(f"nothing at {target}")
            kind = "folder" if target.is_dir() else "file"
            return f"Delete the {kind} {target} (kept in Jarvis's backups, so it can be undone)"
        if name == "run_command":
            from jarvis_books.computer.terminal import assess
            _, warnings = assess(args["command"])
            warn = "".join(f" WARNING: this {w}." for w in warnings)
            admin = " AS ADMINISTRATOR" if args.get("as_admin") == "yes" else ""
            return f"Run{admin} in {self._terminal().shell} (in {args['folder']}): {args['command']}" + warn
        if name == "download_file":
            from jarvis_books.computer.web import hf_url
            return f"Download {hf_url(args['url'])} into {args['folder']}"
        return f"Download the GitHub repository {args['repo']} into {args['folder']}"

    def _t_list_folder(self, path: str = "") -> dict:
        return self._computer().list_folder(path)

    def _t_read_file(self, path: str, start_line: int = 1) -> dict:
        return self._computer().read_file(path, start_line)

    def _t_write_file(self, path: str, content: str, mode: str = "replace") -> dict:
        out = self._computer().write_file(path, content, append=mode == "append")
        self._push_undo(f"writing {out['written']}", lambda: self.computer.restore(out["backup"], out["written"]))
        return out

    def _t_edit_file(self, path: str, find: str, replace: str, all_matches: str = "no") -> dict:
        out = self._computer().edit_file(path, find, replace, all_matches == "yes")
        self._push_undo(f"editing {out['written']}", lambda: self.computer.restore(out["backup"], out["written"]))
        return out

    def _t_make_folder(self, path: str) -> dict:
        out = self._computer().make_folder(path)
        if not out["existed"]:
            self._push_undo(f"making the folder {out['folder']}",
                            lambda: self.computer.restore(None, out["folder"]))
        return out

    def _t_move_path(self, source: str, destination: str) -> dict:
        out = self._computer().move(source, destination)
        self._push_undo(f"moving {out['moved']} to {out['to']}",
                        lambda: self.computer.move(out["to"], out["moved"]))
        return out

    def _t_delete_path(self, path: str) -> dict:
        out = self._computer().delete(path)
        self._push_undo(f"deleting {out['deleted']}", lambda: self.computer.restore(out["backup"], out["deleted"]))
        return out

    def _t_computer_log(self) -> dict:
        out = {"files": self._computer().recent(15)}
        term = self.terminal
        if term is not None and term.log_path.exists():
            import json as _json
            out["commands"] = [_json.loads(x) for x in term.log_path.read_text(encoding="utf-8").splitlines()[-15:]
                               if x.strip()]
        return out

    COMMAND_WAIT_S = 3.0

    @staticmethod
    def _describe_desktop(name: str, args: dict) -> str:
        if name == "close_program":
            how = " right away (unsaved work is lost)" if args.get("force") == "yes" else ""
            return f"Close {args['name']}{how}"
        if name == "type_text":
            return f"Type into the window in front: {truncate_words(args['text'], 40)}"
        if name == "press_keys":
            return f"Press {args['keys']} in the window in front"
        if name == "click_screen":
            what = "Double-click" if (args.get("clicks") or 1) == 2 else (
                "Right-click" if args.get("button") == "right" else "Click")
            return f"{what} the screen at {args['x']}, {args['y']}"
        return ""

    def _t_run_command(self, command: str, folder: str = "", timeout_seconds: int = 120,
                       as_admin: str = "no") -> dict:
        term = self._terminal()
        admin = as_admin == "yes"
        job = self.jobs.submit("command", f"Running: {truncate_words(command, 12)}",
                               lambda progress, cancel: term.run(command, folder, timeout_seconds, cancel,
                                                                 as_admin=admin))
        deadline = time.monotonic() + self.COMMAND_WAIT_S
        while time.monotonic() < deadline and job.status in ("queued", "running"):
            time.sleep(0.05)
        if job.status in ("queued", "running"):
            return {"running": True, "job_id": job.id,
                    "note": "Still running in the background; use command_output with this job_id for the result."}
        return self._t_command_output(job.id)

    def _t_command_output(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None or job.kind != "command":
            raise NotFound("no terminal command with that job id")
        if job.status in ("queued", "running"):
            return {"running": True, "job_id": job.id, "title": job.title}
        if job.status == "failed":
            return {"error": job.error or "the command could not start", "job_id": job.id}
        if job.status == "cancelled" or job.result is None:
            return {"cancelled": True, "job_id": job.id}
        return {"job_id": job.id, **job.result}

    def _t_web_search(self, query: str) -> dict:
        return self._web().search(query)

    def _t_read_web_page(self, url: str) -> dict:
        return self._web().read_page(url)

    def _t_scrape_web_page(self, url: str) -> dict:
        return self._web().scrape(url)

    def _t_call_api(self, url: str, headers: str = "") -> dict:
        return self._web().call_api(url, "GET", headers, "", self.api_keys)

    def _t_send_api(self, url: str, method: str, headers: str = "", body: str = "") -> dict:
        return self._web().call_api(url, method, headers, body, self.api_keys)

    def _t_list_api_keys(self) -> dict:
        names = self.api_keys.names() if self.api_keys is not None else []
        out: dict = {"names": names, "use": "write {key:NAME} in the address or a header"}
        if not names:
            out["note"] = "no API keys are saved; the author can add one in Settings > API keys"
        return out

    def _liens(self):
        if getattr(self, "tax_liens", None) is None:
            from jarvis_books.computer.taxliens import TaxLiens, gemini_pdf_reader
            self.tax_liens = TaxLiens(self._web(), gemini_pdf_reader(self.vision) if self.vision else None)
        return self.tax_liens

    def _t_tax_lien_listings(self, area: str, max_price: Optional[float] = None, min_price: Optional[float] = None,
                             city: str = "", kind: str = "unsold", limit: int = 25, sort: str = "price") -> dict:
        return self._liens().listings(area, max_price, min_price, city, kind or "unsold", limit, sort or "price")

    def _t_research_web(self, question: str, pages: int = 4) -> dict:
        return self._web().research(question, pages)

    def _t_explore_website(self, url: str, look_for: str = "", pages: int = 5) -> dict:
        return self._web().explore_site(url, look_for, pages)

    def _t_hf_search(self, query: str, kind: str = "models") -> dict:
        return self._web().hf_search(query, kind)

    def _t_hf_model_info(self, repo: str) -> dict:
        return self._web().hf_model(repo)

    def _t_github_search(self, query: str) -> dict:
        return self._web().github_search(query)

    def _t_github_read(self, repo: str, path: str = "", ref: str = "") -> dict:
        return self._web().github_read(repo, path, ref)

    def _t_download_file(self, url: str, folder: str = "", name: str = "") -> dict:
        web, target = self._web(), self._download_folder(folder)
        job = self.jobs.submit("download", f"Downloading {truncate_words(url, 8)}",
                               lambda progress, cancel: web.download(url, Path(target), name, progress, cancel.is_set))
        return {"started": True, "job_id": job.id, "folder": target,
                "note": "Downloading in the background; job_status shows progress."}

    def _t_github_download(self, repo: str, folder: str = "", ref: str = "") -> dict:
        web, target = self._web(), self._download_folder(folder)
        job = self.jobs.submit("download", f"Downloading {repo} from GitHub",
                               lambda progress, cancel: web.github_download(repo, Path(target), ref,
                                                                            progress=progress, cancel=cancel.is_set))
        return {"started": True, "job_id": job.id, "folder": target,
                "note": "Downloading in the background; job_status shows progress."}


def _diff_tail(diff_text: str, max_lines: int = 14) -> str:
    lines = diff_text.splitlines()[2:]  # skip the --- / +++ header
    if not lines:
        return ""
    more = f"\n... {len(lines) - max_lines} more lines" if len(lines) > max_lines else ""
    return "\n" + "\n".join(lines[:max_lines]) + more
