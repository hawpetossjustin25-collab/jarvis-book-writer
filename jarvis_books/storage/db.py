"""SQLite connection management and schema migrations for the book library.

SQLite grows on demand (no preallocated size), so the library is only bounded by free disk.
WAL mode lets the UI read while background writing jobs commit.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE books (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        genre TEXT NOT NULL DEFAULT 'general fiction',
        premise TEXT NOT NULL DEFAULT '',
        logline TEXT NOT NULL DEFAULT '',
        audience TEXT NOT NULL DEFAULT 'adult',
        pov TEXT NOT NULL DEFAULT '',
        tense TEXT NOT NULL DEFAULT '',
        structure TEXT NOT NULL DEFAULT '',
        style_notes TEXT NOT NULL DEFAULT '',
        synopsis TEXT NOT NULL DEFAULT '',
        target_words INTEGER NOT NULL DEFAULT 0,
        chapter_words_min INTEGER NOT NULL DEFAULT 0,
        chapter_words_max INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'idea',
        settings TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE chapters (
        id TEXT PRIMARY KEY,
        book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        number INTEGER NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        outline TEXT NOT NULL DEFAULT '',
        plan TEXT NOT NULL DEFAULT '[]',
        text TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'outlined',
        word_count INTEGER NOT NULL DEFAULT 0,
        target_words INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL,
        UNIQUE(book_id, number)
    );
    CREATE INDEX idx_chapters_book ON chapters(book_id, number);
    CREATE TABLE revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        source TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL,
        word_count INTEGER NOT NULL
    );
    CREATE INDEX idx_revisions_chapter ON revisions(chapter_id, id);
    CREATE TABLE bible (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        data TEXT NOT NULL DEFAULT '{}',
        updated_at REAL NOT NULL,
        UNIQUE(book_id, kind, name)
    );
    CREATE TABLE notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id TEXT REFERENCES books(id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        source TEXT NOT NULL DEFAULT 'user',
        text TEXT NOT NULL
    );
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id TEXT,
        channel TEXT NOT NULL,
        role TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE INDEX idx_messages_channel ON messages(channel, id);
    CREATE TABLE chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
        ord INTEGER NOT NULL,
        text TEXT NOT NULL,
        text_hash TEXT NOT NULL,
        embedding BLOB,
        embed_model TEXT
    );
    CREATE INDEX idx_chunks_chapter ON chunks(chapter_id, ord);
    CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content='chunks', content_rowid='id');
    CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
        INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
    END;
    CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END;
    CREATE TABLE audio_segments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
        ord INTEGER NOT NULL,
        text_hash TEXT NOT NULL,
        voice_key TEXT NOT NULL,
        path TEXT NOT NULL,
        bytes INTEGER NOT NULL,
        duration_ms INTEGER,
        created_at REAL NOT NULL,
        last_used REAL NOT NULL
    );
    CREATE INDEX idx_audio_lookup ON audio_segments(text_hash, voice_key);
    CREATE INDEX idx_audio_lru ON audio_segments(last_used);
    CREATE TABLE playback (
        book_id TEXT PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
        chapter_number INTEGER NOT NULL,
        segment INTEGER NOT NULL,
        position_ms INTEGER NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        book_id TEXT,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        progress REAL NOT NULL DEFAULT 0,
        message TEXT NOT NULL DEFAULT '',
        error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    2: """
    CREATE TABLE war_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        started_at REAL NOT NULL,
        ended_at REAL,
        summary TEXT NOT NULL DEFAULT '',
        decisions TEXT NOT NULL DEFAULT '[]',
        open_questions TEXT NOT NULL DEFAULT '[]',
        next_steps TEXT NOT NULL DEFAULT '[]'
    );
    CREATE INDEX idx_war_sessions_book ON war_sessions(book_id, id);
    CREATE TABLE proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        session_id INTEGER REFERENCES war_sessions(id) ON DELETE SET NULL,
        kind TEXT NOT NULL,
        target TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        rationale TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'proposed',
        applied TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        decided_at REAL
    );
    CREATE INDEX idx_proposals_book ON proposals(book_id, status, id);
    """,
    3: """
    CREATE TABLE publish_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        title TEXT NOT NULL,
        privacy TEXT NOT NULL,
        chapters TEXT NOT NULL DEFAULT '[]',
        settings TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'queued',
        progress REAL NOT NULL DEFAULT 0,
        file_path TEXT NOT NULL DEFAULT '',
        upload_uri TEXT NOT NULL DEFAULT '',
        remote_id TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        text_hash TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX idx_publish_posts_book ON publish_posts(book_id, id);
    """,
}

SCHEMA_VERSION = max(MIGRATIONS)


class Database:
    """One shared connection guarded by a re-entrant lock (safe across UI and worker threads)."""

    def __init__(self, path: Path | str):
        self.path = Path(path) if str(path) != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for version in sorted(v for v in MIGRATIONS if v > current):
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split_sql(MIGRATIONS[version]):
                        self._conn.execute(statement)
                    self._conn.execute(f"PRAGMA user_version={version}")
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

    @property
    def schema_version(self) -> int:
        return self.query_one("PRAGMA user_version")[0]

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def backup_to(self, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            dest = sqlite3.connect(str(target))
            try:
                self._conn.backup(dest)
            finally:
                dest.close()
        return target

    def size_bytes(self) -> int:
        if not isinstance(self.path, Path):
            return 0
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _split_sql(script: str) -> list[str]:
    """Split a migration script into statements, keeping trigger bodies intact."""
    statements, buf, depth = [], [], 0
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        buf.append(line)
        upper = stripped.upper()
        if upper.endswith("BEGIN"):
            depth += 1
        if upper == "END;":
            depth -= 1
        if stripped.endswith(";") and depth == 0:
            statements.append("\n".join(buf).rstrip().rstrip(";"))
            buf = []
    if buf:
        statements.append("\n".join(buf))
    return statements
