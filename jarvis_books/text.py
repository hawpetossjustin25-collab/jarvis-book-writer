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


_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]+)\)|((?:https?://|www\.)[^\s<>\"'`]+)", re.I)
_TRAILING = ".,;:!?*_"


def linkify(text: str) -> str:
    """Escaped HTML for chat text with every web address (bare or as a Markdown [label](url) link) clickable."""
    import html
    out, pos = [], 0
    for m in _LINK.finditer(text or ""):
        label, url, bare = m.group(1), m.group(2), m.group(3)
        tail = ""
        if bare:
            url = bare
            while url and (url[-1] in _TRAILING or (url[-1] == ")" and url.count("(") < url.count(")"))):
                tail = url[-1] + tail
                url = url[:-1]
            label = url
        out.append(html.escape(text[pos:m.start()]))
        href = url if url.lower().startswith("http") else "https://" + url
        out.append(f'<a href="{html.escape(href, quote=True)}" style="color:#63b3ed">{html.escape(label)}</a>'
                   + html.escape(tail))
        pos = m.end()
    out.append(html.escape((text or "")[pos:]))
    return "".join(out).replace("\n", "<br>")
