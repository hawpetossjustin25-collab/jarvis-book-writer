"""Audiobook narration with a passage-level cache.

Chapters are split into passages; each passage is synthesized once per (text, voice) and cached, so
editing one paragraph re-records only that passage, and unfinished books can be listened to at any time.
Gemini TTS is the primary narrator. When its daily speech quota is used up, the free Microsoft Edge
neural voices (no LLM involved) keep the audiobook playing.
"""
from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from jarvis_books.llm.errors import LLMError
from jarvis_books.storage.library import Library
from jarvis_books.storage.models import Chapter
from jarvis_books.text import split_passages, text_hash

log = logging.getLogger(__name__)

GEMINI_VOICES = ("Kore", "Charon", "Puck", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr", "Sulafat", "Gacrux")
EDGE_VOICES = ("en-US-AndrewNeural", "en-US-AvaNeural", "en-US-BrianNeural", "en-US-EmmaNeural",
               "en-GB-RyanNeural", "en-GB-SoniaNeural")
DEFAULT_STYLE = "Narrate like a professional audiobook narrator: warm, clear, expressive, with natural pacing."


@dataclass(frozen=True)
class Segment:
    chapter_id: str
    chapter_number: int
    ord: int
    text: str


def wav_duration_ms(data: bytes) -> Optional[int]:
    try:
        with wave.open(io.BytesIO(data)) as w:
            return int(w.getnframes() * 1000 / w.getframerate())
    except (wave.Error, EOFError):
        return None


def edge_tts_bytes(text: str, voice: str) -> bytes:
    import edge_tts

    async def run() -> bytes:
        out = bytearray()
        async for chunk in edge_tts.Communicate(text, voice).stream():
            if chunk.get("type") == "audio":
                out.extend(chunk["data"])
        return bytes(out)

    return asyncio.run(run())


class Narrator:
    def __init__(self, library: Library, engine, cache_dir: Path, voice: str = "Kore",
                 edge_voice: str = "en-US-AndrewNeural", style: str = DEFAULT_STYLE, passage_words: int = 220,
                 edge_fallback: bool = True, edge_synth=edge_tts_bytes):
        self.library = library
        self.engine = engine
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.voice = voice
        self.edge_voice = edge_voice
        self.style = style
        self.passage_words = passage_words
        self.edge_fallback = edge_fallback
        self._edge = edge_synth
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.last_backend = ""

    # ---- segmentation -------------------------------------------------------------
    def chapter_segments(self, chapter: Chapter) -> list[Segment]:
        if not chapter.text.strip():
            return []
        texts = [f"{chapter.heading}."] + split_passages(chapter.text.replace("* * *", ""), self.passage_words)
        return [Segment(chapter.id, chapter.number, i, t) for i, t in enumerate(texts) if t.strip()]

    def book_segments(self, book_id: str) -> list[Segment]:
        out: list[Segment] = []
        for chapter in self.library.list_chapters(book_id):
            out.extend(self.chapter_segments(chapter))
        return out

    # ---- synthesis + cache -------------------------------------------------------------
    def _keys(self) -> list[str]:
        keys = [f"gemini:{self.voice}:{text_hash(self.style)[:8]}"]
        if self.edge_fallback:
            keys.append(f"edge:{self.edge_voice}")
        return keys

    def cached_path(self, segment: Segment) -> Optional[Path]:
        h = text_hash(segment.text)
        for key in self._keys():
            row = self.library.db.query_one(
                "SELECT id, path FROM audio_segments WHERE text_hash=? AND voice_key=? ORDER BY id DESC LIMIT 1", (h, key))
            if row and Path(row["path"]).exists():
                self.library.db.execute("UPDATE audio_segments SET last_used=? WHERE id=?", (time.time(), row["id"]))
                return Path(row["path"])
        return None

    def _lock_for(self, h: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(h, threading.Lock())

    def ensure(self, segment: Segment) -> Path:
        """Return an audio file for the segment, synthesizing it if it is not cached."""
        h = text_hash(segment.text)
        with self._lock_for(h):
            cached = self.cached_path(segment)
            if cached:
                return cached
            data, key, ext = self._synthesize(segment.text)
            path = self.cache_dir / f"{h[:2]}" / f"{h}-{text_hash(key)[:8]}.{ext}"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            now = time.time()
            self.library.db.execute(
                "INSERT INTO audio_segments (chapter_id, ord, text_hash, voice_key, path, bytes, duration_ms, created_at, "
                "last_used) VALUES (?,?,?,?,?,?,?,?,?)",
                (segment.chapter_id, segment.ord, h, key, str(path), len(data),
                 wav_duration_ms(data) if ext == "wav" else None, now, now))
            return path

    def _synthesize(self, text: str) -> tuple[bytes, str, str]:
        gemini_key, *rest = self._keys()
        try:
            data = self.engine.tts(text, self.voice, self.style)
            self.last_backend = "gemini"
            return data, gemini_key, "wav"
        except LLMError as exc:
            if not rest or exc.kind == "auth":
                raise
            log.info("Gemini speech unavailable (%s); narrating with Edge voice", exc.kind)
        data = self._edge(text, self.edge_voice)
        if not data:
            raise LLMError("speech synthesis returned no audio", kind="unavailable")
        self.last_backend = "edge"
        return data, rest[0], "mp3"

    def prepare(self, segments: list[Segment], cancel: Optional[threading.Event] = None, progress=None) -> int:
        made = 0
        for i, seg in enumerate(segments):
            if cancel is not None and cancel.is_set():
                break
            if self.cached_path(seg) is None:
                self.ensure(seg)
                made += 1
            if progress:
                progress((i + 1) / max(1, len(segments)), f"Recorded {i + 1} of {len(segments)} passages")
        return made

    def prune_orphans(self, book_id: str) -> int:
        """Forget cached audio for passages that no longer exist in the book (files are regenerable)."""
        live = {text_hash(s.text) for s in self.book_segments(book_id)}
        rows = self.library.db.query(
            "SELECT a.id, a.text_hash, a.path FROM audio_segments a JOIN chapters c ON c.id=a.chapter_id "
            "WHERE c.book_id=?", (book_id,))
        removed = 0
        for row in rows:
            if row["text_hash"] not in live:
                Path(row["path"]).unlink(missing_ok=True)
                self.library.db.execute("DELETE FROM audio_segments WHERE id=?", (row["id"],))
                removed += 1
        return removed
