"""Audiobook narration with a passage-level cache.

Chapters are split into passages; each passage is synthesized once per (text, voice) and cached, so
editing one paragraph re-records only that passage, and unfinished books can be listened to at any time.
Gemini TTS is the primary narrator. When its daily speech quota is used up, the free Microsoft Edge
neural voices (no LLM involved) keep the audiobook playing.

When a book has a voice cast, each passage is read by the narrator and its characters: lines are grouped into
requests of at most two voices (Gemini's multi-speaker limit) and joined into one audio file per passage, so the
player, exports and videos keep working passage by passage. The cache key covers every line, voice and
direction, so changing one character's voice re-records only the passages they speak in.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from jarvis_books.audio.voices import NARRATOR, parse_edge_voice
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
    book_id: str = ""


def wav_duration_ms(data: bytes) -> Optional[int]:
    try:
        with wave.open(io.BytesIO(data)) as w:
            return int(w.getnframes() * 1000 / w.getframerate())
    except (wave.Error, EOFError):
        return None


def join_wavs(parts: list[bytes], gap_ms: int = 150) -> bytes:
    """One WAV from several (same format), with a short pause between them."""
    frames, params = [], None
    for data in parts:
        with wave.open(io.BytesIO(data)) as w:
            p = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            if params is None:
                params = p
            elif p != params:
                raise LLMError(f"speech came back in mixed formats {params} and {p}", kind="invalid_output")
            frames.append(w.readframes(w.getnframes()))
    if params is None:
        raise LLMError("no speech to join", kind="invalid_output")
    channels, width, rate = params
    silence = b"\x00" * (int(rate * gap_ms / 1000) * channels * width)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(silence.join(frames))
    return buf.getvalue()


def chunk_lines(lines: list) -> list[list]:
    """Consecutive lines grouped so each group has at most two different speakers."""
    chunks: list[list] = []
    for ln in lines:
        if chunks and len({x.speaker for x in chunks[-1]} | {ln.speaker}) <= 2:
            chunks[-1].append(ln)
        else:
            chunks.append([ln])
    return chunks


def speech_text(line) -> str:
    """What a voice says: a character's line without the quotation marks."""
    if line.speaker == NARRATOR:
        return line.text
    return re.sub(r'\s+', " ", re.sub(r'["\u201c\u201d]', "", line.text)).strip()


