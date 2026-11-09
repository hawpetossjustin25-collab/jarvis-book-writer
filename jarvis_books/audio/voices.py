"""Voice catalog and automatic voice casting for multi-voice narration.

Every book has a narrator plus one voice per speaking character. Gemini voices are the main voices; each
character also gets a matching Microsoft Edge voice so a book keeps its cast when Gemini's daily speech quota
runs out. Edge voices may carry a pitch and rate change ("en-US-BrianNeural|+40Hz|+8%") to sound like a boy.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable, Optional

NARRATOR = "Narrator"
GENDERS = ("male", "female", "unknown")
AGES = ("child", "teen", "adult", "elder")

# name -> (gender, character of the voice)
GEMINI_CATALOG: dict[str, tuple[str, str]] = {
    "Zephyr": ("female", "bright"), "Kore": ("female", "firm"), "Leda": ("female", "youthful"),
    "Aoede": ("female", "breezy"), "Callirrhoe": ("female", "easy-going"), "Autonoe": ("female", "bright"),
    "Despina": ("female", "smooth"), "Erinome": ("female", "clear"), "Laomedeia": ("female", "upbeat"),
    "Achernar": ("female", "soft"), "Gacrux": ("female", "mature"), "Pulcherrima": ("female", "forward"),
    "Vindemiatrix": ("female", "gentle"), "Sulafat": ("female", "warm"),
    "Puck": ("male", "upbeat"), "Charon": ("male", "informative"), "Fenrir": ("male", "excitable"),
    "Orus": ("male", "firm"), "Enceladus": ("male", "breathy"), "Iapetus": ("male", "clear"),
    "Umbriel": ("male", "easy-going"), "Algieba": ("male", "smooth"), "Algenib": ("male", "gravelly"),
    "Rasalgethi": ("male", "informative"), "Alnilam": ("male", "firm"), "Schedar": ("male", "even"),
    "Achird": ("male", "friendly"), "Zubenelgenubi": ("male", "casual"), "Sadachbia": ("male", "lively"),
    "Sadaltager": ("male", "knowledgeable"),
}

# Best-fitting Gemini voices first for each (gender, age). Children are directed with a style ("a girl
# about eight"); the youthful voices carry that best.
_GEMINI_FIT: dict[tuple[str, str], tuple[str, ...]] = {
    ("female", "child"): ("Leda", "Achernar", "Laomedeia", "Zephyr", "Autonoe"),
    ("male", "child"): ("Leda", "Puck", "Laomedeia", "Sadachbia", "Zephyr"),
    ("female", "teen"): ("Laomedeia", "Autonoe", "Zephyr", "Leda", "Erinome"),
    ("male", "teen"): ("Puck", "Sadachbia", "Fenrir", "Achird", "Umbriel"),
    ("female", "adult"): ("Kore", "Aoede", "Despina", "Erinome", "Callirrhoe", "Pulcherrima", "Sulafat",
                          "Autonoe", "Zephyr", "Achernar"),
    ("male", "adult"): ("Orus", "Iapetus", "Algieba", "Alnilam", "Achird", "Umbriel", "Enceladus", "Zubenelgenubi",
                        "Fenrir", "Puck", "Schedar"),
    ("female", "elder"): ("Gacrux", "Vindemiatrix", "Sulafat", "Despina"),
    ("male", "elder"): ("Algenib", "Rasalgethi", "Sadaltager", "Schedar", "Charon"),
}

_EDGE_FIT: dict[tuple[str, str], tuple[str, ...]] = {
    ("female", "child"): ("en-US-AnaNeural", "en-GB-MaisieNeural"),
    ("male", "child"): ("en-US-BrianNeural|+40Hz|+8%", "en-US-AndrewNeural|+45Hz|+8%"),
    ("female", "teen"): ("en-GB-MaisieNeural|-10Hz|+0%", "en-US-EmmaNeural|+15Hz|+5%", "en-US-AnaNeural|-20Hz|+0%"),
    ("male", "teen"): ("en-US-BrianNeural|+20Hz|+5%", "en-US-AndrewNeural|+20Hz|+5%"),
    ("female", "adult"): ("en-US-AvaNeural", "en-US-EmmaNeural", "en-GB-SoniaNeural", "en-US-JennyNeural",
                          "en-US-AriaNeural", "en-GB-LibbyNeural", "en-US-MichelleNeural"),
    ("male", "adult"): ("en-US-BrianNeural", "en-GB-RyanNeural", "en-US-GuyNeural", "en-US-EricNeural",
                        "en-US-ChristopherNeural", "en-GB-ThomasNeural", "en-US-SteffanNeural", "en-US-RogerNeural"),
    ("female", "elder"): ("en-US-MichelleNeural|-8Hz|-6%", "en-GB-SoniaNeural|-10Hz|-6%"),
    ("male", "elder"): ("en-US-ChristopherNeural|-6Hz|-8%", "en-US-RogerNeural|-8Hz|-8%"),
}

_LABELS = {("male", "child"): "Boy", ("female", "child"): "Girl", ("male", "teen"): "Teen boy",
           ("female", "teen"): "Teen girl", ("male", "adult"): "Man", ("female", "adult"): "Woman",
           ("male", "elder"): "Older man", ("female", "elder"): "Older woman"}


def who(gender: str, age: str) -> str:
    """'Boy', 'Woman', 'Older man'... for the cast list."""
    return _LABELS.get((gender, age), {"child": "Child", "teen": "Teen", "elder": "Older person"}.get(age, "Adult"))


def default_style(gender: str, age: str) -> str:
    """Baseline direction that makes a voice play the character's age."""
    noun = {"male": "boy", "female": "girl"}.get(gender, "child")
    if age == "child":
        return f"a {noun} about eight years old"
    if age == "teen":
        return f"a teenage {noun}" if gender in ("male", "female") else "a teenager"
    if age == "elder":
        return {"male": "an older man", "female": "an older woman"}.get(gender, "an older person")
    return ""


