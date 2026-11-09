"""The book library: every read and write of books, chapters, story bible, notes and chat."""
from __future__ import annotations

import difflib
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Iterable

from jarvis_books.errors import NotFound, ValidationError
from jarvis_books.storage.db import Database
from jarvis_books.storage.models import (
    BIBLE_KINDS, BOOK_STATUSES, CHAPTER_STATUSES, POST_STATUSES, PROPOSAL_KINDS, PROPOSAL_STATUSES, BibleEntry, Book,
    Chapter, Message, Note, Playback, Post, Proposal, Revision, WarSession,
)
from jarvis_books.text import word_count

log = logging.getLogger(__name__)

BOOK_FIELDS = {
    "title", "genre", "premise", "logline", "audience", "pov", "tense", "structure", "style_notes",
    "synopsis", "target_words", "chapter_words_min", "chapter_words_max", "status", "settings",
}
ChangeListener = Callable[[str, str, dict], None]


class Library:
    """Thread-safe access to the library. Text edits never overwrite history: each save is a revision."""

    def __init__(self, db: Database):
        self.db = db
        self._listeners: list[ChangeListener] = []
        self._listener_lock = threading.Lock()

    # ---- change notification -------------------------------------------------
    def subscribe(self, listener: ChangeListener) -> None:
        with self._listener_lock:
            self._listeners.append(listener)

    def unsubscribe(self, listener: ChangeListener) -> None:
        with self._listener_lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _emit(self, event: str, book_id: str, **detail: Any) -> None:
        with self._listener_lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event, book_id, detail)
            except Exception:  # a broken listener must never break a save
                log.exception("library listener failed for %s", event)

    # ---- books -----------------------------------------------------------------
    def create_book(self, title: str, genre: str = "general fiction", premise: str = "", **fields: Any) -> Book:
        title = (title or "").strip() or "Untitled Book"
        unknown = set(fields) - BOOK_FIELDS
        if unknown:
            raise ValidationError(f"unknown book fields: {sorted(unknown)}")
        now = time.time()
        book_id = uuid.uuid4().hex
        values = {"title": title, "genre": genre or "general fiction", "premise": premise or "", **fields}
        values.setdefault("status", "idea")
        self._validate_book_values(values)
        values["settings"] = json.dumps(values.get("settings") or {})
        cols = ["id", "created_at", "updated_at", *values]
        self.db.execute(
            f"INSERT INTO books ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            (book_id, now, now, *values.values()),
        )
        self._emit("book", book_id, action="created")
        return self.get_book(book_id)

    def _validate_book_values(self, values: dict) -> None:
        if "status" in values and values["status"] not in BOOK_STATUSES:
            raise ValidationError(f"invalid book status {values['status']!r}")
        for key in ("target_words", "chapter_words_min", "chapter_words_max"):
            if key in values and (not isinstance(values[key], int) or values[key] < 0):
                raise ValidationError(f"{key} must be a non-negative integer")

    def get_book(self, book_id: str) -> Book:
        row = self.db.query_one("SELECT * FROM books WHERE id=?", (book_id,))
        if not row:
            raise NotFound(f"book {book_id} not found")
        return Book.from_row(row)

    def list_books(self, include_archived: bool = False) -> list[Book]:
        sql = "SELECT * FROM books" + ("" if include_archived else " WHERE status != 'archived'")
        return [Book.from_row(r) for r in self.db.query(sql + " ORDER BY updated_at DESC")]

    def find_book(self, query: str) -> Book | None:
        """Find a book by id or (fuzzy) title, the way a person would say it out loud."""
        query = (query or "").strip()
        if not query:
            return None
        books = self.list_books(include_archived=True)
        for book in books:
            if book.id == query or book.title.lower() == query.lower():
                return book
        lowered = query.lower().removeprefix("the ").strip()
        for book in books:
            if lowered in book.title.lower():
                return book
        titles = {b.title.lower(): b for b in books}
        close = difflib.get_close_matches(query.lower(), list(titles), n=1, cutoff=0.6)
        return titles[close[0]] if close else None

    def update_book(self, book_id: str, **fields: Any) -> Book:
        unknown = set(fields) - BOOK_FIELDS
        if unknown:
            raise ValidationError(f"unknown book fields: {sorted(unknown)}")
        if not fields:
            return self.get_book(book_id)
        self._validate_book_values(fields)
        if "settings" in fields:
            merged = {**self.get_book(book_id).settings, **(fields["settings"] or {})}
            fields["settings"] = json.dumps(merged)
        sets = ", ".join(f"{k}=?" for k in fields)
        cur = self.db.execute(f"UPDATE books SET {sets}, updated_at=? WHERE id=?", (*fields.values(), time.time(), book_id))
        if cur.rowcount == 0:
            raise NotFound(f"book {book_id} not found")
        self._emit("book", book_id, action="updated", fields=sorted(fields))
        return self.get_book(book_id)

    def touch_book(self, book_id: str) -> None:
        self.db.execute("UPDATE books SET updated_at=? WHERE id=?", (time.time(), book_id))

    def delete_book(self, book_id: str) -> None:
        """Permanently delete. The UI archives by default; callers back up before deleting."""
        self.get_book(book_id)
        self.db.execute("DELETE FROM books WHERE id=?", (book_id,))
        self._emit("book", book_id, action="deleted")

    # ---- chapters --------------------------------------------------------------
    def list_chapters(self, book_id: str) -> list[Chapter]:
        return [Chapter.from_row(r) for r in self.db.query(
            "SELECT * FROM chapters WHERE book_id=? ORDER BY number", (book_id,))]

    def get_chapter(self, book_id: str, number: int) -> Chapter:
        row = self.db.query_one("SELECT * FROM chapters WHERE book_id=? AND number=?", (book_id, int(number)))
        if not row:
            raise NotFound(f"chapter {number} not found")
        return Chapter.from_row(row)

    def get_chapter_by_id(self, chapter_id: str) -> Chapter:
        row = self.db.query_one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
        if not row:
            raise NotFound(f"chapter {chapter_id} not found")
        return Chapter.from_row(row)

    def add_chapter(self, book_id: str, title: str = "", outline: str = "", target_words: int = 0,
                    number: int | None = None) -> Chapter:
        self.get_book(book_id)
        with self.db.transaction() as conn:
            if number is None:
                number = (conn.execute("SELECT COALESCE(MAX(number),0) FROM chapters WHERE book_id=?",
                                       (book_id,)).fetchone()[0]) + 1
            elif conn.execute("SELECT 1 FROM chapters WHERE book_id=? AND number=?", (book_id, number)).fetchone():
                # Insert: shift later chapters up (two-step to respect the unique index).
                conn.execute("UPDATE chapters SET number=-number-1 WHERE book_id=? AND number>=?", (book_id, number))
                conn.execute("UPDATE chapters SET number=-number WHERE book_id=? AND number<0", (book_id,))
            chapter_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO chapters (id, book_id, number, title, outline, target_words, updated_at) VALUES (?,?,?,?,?,?,?)",
                (chapter_id, book_id, number, title, outline, max(0, int(target_words)), time.time()),
            )
        self.touch_book(book_id)
        self._emit("chapter", book_id, action="created", number=number)
        return self.get_chapter_by_id(chapter_id)

    def set_outline(self, book_id: str, outline: Iterable[dict]) -> list[Chapter]:
        """Create or update outlined chapters. Existing chapter text is never touched."""
        self.get_book(book_id)
        now = time.time()
        with self.db.transaction() as conn:
            for item in outline:
                number = int(item["number"])
                existing = conn.execute("SELECT id FROM chapters WHERE book_id=? AND number=?", (book_id, number)).fetchone()
                title = str(item.get("title", ""))
                summary = str(item.get("outline", ""))
                target = max(0, int(item.get("target_words") or 0))
                if existing:
                    conn.execute("UPDATE chapters SET title=?, outline=?, target_words=?, updated_at=? WHERE id=?",
                                 (title, summary, target, now, existing[0]))
                else:
                    conn.execute(
                        "INSERT INTO chapters (id, book_id, number, title, outline, target_words, updated_at) "
                        "VALUES (?,?,?,?,?,?,?)", (uuid.uuid4().hex, book_id, number, title, summary, target, now))
        self.touch_book(book_id)
        self._emit("outline", book_id, action="updated")
        return self.list_chapters(book_id)

    def update_chapter(self, chapter_id: str, **fields: Any) -> Chapter:
        allowed = {"title", "outline", "plan", "summary", "status", "target_words"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValidationError(f"unknown chapter fields: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in CHAPTER_STATUSES:
            raise ValidationError(f"invalid chapter status {fields['status']!r}")
        if "plan" in fields:
            fields["plan"] = json.dumps(fields["plan"])
        chapter = self.get_chapter_by_id(chapter_id)
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE chapters SET {sets}, updated_at=? WHERE id=?", (*fields.values(), time.time(), chapter_id))
            self.touch_book(chapter.book_id)
            self._emit("chapter", chapter.book_id, action="updated", number=chapter.number, fields=sorted(fields))
        return self.get_chapter_by_id(chapter_id)

    def save_chapter_text(self, chapter_id: str, text: str, source: str = "user", note: str = "",
                          status: str | None = None) -> Chapter:
        """Save new chapter text and record it as a revision. Identical text is not duplicated."""
        if status is not None and status not in CHAPTER_STATUSES:
            raise ValidationError(f"invalid chapter status {status!r}")
        chapter = self.get_chapter_by_id(chapter_id)
        text = (text or "").replace("\r\n", "\n")
        now = time.time()
        count = word_count(text)
        with self.db.transaction() as conn:
            last = conn.execute("SELECT text FROM revisions WHERE chapter_id=? ORDER BY id DESC LIMIT 1",
                                (chapter_id,)).fetchone()
            if last is None or last[0] != text:
                conn.execute("INSERT INTO revisions (chapter_id, created_at, source, note, text, word_count) VALUES (?,?,?,?,?,?)",
                             (chapter_id, now, source, note, text, count))
            new_status = status or (chapter.status if chapter.status in ("drafted", "accepted") else
                                    ("drafted" if text.strip() else chapter.status))
            conn.execute("UPDATE chapters SET text=?, word_count=?, status=?, updated_at=? WHERE id=?",
                         (text, count, new_status, now, chapter_id))
        self.touch_book(chapter.book_id)
        self._emit("chapter_text", chapter.book_id, number=chapter.number, chapter_id=chapter_id, source=source)
        return self.get_chapter_by_id(chapter_id)

    def delete_chapter(self, chapter_id: str) -> None:
        chapter = self.get_chapter_by_id(chapter_id)
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM chapters WHERE id=?", (chapter_id,))
            conn.execute("UPDATE chapters SET number=-(number-1) WHERE book_id=? AND number>?", (chapter.book_id, chapter.number))
            conn.execute("UPDATE chapters SET number=-number WHERE book_id=? AND number<0", (chapter.book_id,))
        self._emit("chapter", chapter.book_id, action="deleted", number=chapter.number)

    # ---- revisions -------------------------------------------------------------
    def revisions(self, chapter_id: str) -> list[Revision]:
        return [Revision.from_row(r) for r in self.db.query(
            "SELECT * FROM revisions WHERE chapter_id=? ORDER BY id DESC", (chapter_id,))]

    def restore_revision(self, revision_id: int) -> Chapter:
        row = self.db.query_one("SELECT * FROM revisions WHERE id=?", (revision_id,))
        if not row:
            raise NotFound(f"revision {revision_id} not found")
        rev = Revision.from_row(row)
        return self.save_chapter_text(rev.chapter_id, rev.text, source="restore", note=f"restored revision {revision_id}")

    # ---- story bible -------------------------------------------------------------
    def upsert_bible(self, book_id: str, kind: str, name: str, summary: str = "",
                     data: dict | None = None) -> BibleEntry:
        kind = (kind or "").strip().lower()
        if kind not in BIBLE_KINDS:
            raise ValidationError(f"invalid story bible kind {kind!r}")
        name = (name or "").strip()
        if not name:
            raise ValidationError("story bible entries need a name")
        self.get_book(book_id)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM bible WHERE book_id=? AND kind=? AND lower(name)=lower(?)",
                               (book_id, kind, name)).fetchone()
            if row:
                merged = {**json.loads(row["data"] or "{}"), **(data or {})}
                conn.execute("UPDATE bible SET summary=?, data=?, updated_at=? WHERE id=?",
                             (summary or row["summary"], json.dumps(merged), time.time(), row["id"]))
                entry_id = row["id"]
            else:
                entry_id = conn.execute(
                    "INSERT INTO bible (book_id, kind, name, summary, data, updated_at) VALUES (?,?,?,?,?,?)",
                    (book_id, kind, name, summary, json.dumps(data or {}), time.time())).lastrowid
        self._emit("bible", book_id, entry_kind=kind, name=name)
        return BibleEntry.from_row(self.db.query_one("SELECT * FROM bible WHERE id=?", (entry_id,)))

    def list_bible(self, book_id: str, kind: str | None = None) -> list[BibleEntry]:
        if kind:
            rows = self.db.query("SELECT * FROM bible WHERE book_id=? AND kind=? ORDER BY name", (book_id, kind))
        else:
            rows = self.db.query("SELECT * FROM bible WHERE book_id=? ORDER BY kind, name", (book_id,))
        return [BibleEntry.from_row(r) for r in rows]

    def delete_bible_entry(self, entry_id: int) -> None:
        row = self.db.query_one("SELECT book_id FROM bible WHERE id=?", (entry_id,))
        if not row:
            raise NotFound(f"story bible entry {entry_id} not found")
        self.db.execute("DELETE FROM bible WHERE id=?", (entry_id,))
        self._emit("bible", row[0], action="deleted")

    # ---- notes and conversation -------------------------------------------------
    def add_note(self, book_id: str | None, text: str, source: str = "user") -> Note:
        text = (text or "").strip()
        if not text:
            raise ValidationError("note is empty")
        note_id = self.db.execute("INSERT INTO notes (book_id, created_at, source, text) VALUES (?,?,?,?)",
                                  (book_id, time.time(), source, text)).lastrowid
        if book_id:
            self._emit("note", book_id, note_id=note_id)
        return Note.from_row(self.db.query_one("SELECT * FROM notes WHERE id=?", (note_id,)))

    def list_notes(self, book_id: str | None) -> list[Note]:
        rows = self.db.query("SELECT * FROM notes WHERE book_id IS ? ORDER BY id DESC", (book_id,))
        return [Note.from_row(r) for r in rows]

    def add_message(self, channel: str, role: str, text: str, book_id: str | None = None) -> Message:
        msg_id = self.db.execute("INSERT INTO messages (book_id, channel, role, text, created_at) VALUES (?,?,?,?,?)",
                                 (book_id, channel, role, text, time.time())).lastrowid
        return Message.from_row(self.db.query_one("SELECT * FROM messages WHERE id=?", (msg_id,)))

    def recent_messages(self, channel: str, limit: int = 30, book_id: str | None = None) -> list[Message]:
        if book_id:
            rows = self.db.query("SELECT * FROM messages WHERE channel=? AND book_id=? ORDER BY id DESC LIMIT ?",
                                 (channel, book_id, int(limit)))
        else:
            rows = self.db.query("SELECT * FROM messages WHERE channel=? ORDER BY id DESC LIMIT ?",
                                 (channel, int(limit)))
        return [Message.from_row(r) for r in reversed(rows)]

    # ---- audiobook playback position -----------------------------------------
    def get_playback(self, book_id: str) -> Playback | None:
        row = self.db.query_one("SELECT * FROM playback WHERE book_id=?", (book_id,))
        return Playback(**dict(row)) if row else None

    def set_playback(self, book_id: str, chapter_number: int, segment: int, position_ms: int) -> None:
        self.db.execute(
            "INSERT INTO playback (book_id, chapter_number, segment, position_ms, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(book_id) DO UPDATE SET chapter_number=excluded.chapter_number, segment=excluded.segment, "
            "position_ms=excluded.position_ms, updated_at=excluded.updated_at",
            (book_id, int(chapter_number), int(segment), int(position_ms), time.time()))

    # ---- war room ---------------------------------------------------------------
    def start_war_session(self, book_id: str) -> WarSession:
        self.get_book(book_id)
        sid = self.db.execute("INSERT INTO war_sessions (book_id, started_at) VALUES (?,?)",
                              (book_id, time.time())).lastrowid
        return self.get_war_session(sid)

    def get_war_session(self, session_id: int) -> WarSession:
        row = self.db.query_one("SELECT * FROM war_sessions WHERE id=?", (session_id,))
        if not row:
            raise NotFound(f"war room session {session_id} not found")
        return WarSession.from_row(row)

    def end_war_session(self, session_id: int, summary: str, decisions: list, open_questions: list,
                        next_steps: list) -> WarSession:
        self.db.execute("UPDATE war_sessions SET ended_at=?, summary=?, decisions=?, open_questions=?, next_steps=? "
                        "WHERE id=?", (time.time(), summary.strip(), json.dumps(decisions), json.dumps(open_questions),
                                       json.dumps(next_steps), session_id))
        session = self.get_war_session(session_id)
        self._emit("war_session", session.book_id, session_id=session_id)
        return session

    def war_sessions(self, book_id: str, limit: int = 10) -> list[WarSession]:
        rows = self.db.query("SELECT * FROM war_sessions WHERE book_id=? ORDER BY id DESC LIMIT ?", (book_id, limit))
        return [WarSession.from_row(r) for r in rows]

    def add_proposal(self, book_id: str, kind: str, title: str, content: str, target: str = "",
                     rationale: str = "", session_id: int | None = None) -> Proposal:
        if kind not in PROPOSAL_KINDS:
            raise ValidationError(f"invalid proposal kind {kind!r}")
        if not (title or "").strip() or not (content or "").strip():
            raise ValidationError("a proposal needs a title and content")
        self.get_book(book_id)
        pid = self.db.execute(
            "INSERT INTO proposals (book_id, session_id, kind, target, title, content, rationale, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (book_id, session_id, kind, (target or "").strip(), title.strip(),
                                         content.strip(), (rationale or "").strip(), time.time())).lastrowid
        self._emit("proposal", book_id, proposal_id=pid, action="added")
        return self.get_proposal(pid)

    def get_proposal(self, proposal_id: int) -> Proposal:
        row = self.db.query_one("SELECT * FROM proposals WHERE id=?", (proposal_id,))
        if not row:
            raise NotFound(f"proposal {proposal_id} not found")
        return Proposal.from_row(row)

    def list_proposals(self, book_id: str, status: str | None = None) -> list[Proposal]:
        if status:
            rows = self.db.query("SELECT * FROM proposals WHERE book_id=? AND status=? ORDER BY id DESC", (book_id, status))
        else:
            rows = self.db.query("SELECT * FROM proposals WHERE book_id=? ORDER BY id DESC", (book_id,))
        return [Proposal.from_row(r) for r in rows]

    def set_proposal_status(self, proposal_id: int, status: str, applied: dict | None = None) -> Proposal:
        if status not in PROPOSAL_STATUSES:
            raise ValidationError(f"invalid proposal status {status!r}")
        prop = self.get_proposal(proposal_id)
        self.db.execute("UPDATE proposals SET status=?, applied=?, decided_at=? WHERE id=?",
                        (status, json.dumps(prop.applied if applied is None else applied), time.time(), proposal_id))
        self._emit("proposal", prop.book_id, proposal_id=proposal_id, action=status)
        return self.get_proposal(proposal_id)

    # ---- publishing -------------------------------------------------------------
    POST_FIELDS = {"status", "progress", "file_path", "upload_uri", "remote_id", "url", "error", "text_hash", "settings"}

    def add_post(self, book_id: str, provider: str, title: str, privacy: str, chapters: list[int],
                 settings: dict | None = None) -> Post:
        if not (title or "").strip():
            raise ValidationError("a post needs a title")
        self.get_book(book_id)
        now = time.time()
        pid = self.db.execute(
            "INSERT INTO publish_posts (book_id, provider, title, privacy, chapters, settings, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (book_id, provider, title.strip(), privacy, json.dumps(list(chapters)),
                                         json.dumps(settings or {}), now, now)).lastrowid
        self._emit("post", book_id, post_id=pid)
        return self.get_post(pid)

    def get_post(self, post_id: int) -> Post:
        row = self.db.query_one("SELECT * FROM publish_posts WHERE id=?", (post_id,))
        if not row:
            raise NotFound(f"post {post_id} not found")
        return Post.from_row(row)

    def update_post(self, post_id: int, **fields: Any) -> Post:
        unknown = set(fields) - self.POST_FIELDS
        if unknown:
            raise ValidationError(f"unknown post fields: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in POST_STATUSES:
            raise ValidationError(f"invalid post status {fields['status']!r}")
        if "settings" in fields:
            fields["settings"] = json.dumps(fields["settings"])
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE publish_posts SET {cols}, updated_at=? WHERE id=?",
                        (*fields.values(), time.time(), post_id))
        post = self.get_post(post_id)
        self._emit("post", post.book_id, post_id=post_id)
        return post

    def list_posts(self, book_id: str | None = None, limit: int = 100) -> list[Post]:
        if book_id:
            rows = self.db.query("SELECT * FROM publish_posts WHERE book_id=? ORDER BY id DESC LIMIT ?", (book_id, limit))
        else:
            rows = self.db.query("SELECT * FROM publish_posts ORDER BY id DESC LIMIT ?", (limit,))
        return [Post.from_row(r) for r in rows]

    # ---- meta -----------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value))

    # ---- stats -------------------------------------------------------------------
    def book_stats(self, book_id: str) -> dict[str, int]:
        row = self.db.query_one(
            "SELECT COUNT(*), COALESCE(SUM(word_count),0), "
            "SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN word_count>0 THEN 1 ELSE 0 END) FROM chapters WHERE book_id=?", (book_id,))
        return {"chapters": row[0], "words": row[1], "accepted": row[2] or 0, "written": row[3] or 0}