def _token(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", name) or "Speaker"


def edge_tts_bytes(text: str, voice: str) -> bytes:
    import edge_tts

    name, pitch, rate = parse_edge_voice(voice)

    async def run() -> bytes:
        out = bytearray()
        async for chunk in edge_tts.Communicate(text, name, rate=rate, pitch=pitch).stream():
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
        self._edge_synth = edge_synth
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.last_backend = ""
        self.director = None  # the VoiceDirector (narrator per book + character voices); set by the Studio
        self._texts: dict[str, tuple[str, list[str]]] = {}  # chapter id -> (text hash, passage texts)

    # ---- segmentation -------------------------------------------------------------
    def chapter_segments(self, chapter: Chapter) -> list[Segment]:
        if not chapter.text.strip():
            return []
        texts = [f"{chapter.heading}."] + split_passages(chapter.text.replace("* * *", ""), self.passage_words)
        segments = [Segment(chapter.id, chapter.number, i, t, chapter.book_id) for i, t in enumerate(texts) if t.strip()]
        self._texts[chapter.id] = (text_hash(chapter.text + chapter.heading), texts)
        return segments

    def _chapter_texts(self, segment: Segment) -> list[str]:
        cached = self._texts.get(segment.chapter_id)
        if cached is None:
            self.chapter_segments(self.library.get_chapter_by_id(segment.chapter_id))
            cached = self._texts.get(segment.chapter_id, ("", [segment.text]))
        return cached[1]

    def book_segments(self, book_id: str) -> list[Segment]:
        out: list[Segment] = []
        for chapter in self.library.list_chapters(book_id):
            out.extend(self.chapter_segments(chapter))
        return out

    # ---- synthesis + cache -------------------------------------------------------------
    def _edge(self, text: str, voice: str) -> bytes:
        try:
            return self._edge_synth(text, voice)
        except LLMError:
            raise
        except Exception as exc:  # network trouble or a voice Edge doesn't know
            raise LLMError(f"backup voice {voice} failed: {exc}", kind="unavailable") from exc

    def _plan(self, segment: Segment, offline: bool) -> Optional[dict]:
        """How a passage is voiced and the cache keys for it. None when its dialogue is not labelled yet."""
        voice, edge_voice = self.voice, self.edge_voice
        if self.director is not None and segment.book_id:
            narrator = self.director.narrator(segment.book_id)
            voice, edge_voice = narrator["voice"] or voice, narrator["edge_voice"] or edge_voice
            if self.director.has_characters(segment.book_id):
                texts = self._chapter_texts(segment)
                index = segment.ord if segment.ord < len(texts) and texts[segment.ord] == segment.text \
                    else texts.index(segment.text) if segment.text in texts else None
                lines = self.director.lines_for(segment.book_id, texts, index, offline=offline) \
                    if index is not None else None
                if lines is None and index is None:
                    lines = self.director.script(segment.book_id, segment.text, offline=offline)
                if lines is None:
                    return None
                if any(ln.speaker != NARRATOR for ln in lines):
                    cast = {m["name"]: m for m in self.director.cast(segment.book_id)}
                    cast[NARRATOR] = {**cast.get(NARRATOR, {}), "voice": voice, "edge_voice": edge_voice,
                                      "style": ""}
                    lines = [ln for ln in lines if ln.speaker in cast]
                    sig = [[ln.speaker, cast[ln.speaker]["voice"], cast[ln.speaker]["style"], ln.style, ln.text]
                           for ln in lines]
                    edge_sig = [[cast[ln.speaker]["edge_voice"], ln.text] for ln in lines]
                    keys = [f"cast:{text_hash(json.dumps([self.style, sig]))[:16]}"]
                    if self.edge_fallback:
                        keys.append(f"cast-edge:{text_hash(json.dumps(edge_sig))[:16]}")
                    return {"keys": keys, "lines": lines, "cast": cast}
        keys = [f"gemini:{voice}:{text_hash(self.style)[:8]}"]
        if self.edge_fallback:
            keys.append(f"edge:{edge_voice}")
        return {"keys": keys, "voice": voice, "edge_voice": edge_voice}

    def _keys(self) -> list[str]:
        keys = [f"gemini:{self.voice}:{text_hash(self.style)[:8]}"]
        if self.edge_fallback:
            keys.append(f"edge:{self.edge_voice}")
        return keys

    def cached_path(self, segment: Segment) -> Optional[Path]:
        """The recorded audio for a passage, if any. Never calls Gemini (the player uses it on the UI thread)."""
        plan = self._plan(segment, offline=True)
        if plan is None:
            return None
        h = text_hash(segment.text)
        for key in plan["keys"]:
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
            plan = self._plan(segment, offline=False)
            if "lines" in plan:
                data, key, ext = self._synthesize_cast(plan)
            else:
                data, key, ext = self._synthesize(segment.text, plan)
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

    def _synthesize(self, text: str, plan: Optional[dict] = None) -> tuple[bytes, str, str]:
        plan = plan or {"keys": self._keys(), "voice": self.voice, "edge_voice": self.edge_voice}
        gemini_key, *rest = plan["keys"]
        try:
            data = self.engine.tts(text, plan["voice"], self.style)
            self.last_backend = "gemini"
            return data, gemini_key, "wav"
        except LLMError as exc:
            if not rest or exc.kind == "auth":
                raise
            log.info("Gemini speech unavailable (%s); narrating with Edge voice", exc.kind)
        data = self._edge(text, plan["edge_voice"])
        if not data:
            raise LLMError("speech synthesis returned no audio", kind="unavailable")
        self.last_backend = "edge"
        return data, rest[0], "mp3"

    def _direction(self, name: str, member: dict) -> str:
        if name == NARRATOR:
            return f"{_token(name)} is the narrator. {self.style}"
        style = f", {member['style']}" if member.get("style") else ""
        return f"{_token(name)} is the character {name}{style}. Perform their lines in character."

    def _gemini_chunk(self, lines: list, cast: dict) -> bytes:
        names = list(dict.fromkeys(ln.speaker for ln in lines))
        if len(names) == 1:
            name = names[0]
            text = " ".join((f"[{ln.style}] " if ln.style else "") + speech_text(ln) for ln in lines)
            style = self.style if name == NARRATOR else (
                f"Speak as {name}" + (f", {cast[name]['style']}" if cast[name].get("style") else "") + ", in character")
            return self.engine.tts(text, cast[name]["voice"], style)
        tokens = {n: _token(n) for n in names}
        if tokens[names[0]] == tokens[names[1]]:
            tokens[names[1]] += "Two"
        def tag(ln) -> str:
            styles = [ln.style] if ln.speaker == NARRATOR else [cast[ln.speaker].get("style", ""), ln.style]
            styles = [x.strip() for x in styles if x and x.strip()]
            return f"[{', '.join(styles)}] " if styles else ""
        transcript = "\n".join(f"{tokens[ln.speaker]}: {tag(ln)}{speech_text(ln)}" for ln in lines)
        direction = "Read this audiobook passage aloud, exactly as written. " + " ".join(
            self._direction(n, cast[n]).replace(_token(n), tokens[n], 1) for n in names)
        return self.engine.tts_multi(transcript, [(tokens[n], cast[n]["voice"]) for n in names], direction)

    def _synthesize_cast(self, plan: dict) -> tuple[bytes, str, str]:
        gemini_key, *rest = plan["keys"]
        cast = plan["cast"]
        lines = [ln for ln in plan["lines"] if re.search(r"\w", speech_text(ln))]  # skip stray ', ' or '.'
        try:
            chunks = chunk_lines(lines)
            if len(chunks) == 1:
                data = join_wavs([self._gemini_chunk(chunks[0], cast)])
            else:  # the pieces are independent, so record them side by side and join them in order
                with ThreadPoolExecutor(max_workers=min(3, len(chunks)), thread_name_prefix="cast-tts") as pool:
                    data = join_wavs(list(pool.map(lambda c: self._gemini_chunk(c, cast), chunks)))
            self.last_backend = "gemini"
            return data, gemini_key, "wav"
        except LLMError as exc:
            if not rest or exc.kind == "auth":
                raise
            log.info("Gemini speech unavailable (%s); voicing the cast with Edge voices", exc.kind)
        parts = [self._edge(speech_text(ln), cast[ln.speaker]["edge_voice"]) for ln in lines]
        if not all(parts):
            raise LLMError("speech synthesis returned no audio", kind="unavailable")
        self.last_backend = "edge"
        return b"".join(parts), rest[0], "mp3"

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
