"""Who says what: turns a passage into narrator and character lines for multi-voice narration.

The passage is split locally into narration and quoted speech, so the spoken words always match the book
exactly; Gemini only labels each quote with its speaker and a short delivery direction (whispering, angry...).
Passages without dialogue never call Gemini. Labels are cached per passage, so only edited passages are redone.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Optional

from jarvis_books.audio.voices import AGES, GENDERS, NARRATOR, assign_voices
from jarvis_books.errors import JarvisError
from jarvis_books.llm.errors import LLMError
from jarvis_books.llm.models import LLMRequest, PromptSection
from jarvis_books.storage.library import Library
from jarvis_books.text import text_hash, truncate_words

log = logging.getLogger(__name__)

_QUOTE = re.compile(r"\u201c[^\u201d\n]*(?:\u201d|(?=\n)|$)|\"[^\"\n]*(?:\"|(?=\n)|$)")
_STYLE_MAX = 60

LABEL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "quotes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "n": {"type": "INTEGER"}, "speaker": {"type": "STRING"}, "style": {"type": "STRING"}},
            "required": ["n", "speaker"]}},
        "new_characters": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING"}, "gender": {"type": "STRING", "enum": list(GENDERS)},
            "age": {"type": "STRING", "enum": list(AGES)}, "description": {"type": "STRING"}},
            "required": ["name", "gender", "age"]}},
    },
    "required": ["quotes"],
}

PROFILE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"characters": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "name": {"type": "STRING"}, "aliases": {"type": "ARRAY", "items": {"type": "STRING"}},
        "gender": {"type": "STRING", "enum": list(GENDERS)}, "age": {"type": "STRING", "enum": list(AGES)},
        "description": {"type": "STRING"}}, "required": ["name", "gender", "age"]}}},
    "required": ["characters"],
}

CASTING_DIRECTOR = (
    "You are an audiobook casting director. For every character, decide how their voice should sound: gender "
    "(male, female, or unknown), age group (child = under 13, teen = 13-19, adult, elder = about 60+), and a "
    "one-line voice description (for example 'gruff, slow, weary sea captain'). Use only what the story says or "
    "clearly implies. List aliases the text uses for them (nicknames, titles, surnames). Main characters first.")

DIALOGUE_LABELER = (
    "You label dialogue for a multi-voice audiobook. Each quote in the passage is marked [Q1], [Q2]... For every "
    "quote give the speaker's name exactly as written in the cast list, and a short delivery direction of at most "
    "six words for how the line is said, judged from the scene and the line itself (whispering, shouting, "
    "frightened, teasing, cheerful and loud, flat and tired...), or an empty string when it is plain speech. Use dialogue tags, turn-taking and context to decide who speaks. If a quote "
    "is not speech (a sign, a title, a thought read aloud by the narrator) use Narrator. If someone not in the "
    "cast speaks, use a short name for them and add them to new_characters with gender and age group.")


@dataclass(frozen=True)
class Line:
    speaker: str
    text: str
    style: str = ""


def split_spans(text: str) -> list[tuple[str, str]]:
    """[(kind, text)] with kind 'narration' or 'quote'; joining the texts gives back the passage exactly."""
    spans: list[tuple[str, str]] = []
    pos = 0
    for m in _QUOTE.finditer(text or ""):
        if m.start() > pos:
            spans.append(("narration", text[pos:m.start()]))
        spans.append(("quote", m.group(0)))
        pos = m.end()
    if pos < len(text or ""):
        spans.append(("narration", text[pos:]))
    return spans


def _clean_style(style: str) -> str:
    style = re.sub(r"\s+", " ", str(style or "")).strip().strip(".")
    return style[:_STYLE_MAX]


def merge_lines(spans: list[tuple[str, str]], speakers: dict[int, tuple[str, str]]) -> list[Line]:
    """Narration goes to the Narrator; consecutive pieces by the same voice and direction become one line."""
    lines: list[Line] = []
    q = 0
    for kind, text in spans:
        if kind == "quote":
            q += 1
            speaker, style = speakers.get(q, (NARRATOR, ""))
        else:
            speaker, style = NARRATOR, ""
        if not text.strip():
            if lines:  # keep the spacing inside the previous line so joins stay readable
                lines[-1] = Line(lines[-1].speaker, lines[-1].text + text, lines[-1].style)
            continue
        if lines and lines[-1].speaker == speaker and lines[-1].style == style:
            lines[-1] = Line(speaker, lines[-1].text + text, style)
        else:
            lines.append(Line(speaker, text, style))
    return [Line(ln.speaker, ln.text.strip(), ln.style) for ln in lines if ln.text.strip()]


_SPEECH = (r"said|says|asked|asks|whispered|whispers|replied|replies|answered|shouted|yelled|called|cried|"
           r"muttered|murmured|added|continued|snapped|told|begged|demanded|insisted|growled|hissed|breathed|"
           r"admitted|agreed|warned|explained|began|went on|repeated|protested|stammered|teased|offered|pleaded|"
           r"screamed|barked|grumbled|whimpered|mumbled|corrected|announced|declared|exclaimed|responded|spoke")
_WHO = r"(?:(?i:he|she|they|I|we|you|it|the\s+[a-z]+(?:\s+[a-z]+)?)|[A-Z][\w'\u2019-]*(?:\s+[A-Z][\w'\u2019-]*){0,2})"
_TO = r"(?:\s+(?i:to|at)\s+(?:(?i:him|her|them|me|us|you|the\s+[a-z]+)|[A-Z][\w'\u2019-]*))?"
_TAG = re.compile(rf"^[\s,.;:\u2014\u2013-]*(?:(?i:{_SPEECH})\s+{_WHO}|{_WHO}\s+(?i:{_SPEECH}))"
                  rf"(?:\s+(?i:[a-z]+ly))?{_TO}(?:\s+(?i:[a-z]+ly))?[\s,.!?;:\u2014\u2013-]*$")


def is_plain_tag(text: str) -> bool:
    """True for a bare speech tag ('said Tom.', 'she whispered softly,') that only tells who is talking."""
    return bool(_TAG.match(text or ""))


def trim_tags(passages: list[list[Line]], limit: int = 3, counts: Optional[dict] = None) -> list[list[Line]]:
    """Narrate only each character's first `limit` bare speech tags; after that the voices say who is talking.

    Only the audio changes: tags with action in them ('Lily asked, wide-eyed') are always read, and the book's
    text is never touched. `counts` carries on between calls (one chapter = one count).
    """
    counts = {} if counts is None else counts
    out = []
    for lines in passages:
        kept: list[Line] = []
        for i, ln in enumerate(lines):
            if ln.speaker == NARRATOR and is_plain_tag(ln.text):
                prev = lines[i - 1].speaker if i > 0 else NARRATOR
                nxt = lines[i + 1].speaker if i + 1 < len(lines) else NARRATOR
                post = ln.text[:1].islower() or ln.text[:1] in ",;\u2014\u2013-"
                who = prev if prev != NARRATOR and (post or nxt == NARRATOR) else nxt
                if who != NARRATOR:
                    counts[who] = counts.get(who, 0) + 1
                    if counts[who] > limit:
                        continue
            if kept and kept[-1].speaker == ln.speaker != NARRATOR and kept[-1].style == ln.style \
                    and i > 0 and lines[i - 1].speaker == NARRATOR:  # a dropped tag joined two pieces of one speech
                kept[-1] = Line(ln.speaker, f"{kept[-1].text} {ln.text}", ln.style)
                continue
            kept.append(ln)
        out.append(kept)
    return out


class VoiceDirector:
    """Keeps each book's voice cast and turns passages into narrator/character lines."""

    def __init__(self, library: Library, engine, settings: Callable[[], dict]):
        self.library = library
        self.engine = engine
        self.settings = settings

    # ---- the cast -----------------------------------------------------------------
    def narrator(self, book_id: str) -> dict:
        member = self.library.get_cast_member(book_id, NARRATOR)
        if member is None:
            s = self.settings()
            member = self.library.upsert_cast_member(book_id, NARRATOR, voice=s["voice"], edge_voice=s["edge_voice"],
                                                     description="the book's narrator", ord=0)
        return member

    def cast(self, book_id: str) -> list[dict]:
        """The narrator first, then the characters (main characters first)."""
        self.narrator(book_id)
        return sorted(self.library.list_cast(book_id), key=lambda m: m["name"].lower() != NARRATOR.lower())

    def characters(self, book_id: str) -> list[dict]:
        return [m for m in self.cast(book_id) if m["name"].lower() != NARRATOR.lower()]

    def build_cast(self, book_id: str) -> list[dict]:
        """Ask Gemini how every story-bible character should sound, then give each one a fitting voice."""
        self.narrator(book_id)
        entries = self.library.list_bible(book_id, "character")
        if entries:
            book = self.library.get_book(book_id)
            listing = "\n".join(f"- {e.name}: {truncate_words(e.summary, 60)}" for e in entries)
            data = self.engine.generate(LLMRequest(
                role="summarizer", system=CASTING_DIRECTOR, json_schema=PROFILE_SCHEMA, temperature=0.2,
                purpose="voice casting", instruction="Cast every character listed.",
                sections=[PromptSection("Book", f"{book.title} ({book.genre}). {book.premise}", priority=80),
                          PromptSection("Characters", listing, required=True)])).data or {}
            known = {e.name.lower() for e in entries}
            for c in data.get("characters") or []:
                name = (c.get("name") or "").strip()
                if not name or name.lower() == NARRATOR.lower():
                    continue
                if name.lower() not in known and not any(name.lower() in k or k in name.lower() for k in known):
                    continue  # only characters that exist in the story bible
                self._add_profile(book_id, name, c)
        self._assign(book_id)
        self.library.clear_voice_scripts(book_id)
        return self.cast(book_id)

    def _add_profile(self, book_id: str, name: str, c: dict) -> dict:
        current = self.library.get_cast_member(book_id, name)
        gender = c.get("gender") if c.get("gender") in GENDERS else "unknown"
        age = c.get("age") if c.get("age") in AGES else "adult"
        fields = {"description": truncate_words(str(c.get("description") or ""), 25),
                  "aliases": [a.strip() for a in (c.get("aliases") or []) if str(a).strip()][:6]}
        if current is None or not current["locked"]:
            fields.update(gender=gender, age=age)
            if current is not None and (current["gender"], current["age"]) != (gender, age):
                fields.update(voice="", edge_voice="", style="")  # re-cast to fit the new profile
        return self.library.upsert_cast_member(book_id, name, **fields)

    def _assign(self, book_id: str) -> None:
        narrator = self.narrator(book_id)
        members = self.characters(book_id)
        for m in assign_voices(members, narrator["voice"], narrator["edge_voice"]):
            before = next(x for x in members if x["name"] == m["name"])
            changed = {k: m[k] for k in ("voice", "edge_voice", "style") if m[k] != before[k]}
            if changed:
                self.library.upsert_cast_member(book_id, m["name"], **changed)

    def _resolve(self, book_id: str, name: str) -> Optional[str]:
        name = (name or "").strip().lower()
        if not name:
            return None
        if name == NARRATOR.lower():
            return NARRATOR
        for m in self.cast(book_id):
            if name == m["name"].lower() or name in (a.lower() for a in m["aliases"]):
                return m["name"]
        for m in self.cast(book_id):  # "Captain Reyes" vs "Reyes"
            first = m["name"].lower()
            if first in name.split() or name in first.split():
                return m["name"]
        return None

    # ---- lines ----------------------------------------------------------------------
    def script(self, book_id: str, text: str, before: str = "") -> list[Line]:
        """Narrator and character lines for one passage, word for word as written."""
        spans = split_spans(text)
        quotes = [t for kind, t in spans if kind == "quote"]
        if not quotes or self.engine is None:
            return merge_lines(spans, {})
        key = text_hash(text)
        labels = self.library.get_voice_script(book_id, key)
        if labels is None:
            try:
                labels = self._label(book_id, spans, before)
            except (LLMError, JarvisError) as exc:  # no speech labels right now: the narrator reads it, try again next time
                log.info("dialogue labels unavailable (%s); narrator reads this passage", exc)
                return merge_lines(spans, {})
            self.library.save_voice_script(book_id, key, labels)
        speakers = {}
        for item in labels:
            who = self._resolve(book_id, item.get("speaker", "")) or NARRATOR
            speakers[int(item["n"])] = (who, _clean_style(item.get("style", "")) if who != NARRATOR else "")
        return merge_lines(spans, speakers)

    def tag_limit(self) -> int:
        try:
            return max(0, int(self.settings().get("dialogue_tags", 3)))
        except (TypeError, ValueError):
            return 3

    def chapter_lines(self, book_id: str, passages: list[str]) -> list[list[Line]]:
        """Lines for every passage of one chapter, with repeated 'said Tom' tags left to the voices."""
        scripted, before = [], ""
        for text in passages:
            scripted.append(self.script(book_id, text, before))
            before = text
        return trim_tags(scripted, self.tag_limit())

    def _label(self, book_id: str, spans: list[tuple[str, str]], before: str) -> list[dict]:
        marked, n = [], 0
        for kind, t in spans:
            if kind == "quote":
                n += 1
                marked.append(f"[Q{n}]{t}")
            else:
                marked.append(t)
        cast = "\n".join(f"- {m['name']}" + (f" (also: {', '.join(m['aliases'])})" if m["aliases"] else "")
                         + (f": {m['description']}" if m["description"] else "") for m in self.characters(book_id))
        sections = [PromptSection("Cast", cast or "(no characters known yet)", required=True),
                    PromptSection("Passage", "".join(marked), required=True)]
        if before.strip():
            sections.insert(1, PromptSection("Just before this passage", truncate_words(before, 150, from_end=True),
                                             priority=30, trim_from="start"))
        data = self.engine.generate(LLMRequest(
            role="summarizer", system=DIALOGUE_LABELER, json_schema=LABEL_SCHEMA, temperature=0.1,
            purpose="dialogue labels", sections=sections,
            instruction=f"Label all {n} quotes, Q1 to Q{n}.")).data or {}
        added = False
        for c in data.get("new_characters") or []:
            name = (c.get("name") or "").strip()
            if name and name.lower() != NARRATOR.lower() and self._resolve(book_id, name) is None:
                self._add_profile(book_id, name[:60], c)
                added = True
        if added:
            self._assign(book_id)
        labels = []
        for item in data.get("quotes") or []:
            try:
                q = int(item.get("n"))
            except (TypeError, ValueError):
                continue
            if 1 <= q <= n:
                labels.append({"n": q, "speaker": str(item.get("speaker") or ""), "style": str(item.get("style") or "")})
        return labels
