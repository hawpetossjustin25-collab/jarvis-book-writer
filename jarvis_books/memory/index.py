"""Searchable story memory: every chapter is split into passages, keyword-indexed (SQLite FTS5)
and embedded with Gemini when available. Search fuses both rankings, so Jarvis can find any
moment in any book without loading whole books into the prompt."""
from __future__ import annotations

import logging
import re
import threading
from array import array
from dataclasses import dataclass
from typing import Optional

from jarvis_books.storage.library import Library
from jarvis_books.text import split_passages, text_hash

log = logging.getLogger(__name__)


@dataclass
class SearchHit:
    book_id: str
    book_title: str
    chapter_number: int
    chapter_title: str
    text: str
    score: float


def _pack(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _unpack(blob: bytes) -> array:
    out = array("f")
    out.frombytes(blob)
    return out


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def fts_query(text: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9']+", text.lower()) if len(w) > 2][:12]
    return " OR ".join(f'"{w}"' for w in words)


class StoryIndex:
    def __init__(self, library: Library, engine=None, passage_words: int = 300):
        self.library = library
        self.engine = engine
        self.passage_words = passage_words
        self._lock = threading.Lock()

    def index_chapter(self, chapter_id: str) -> int:
        """Re-index a chapter's passages. Unchanged passages keep their embeddings. Returns passages added."""
        chapter = self.library.get_chapter_by_id(chapter_id)
        passages = split_passages(chapter.text, self.passage_words)
        db = self.library.db
        with self._lock:
            existing = {r["text_hash"]: r for r in db.query(
                "SELECT id, text_hash, embedding, embed_model FROM chunks WHERE chapter_id=?", (chapter_id,))}
            added = 0
            with db.transaction() as conn:
                conn.execute("DELETE FROM chunks WHERE chapter_id=?", (chapter_id,))
                for ord_, passage in enumerate(passages):
                    h = text_hash(passage)
                    old = existing.get(h)
                    conn.execute("INSERT INTO chunks (book_id, chapter_id, ord, text, text_hash, embedding, embed_model) "
                                 "VALUES (?,?,?,?,?,?,?)", (chapter.book_id, chapter_id, ord_, passage, h,
                                                            old["embedding"] if old else None,
                                                            old["embed_model"] if old else None))
                    added += 0 if old else 1
        return added

    def index_book(self, book_id: str) -> int:
        return sum(self.index_chapter(c.id) for c in self.library.list_chapters(book_id) if c.text.strip())

    def embed_pending(self, limit: int = 64) -> int:
        """Embed passages that have no vector yet. Safe to call repeatedly in the background."""
        if self.engine is None:
            return 0
        rows = self.library.db.query("SELECT id, text FROM chunks WHERE embedding IS NULL LIMIT ?", (limit,))
        if not rows:
            return 0
        model, vectors = self.engine.embed([r["text"] for r in rows], "RETRIEVAL_DOCUMENT")
        with self.library.db.transaction() as conn:
            for row, vec in zip(rows, vectors):
                conn.execute("UPDATE chunks SET embedding=?, embed_model=? WHERE id=?", (_pack(vec), model, row["id"]))
        return len(rows)

    def search(self, query: str, book_id: Optional[str] = None, k: int = 6) -> list[SearchHit]:
        query = (query or "").strip()
        if not query:
            return []
        scores: dict[int, float] = {}
        where, params = ("AND c.book_id=?", [book_id]) if book_id else ("", [])
        match = fts_query(query)
        if match:
            rows = self.library.db.query(
                f"SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id=f.rowid WHERE chunks_fts MATCH ? {where} "
                "ORDER BY bm25(chunks_fts) LIMIT 30", (match, *params))
            for rank, row in enumerate(rows):
                scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (60 + rank)
        if self.engine is not None:
            try:
                model, vectors = self.engine.embed([query], "RETRIEVAL_QUERY")
                qv = vectors[0]
                rows = self.library.db.query(
                    f"SELECT c.id, c.embedding FROM chunks c WHERE c.embedding IS NOT NULL AND c.embed_model=? {where}",
                    (model, *params))
                ranked = sorted(rows, key=lambda r: -_cosine(qv, _unpack(r["embedding"])))[:30]
                for rank, row in enumerate(ranked):
                    scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (60 + rank)
            except Exception as exc:  # keyword search still works offline or out of quota
                log.info("semantic search unavailable: %s", exc)
        top = sorted(scores.items(), key=lambda kv: -kv[1])[:k]
        hits = []
        for chunk_id, score in top:
            row = self.library.db.query_one(
                "SELECT c.text, c.book_id, b.title AS book_title, ch.number, ch.title FROM chunks c "
                "JOIN books b ON b.id=c.book_id JOIN chapters ch ON ch.id=c.chapter_id WHERE c.id=?", (chunk_id,))
            if row:
                hits.append(SearchHit(row["book_id"], row["book_title"], row["number"], row["title"], row["text"], score))
        return hits