def describe_voice(voice: str) -> str:
    gender, trait = GEMINI_CATALOG.get(voice, ("", ""))
    return f"{voice} ({trait} {gender})".replace("  ", " ") if gender else voice


def _fits(table: dict, gender: str, age: str) -> tuple[str, ...]:
    age = age if age in AGES else "adult"
    if gender in ("male", "female"):
        return table[(gender, age)]
    mixed: list[str] = []  # unknown gender: alternate between the two lists
    for pair in zip(table[("female", age)], table[("male", age)]):
        mixed.extend(pair)
    return tuple(mixed)


def _pick(candidates: Iterable[str], used: Counter, avoid: set) -> str:
    options = [c for c in candidates if c not in avoid] or list(candidates)
    return min(options, key=lambda c: (used[c], options.index(c)))


def assign_voices(members: list[dict], narrator_voice: str, narrator_edge: str) -> list[dict]:
    """Give every member without a voice the best-fitting free voice; members the author locked keep theirs.

    Members are dicts with name, gender, age, voice, edge_voice, style, locked. Earlier members (the main
    characters) get first pick; voices repeat only when the book has more characters than fitting voices.
    """
    used: Counter = Counter()
    edge_used: Counter = Counter()
    for m in members:
        if m.get("voice"):
            used[m["voice"]] += 1
        if m.get("edge_voice"):
            edge_used[m["edge_voice"]] += 1
    out = []
    for m in members:
        m = dict(m)
        if not m.get("locked") or not m.get("voice"):
            if not m.get("voice"):
                m["voice"] = _pick(_fits(_GEMINI_FIT, m.get("gender", ""), m.get("age", "")), used, {narrator_voice})
                used[m["voice"]] += 1
            if not m.get("edge_voice"):
                m["edge_voice"] = _pick(_fits(_EDGE_FIT, m.get("gender", ""), m.get("age", "")), edge_used,
                                        {narrator_edge})
                edge_used[m["edge_voice"]] += 1
            if not m.get("style"):
                m["style"] = default_style(m.get("gender", ""), m.get("age", ""))
        out.append(m)
    return out


def edge_matching(voice: str, gender: str = "", age: str = "") -> Optional[str]:
    """The first Edge voice that fits a character, for when the author picks a Gemini voice by hand."""
    if not gender:
        gender = GEMINI_CATALOG.get(voice, ("unknown", ""))[0]
    return _fits(_EDGE_FIT, gender, age or "adult")[0]


def parse_edge_voice(spec: str) -> tuple[str, str, str]:
    """'en-US-BrianNeural|+40Hz|+8%' -> (voice, pitch, rate)."""
    parts = (spec or "").split("|")
    voice = parts[0]
    pitch = parts[1] if len(parts) > 1 and parts[1] else "+0Hz"
    rate = parts[2] if len(parts) > 2 and parts[2] else "+0%"
    return voice, pitch, rate
