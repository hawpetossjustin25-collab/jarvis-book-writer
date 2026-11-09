"""Deterministic continuity checks (free, instant) that run before the AI editor."""
from __future__ import annotations

import collections
import re
from dataclasses import dataclass

from jarvis_books.storage.models import BibleEntry
from jarvis_books.text import word_count

_COMMON = set("""
The A An And But Or If Then When While After Before As At By For From In Into Of On Onto Over Under With Without
He She It They We You I His Her Its Their Our Your My Me Him Them Us This That These Those There Here What Who
Why How Where Which Yes No Not Now Just Still Even Only Maybe Perhaps Then Once Twice Every Each All Some Most Mr
Mrs Ms Dr Chapter God Oh Okay OK Hey Well So Too Very Inside Outside Behind Above Below Across Through Around
Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July August
September October November December
""".split())

_PRESENT = re.compile(r"\b(says|asks|walks|looks|turns|is|am|are)\b", re.I)
_PAST = re.compile(r"\b(said|asked|walked|looked|turned|was|were)\b", re.I)


@dataclass
class Finding:
    severity: str
    kind: str
    description: str


def unknown_names(text: str, bible: list[BibleEntry], min_count: int = 2) -> list[str]:
    known = set()
    for e in bible:
        for part in re.findall(r"[A-Z][a-z']+", e.name):
            known.add(part)
        for alias in (e.data.get("aliases") or []) if isinstance(e.data.get("aliases"), list) else []:
            known.update(re.findall(r"[A-Z][a-z']+", str(alias)))
    counts: collections.Counter = collections.Counter()
    for sentence in re.split(r"(?<=[.!?\"\u201d])\s+", text):
        tokens = re.findall(r"\b[A-Z][a-z']{2,}\b", sentence)
        first = re.match(r"\W*([A-Z][a-z']+)", sentence)
        for i, tok in enumerate(tokens):
            if i == 0 and first and first.group(1) == tok:
                continue  # sentence-initial capital is not evidence of a name
            if tok not in _COMMON and tok not in known:
                counts[tok.removesuffix("'s")] += 1
    return [name for name, n in counts.most_common() if n >= min_count]


def repeated_phrases(text: str, n: int = 4, threshold: int = 3) -> list[str]:
    words = re.findall(r"[a-z']+", text.lower())
    grams = collections.Counter(" ".join(words[i:i + n]) for i in range(len(words) - n + 1))
    stop = {"he said to her", "she said to him", "out of the room", "at the same time", "for the first time"}
    return [g for g, c in grams.most_common(10) if c >= threshold and g not in stop]


def check_chapter(text: str, bible: list[BibleEntry], target_words: int, tense: str = "") -> list[Finding]:
    findings: list[Finding] = []
    words = word_count(text)
    if target_words:
        if words < target_words * 0.6:
            findings.append(Finding("major", "length", f"chapter is {words} words; target is about {target_words}"))
        elif words > target_words * 1.7:
            findings.append(Finding("minor", "length", f"chapter is {words} words; target is about {target_words}"))
    lowered = text.lower()
    for e in bible:
        status = str(e.data.get("status", "")).lower()
        if e.kind == "character" and status in ("dead", "deceased", "killed"):
            first = e.name.split()[0]
            if re.search(rf"\b{re.escape(first)}\b\s+(said|says|walked|walks|smiled|smiles|replied|nodded)", text):
                findings.append(Finding("major", "continuity", f"{e.name} is dead in the story bible but acts in this chapter"))
    t = (tense or "").lower()
    present, past = len(_PRESENT.findall(text)), len(_PAST.findall(text))
    if t.startswith("past") and present > past * 1.5 and present > 20:
        findings.append(Finding("major", "tense", "book is past tense but this chapter reads as present tense"))
    if t.startswith("present") and past > present * 1.5 and past > 20:
        findings.append(Finding("major", "tense", "book is present tense but this chapter reads as past tense"))
    for phrase in repeated_phrases(text):
        findings.append(Finding("minor", "repetition", f"the phrase '{phrase}' repeats"))
    if "as an ai" in lowered or "language model" in lowered:
        findings.append(Finding("major", "artifact", "model commentary leaked into the prose"))
    if re.search(r"^#+\s|\*\*", text, re.M):
        findings.append(Finding("minor", "format", "markdown formatting found in manuscript text"))
    return findings
