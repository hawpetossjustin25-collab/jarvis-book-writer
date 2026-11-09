"""Small text helpers shared across the app."""
from __future__ import annotations

import hashlib
import re

_WORD = re.compile(r"[A-Za-z0-9'\u2019-]+")


def word_count(text: str) -> int:
    return len(_WORD.findall(text or ""))


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def text_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def safe_filename(name: str, limit: int = 80) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "Untitled")[:limit]


def truncate_words(text: str, max_words: int, from_end: bool = False) -> str:
    words = (text or "").split()
    if len(words) <= max_words:
        return text or ""
    kept = words[-max_words:] if from_end else words[:max_words]
    return ("... " + " ".join(kept)) if from_end else (" ".join(kept) + " ...")


def split_passages(text: str, target_words: int = 300) -> list[str]:
    """Group paragraphs into passages of roughly target_words for retrieval."""
    out: list[str] = []
    buf: list[str] = []
    count = 0
    for para in paragraphs(text):
        n = word_count(para)
        if buf and count + n > target_words * 1.4:
            out.append("\n\n".join(buf))
            buf, count = [], 0
        buf.append(para)
        count += n
        if count >= target_words:
            out.append("\n\n".join(buf))
            buf, count = [], 0
    if buf:
        out.append("\n\n".join(buf))
    return out
