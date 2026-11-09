"""Gemini responseSchema definitions for each writing agent's structured output."""
from jarvis_books.craft.structures import STRUCTURES

S = {"type": "STRING"}
I = {"type": "INTEGER"}


def _arr(items: dict) -> dict:
    return {"type": "ARRAY", "items": items}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "OBJECT", "properties": props, "required": required or list(props)}


BOOK_PLAN = _obj({
    "title": S, "genre": S, "logline": S, "premise": S, "audience": S, "pov": S, "tense": S,
    "structure": {"type": "STRING", "enum": list(STRUCTURES)},
    "themes": _arr(S), "synopsis": S, "style_notes": S,
    "characters": _arr(_obj({"name": S, "role": S, "summary": S, "want": S, "need": S, "flaw": S, "arc": S,
                             "voice": S})),
    "locations": _arr(_obj({"name": S, "summary": S})),
    "threads": _arr(_obj({"name": S, "summary": S})),
})

OUTLINE = _obj({"chapters": {"type": "ARRAY", "minItems": 1, "items": _obj(
    {"number": I, "title": S, "outline": S, "beat": S, "target_words": I})}})

CHAPTER_PLAN = _obj({
    "scenes": {"type": "ARRAY", "minItems": 1, "items": _obj(
        {"pov": S, "setting": S, "goal": S, "conflict": S, "outcome": S, "beats": S, "target_words": I})},
    "hook": S,
})

REVIEW = _obj({
    "verdict": {"type": "STRING", "enum": ["accept", "revise"]},
    "issues": _arr(_obj({"severity": {"type": "STRING", "enum": ["minor", "major"]}, "kind": S,
                         "description": S, "fix": S})),
})

STATE_UPDATE = _obj({
    "summary": S,
    "character_updates": _arr(_obj({"name": S, "summary": S, "status": S})),
    "new_entities": _arr(_obj({"kind": {"type": "STRING", "enum": ["character", "location", "item", "rule", "event"]},
                               "name": S, "summary": S})),
    "open_threads": _arr(_obj({"name": S, "summary": S})),
    "resolved_threads": _arr(S),
})
