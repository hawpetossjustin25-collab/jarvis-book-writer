"""OS2's reading tools: plain functions that turn page text into sentences, entities, claims, a category and a
short summary. They run offline, need no model, and never guess: a claim is only what a sentence plainly states.

Page text is untrusted data. Nothing here follows or acts on what a page says; it is only measured and quoted.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Optional

STOP = frozenset("""a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have having he
her here hers him his how i if in into is it its itself just me more most my no nor not now of off on once only or
other our out over own same she should so some such than that the their them then there these they this those
through to too under until up very was we were what when where which while who whom why will with would you your
""".split())
MONTHS = ("january february march april may june july august september october november december "
          "jan feb mar apr jun jul aug sep sept oct nov dec").split()
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
SCALE = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "mm": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9,
         "trillion": 1e12, "t": 1e12}
ORG_WORDS = ("inc", "inc.", "llc", "ltd", "corp", "corporation", "company", "co.", "university", "college",
             "department", "agency", "association", "foundation", "institute", "bank", "group", "commission",
             "council", "office", "authority", "bureau", "school", "society", "court")
PLACE_WORDS = ("county", "city", "state", "river", "lake", "mountain", "island", "park", "street", "avenue",
               "township", "parish", "province", "district")
US_STATES = frozenset("""alabama alaska arizona arkansas california colorado connecticut delaware florida georgia
hawaii idaho illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan minnesota
mississippi missouri montana nebraska nevada ohio oklahoma oregon pennsylvania tennessee texas utah vermont
virginia washington wisconsin wyoming""".split()) | {"new york", "new jersey", "new mexico", "new hampshire",
                                                      "north carolina", "south carolina", "north dakota",
                                                      "south dakota", "rhode island", "west virginia"}
# Predicates with one true value at a time: two sources giving different values for these is a contradiction.
FUNCTIONAL = ("founded", "established", "born", "died", "released", "launched", "created", "opened", "population",
              "price", "cost", "tax", "rate", "area", "height", "length", "revenue", "employees", "capital",
              "headquarters", "starting bid", "minimum bid", "age", "version", "elevation", "salary")

_SENT_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")
_ABBREV = re.compile(r"\b(Mr|Mrs|Ms|Dr|St|Jr|Sr|Inc|Ltd|Co|Corp|vs|etc|e\.g|i\.e|U\.S|No)\.$")
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
MONEY_RE = re.compile(rf"(?:US)?\$\s?(?:{_NUM})(?:\s?(?:thousand|million|billion|trillion|bn|mm|[kmbt])\b)?", re.I)
PERCENT_RE = re.compile(rf"(?:{_NUM})\s?(?:%|percent\b)", re.I)
DATE_RE = re.compile(rf"\b(?:(?:{_MONTH})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}|\d{{1,2}}\s+(?:{_MONTH})\.?\s+\d{{4}}|"
                     rf"\d{{4}}-\d{{2}}-\d{{2}}|(?:{_MONTH})\.?\s+\d{{4}})\b", re.I)
YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_CAP = r"(?:[A-Z][a-zA-Z0-9&'’.-]*|of|the|and|de|for|&)"
NAME_RE = re.compile(rf"\b[A-Z][a-zA-Z0-9&'’.-]*(?:\s+{_CAP})*")


@dataclass(frozen=True)
class Entity:
    name: str
    kind: str  # name, org, place, date, money, percent, email

    @property
    def norm(self) -> str:
        return norm_text(self.name)


@dataclass(frozen=True)
class Claim:
    subject: str
    predicate: str
    value: str
    sentence: str

    @property
    def subject_norm(self) -> str:
        return norm_text(self.subject)

    @property
    def value_norm(self) -> str:
        return norm_value(self.value)


def norm_text(text: str) -> str:
    text = re.sub(r"[“”\"'’`]", "", (text or "").lower())
    text = re.sub(r"^(the|a|an)\s+", "", text.strip())
    text = re.sub(r"[^\w\s&$%.-]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" .-")


def number(text: str) -> Optional[float]:
    """The amount a value states: '$1.2 million' -> 1200000, '12%' -> 12, '3,400' -> 3400; None when it has none."""
    m = re.search(rf"({_NUM})\s?(thousand|million|billion|trillion|bn|mm|[kmbt])?\b", text or "", re.I)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    if m.group(2):
        value *= SCALE[m.group(2).lower()]
    return value


def norm_value(value: str) -> str:
    """A comparable form of a claimed value: dates as YYYY[-MM[-DD]], amounts as numbers, other text lowercased."""
    text = (value or "").strip()
    date = parse_date(text)
    if date:
        return date
    if MONEY_RE.fullmatch(text.strip()) or PERCENT_RE.fullmatch(text.strip()) or re.fullmatch(
            rf"(?:about |around |roughly |nearly |over |more than |almost )?(?:{_NUM})(?:\s?\w+)?", text, re.I):
        amount = number(text)
        if amount is not None:
            return f"{amount:g}"
    return norm_text(text)


def parse_date(text: str) -> Optional[str]:
    text = (text or "").strip()
    m = re.fullmatch(r"(?:in |on )?(\d{4})-(\d{2})-(\d{2})", text, re.I)
    if m:
        return "-".join(m.groups())
    m = re.fullmatch(rf"(?:in |on )?({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", text, re.I)
    if m:
        return f"{m.group(3)}-{_month(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.fullmatch(rf"(?:in |on )?(\d{{1,2}})\s+({_MONTH})\.?\s+(\d{{4}})", text, re.I)
    if m:
        return f"{m.group(3)}-{_month(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.fullmatch(rf"(?:in )?({_MONTH})\.?\s+(\d{{4}})", text, re.I)
    if m:
        return f"{m.group(2)}-{_month(m.group(1)):02d}"
    m = re.fullmatch(r"(?:in )?(1[5-9]\d{2}|20\d{2})", text, re.I)
    return m.group(1) if m else None


def _month(name: str) -> int:
    name = name.lower().rstrip(".")[:3]
    return [m[:3] for m in MONTHS[:12]].index(name) + 1


def same_value(a: str, b: str) -> bool:
    """Whether two normalized values agree: dates match to the precision both give; amounts within 2%."""
    if a == b:
        return True
    if re.fullmatch(r"\d{4}(-\d{2}){0,2}", a) and re.fullmatch(r"\d{4}(-\d{2}){0,2}", b):
        return a.startswith(b) or b.startswith(a)
    try:
        x, y = float(a), float(b)
    except ValueError:
        return False
    return abs(x - y) <= 0.02 * max(abs(x), abs(y))


# ---- sentences, entities ------------------------------------------------------------------------------------
def sentences(text: str, limit: int = 2000) -> list[str]:
    out: list[str] = []
    for block in re.split(r"\n\s*\n|\n(?=[A-Z#*-])", text or ""):
        block = re.sub(r"\s+", " ", block).strip()
        if not block:
            continue
        parts = _SENT_END.split(block)
        merged: list[str] = []
        for part in parts:
            if merged and _ABBREV.search(merged[-1]):
                merged[-1] += " " + part
            else:
                merged.append(part)
        out.extend(p.strip() for p in merged if 12 <= len(p.strip()) <= 600)
        if len(out) >= limit:
            break
    return out[:limit]


def entity_kind(name: str) -> str:
    low = name.lower()
    words = low.split()
    if words and words[-1].rstrip(",") in ORG_WORDS or words and words[0] == "university":
        return "org"
    if low in US_STATES or words and words[-1] in PLACE_WORDS:
        return "place"
    return "name"


def entities(text: str) -> list[Entity]:
    """Named things, dates, amounts and emails in the order they appear (each once)."""
    found: list[Entity] = []
    seen: set[tuple[str, str]] = set()

    def add(name: str, kind: str) -> None:
        name = re.sub(r"^(The|A|An)\s+", "", re.sub(r"(?:'s|’s)$", "", name.strip(" ,.;:")))
        if kind in ("name", "org", "place"):
            kind = entity_kind(name)
        key = (kind, norm_text(name))
        if len(name) >= 2 and key[1] and key not in seen:
            seen.add(key)
            found.append(Entity(name, kind))

    for sent in sentences(text):
        taken: list[tuple[int, int]] = []
        for kind, rx in (("email", EMAIL_RE), ("money", MONEY_RE), ("percent", PERCENT_RE), ("date", DATE_RE)):
            for m in rx.finditer(sent):
                if not any(a <= m.start() < b for a, b in taken):
                    taken.append(m.span())
                    add(m.group(0), kind)
        for m in NAME_RE.finditer(sent):
            if any(a <= m.start() < b for a, b in taken):
                continue
            name = re.sub(r"(\s+(of|the|and|de|for|&))+$", "", m.group(0)).strip()
            words = name.split()
            if m.start() == 0 and len(words) == 1:
                continue  # one capitalised word starting a sentence is usually just the first word
            if all(w.lower() in STOP or w.lower().rstrip(".") in MONTHS for w in words):
                continue
            add(name, entity_kind(name))
    return found


# ---- claims -------------------------------------------------------------------------------------------------
_SUBJ = r"(?P<s>(?:The |the )?[A-Z][\w&'’.-]*(?:\s+(?:[A-Z][\w&'’.-]*|\d[\w-]*|of|the|and|de|for|&)){0,7})"
_VALUE = r"(?P<o>[^;:()]{1,120}?)"
_END = r"(?=\s*(?:[.;!?](?!\d)|,\s|\s+(?:and|but|which|while|where|after|before|because)\s|$))"
_EVENT_END = r"(?=\s*(?:[.;!?](?!\d)|,\s|\s+(?:and|but|by|in|at|as|with|from|under|after|when|which)\s|$))"
_EVENT = r"(?P<p>founded|established|born|died|released|launched|created|opened|incorporated)"
_ATTR = (r"(?P<p>population|price|cost|tax rate|interest rate|tax|rate|area|height|length|revenue|elevation|capital|headquarters|"
         r"starting bid|minimum bid|salary|age|version|number of employees)")
CLAIM_PATTERNS = (
    re.compile(rf"{_SUBJ},?\s+(?:was|were|is|has been)\s+(?:first\s+|officially\s+)?{_EVENT}\s+(?:in|on)\s+"
               rf"(?P<o>(?:{DATE_RE.pattern})|\d{{4}}){_EVENT_END}", re.I),
    re.compile(rf"(?:the\s+)?{_ATTR}\s+of\s+{_SUBJ}\s+(?:is|was|stands at|reached|totals|totaled|is about|"
               rf"is around|was about)\s+{_VALUE}{_END}", re.I),
    re.compile(rf"{_SUBJ}(?:'s|’s)\s+{_ATTR}\s+(?:is|was|stands at|reached|totals|totaled)\s+{_VALUE}{_END}", re.I),
    re.compile(rf"{_SUBJ}\s+(?:has|had)\s+a\s+{_ATTR}\s+of\s+{_VALUE}{_END}", re.I),
    re.compile(rf"{_SUBJ}\s+(?:costs|cost)\s+(?P<o>{MONEY_RE.pattern}){_END}", re.I),
    re.compile(rf"{_SUBJ}\s+(?:employs|employed)\s+(?P<o>(?:about |around |over |more than |nearly )?(?:{_NUM})"
               rf"(?:\s?(?:thousand|million))?)\s+(?:people|employees|staff|workers)", re.I),
    re.compile(rf"{_SUBJ}\s+(?:is|was|are|were)\s+(?P<o>(?:a|an|the)\s+[^,;:()]{{2,100}}?){_END}"),
)
_PRED_FOR = {"number of employees": "employees"}


def claims(text: str, limit: int = 300) -> list[Claim]:
    """What sentences plainly state: '<subject> was founded in 1998', 'the population of X is 3,400', 'X is a Y'."""
    out: list[Claim] = []
    seen: set[tuple[str, str, str]] = set()
    for sent in sentences(text):
        for i, rx in enumerate(CLAIM_PATTERNS):
            for m in rx.finditer(sent):
                subject = re.sub(r"^(the)\s+", "", m.group("s").strip(), flags=re.I).strip(" ,")
                if not subject or subject.lower() in STOP or len(subject) < 2:
                    continue
                groups = m.groupdict()
                if i == len(CLAIM_PATTERNS) - 1:
                    predicate = "is"
                elif groups.get("p"):
                    predicate = groups["p"].lower()
                else:
                    predicate = {4: "cost", 5: "employees"}[i]
                predicate = _PRED_FOR.get(predicate, predicate)
                value = m.group("o").strip(" ,.")
                if not value or (predicate in FUNCTIONAL and predicate not in ("capital", "headquarters")
                                 and number(value) is None and parse_date(value) is None):
                    continue
                key = (norm_text(subject), predicate, norm_value(value))
                if key in seen:
                    continue
                seen.add(key)
                out.append(Claim(subject, predicate, value, sent[:600]))
                if len(out) >= limit:
                    return out
    return out


# ---- category, summary --------------------------------------------------------------------------------------
CATEGORIES = {
    "government": ("county", "ordinance", "department of", "official website", "state of", "tax commissioner",
                   "municipal", "public notice", "board of", "city council"),
    "news": ("reported", "according to", "said on", "breaking", "press release", "reporter", "news", "updated"),
    "documentation": ("install", "function", "parameter", "returns", "example", "api", "documentation",
                      "tutorial", "class", "module", "usage"),
    "commerce": ("price", "add to cart", "buy", "shipping", "in stock", "order", "checkout", "sale", "discount"),
    "academic": ("abstract", "doi", "journal", "et al", "study", "university", "research", "citation"),
    "forum": ("reply", "posted by", "thread", "upvote", "comments", "joined", "member since", "quote"),
    "reference": ("encyclopedia", "wikipedia", "is a", "refers to", "may refer to", "history", "etymology"),
}
_URL_HINTS = {"government": (".gov", ".mil", ".us/"), "news": ("/news", "news.", "/article"),
              "documentation": ("docs.", "/docs", "/doc/", "readthedocs", "/api/", "/reference"),
              "academic": (".edu", "arxiv.org", "doi.org", "/abs/", "scholar"),
              "forum": ("forum", "reddit.com", "stackoverflow.com", "/thread", "/t/"),
              "reference": ("wikipedia.org", "britannica.com"), "commerce": ("/shop", "/product", "/cart", "store.")}


def classify(url: str, title: str, text: str) -> tuple[str, dict]:
    """The page's kind (government, news, documentation, commerce, academic, forum, reference, or general)."""
    low_url = (url or "").lower()
    body = f"{title or ''} {(text or '')[:6000]}".lower()
    scores: dict[str, float] = {}
    for cat, words in CATEGORIES.items():
        score = sum(min(body.count(w), 5) for w in words) / 5.0
        score += 3.0 * sum(1 for hint in _URL_HINTS.get(cat, ()) if hint in low_url)
        if score:
            scores[cat] = round(score, 2)
    if not scores or max(scores.values()) < 1.0:
        return "general", scores
    return max(scores, key=scores.get), scores


def keywords(text: str) -> Counter:
    return Counter(w for w in re.findall(r"[a-z][a-z0-9'-]{2,}", (text or "").lower()) if w not in STOP)


def summarize(text: str, max_sentences: int = 3, focus: str = "") -> str:
    """The page's most central sentences in their original order (extractive: only the page's own words)."""
    sents = [s for s in sentences(text, 400) if 40 <= len(s) <= 400]
    if not sents:
        return (text or "").strip()[:300]
    freq = keywords(" ".join(sents))
    focus_words = set(keywords(focus))
    top = freq.most_common(1)[0][1] if freq else 1

    def score(i: int, s: str) -> float:
        words = keywords(s)
        if not words:
            return 0.0
        base = sum(freq[w] / top for w in words) / (len(words) ** 0.5)
        return base + 1.5 * len(focus_words & set(words)) + (0.3 if i < 3 else 0.0)

    best = sorted(range(len(sents)), key=lambda i: score(i, sents[i]), reverse=True)[:max_sentences]
    return " ".join(sents[i] for i in sorted(best))
