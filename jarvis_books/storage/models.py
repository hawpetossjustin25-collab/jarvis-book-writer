from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

BOOK_STATUSES = ("idea", "planned", "drafting", "complete", "archived")
CHAPTER_STATUSES = ("outlined", "planned", "drafted", "accepted")
BIBLE_KINDS = ("character", "location", "item", "rule", "theme", "thread", "event", "style")
# A proposal changes canon only when accepted: bible entry, chapter outline, or a saved idea/note.
PROPOSAL_KINDS = ("bible", "outline", "note")
PROPOSAL_STATUSES = ("proposed", "accepted", "rejected", "undone")
POST_STATUSES = ("queued", "rendering", "uploading", "processing", "published", "failed", "cancelled")


@dataclass
class Book:
    id: str
    title: str
    genre: str
    premise: str
    logline: str
    audience: str
    pov: str
    tense: str
    structure: str
    style_notes: str
    synopsis: str
    target_words: int
    chapter_words_min: int
    chapter_words_max: int
    status: str
    settings: dict[str, Any]
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row) -> "Book":
        data = dict(row)
        data["settings"] = json.loads(data.get("settings") or "{}")
        return cls(**data)


@dataclass
class Chapter:
    id: str
    book_id: str
    number: int
    title: str
    outline: str
    plan: list[dict]
    text: str
    summary: str
    status: str
    word_count: int
    target_words: int
    updated_at: float

    @classmethod
    def from_row(cls, row) -> "Chapter":
        data = dict(row)
        data["plan"] = json.loads(data.get("plan") or "[]")
        return cls(**data)

    @property
    def heading(self) -> str:
        return f"Chapter {self.number}" + (f": {self.title}" if self.title else "")


@dataclass
class Revision:
    id: int
    chapter_id: str
    created_at: float
    source: str
    note: str
    text: str
    word_count: int

    @classmethod
    def from_row(cls, row) -> "Revision":
        return cls(**dict(row))


@dataclass
class BibleEntry:
    id: int
    book_id: str
    kind: str
    name: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0

    @classmethod
    def from_row(cls, row) -> "BibleEntry":
        data = dict(row)
        data["data"] = json.loads(data.get("data") or "{}")
        return cls(**data)


@dataclass
class Note:
    id: int
    book_id: str | None
    created_at: float
    source: str
    text: str

    @classmethod
    def from_row(cls, row) -> "Note":
        return cls(**dict(row))


@dataclass
class Message:
    id: int
    book_id: str | None
    channel: str
    role: str
    text: str
    created_at: float

    @classmethod
    def from_row(cls, row) -> "Message":
        return cls(**dict(row))


@dataclass
class Playback:
    book_id: str
    chapter_number: int
    segment: int
    position_ms: int
    updated_at: float


@dataclass
class WarSession:
    id: int
    book_id: str
    started_at: float
    ended_at: float | None
    summary: str
    decisions: list
    open_questions: list
    next_steps: list

    @classmethod
    def from_row(cls, row) -> "WarSession":
        d = dict(row)
        for k in ("decisions", "open_questions", "next_steps"):
            d[k] = json.loads(d[k] or "[]")
        return cls(**d)


@dataclass
class Proposal:
    id: int
    book_id: str
    session_id: int | None
    kind: str
    target: str
    title: str
    content: str
    rationale: str
    status: str
    applied: dict
    created_at: float
    decided_at: float | None

    @classmethod
    def from_row(cls, row) -> "Proposal":
        d = dict(row)
        d["applied"] = json.loads(d["applied"] or "{}")
        return cls(**d)


@dataclass
class Post:
    id: int
    book_id: str
    provider: str
    title: str
    privacy: str
    chapters: list
    settings: dict
    status: str
    progress: float
    file_path: str
    upload_uri: str
    remote_id: str
    url: str
    error: str
    text_hash: str
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row) -> "Post":
        d = dict(row)
        d["chapters"] = json.loads(d["chapters"] or "[]")
        d["settings"] = json.loads(d["settings"] or "{}")
        return cls(**d)
