"""Training examples for Jarvis's tool brain, made from the app's own tools.

Each line of data/tool_templates.txt is `tool | request pattern | fixed arguments (JSON, optional)`. A pattern
has `{argument:pool}` slots filled from the pools below and `[a|b|]` choices (an empty choice drops the words).
The generator knows where each slot landed in the request, so the brain learns to copy names and numbers
straight out of what the author said. Requests in the fixed tool test set are never generated, so the test
stays a fair measure.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from jarvis_books.llm.tool_eval import ToolCase

TEMPLATES_PATH = Path(__file__).with_name("data") / "tool_templates.txt"
NO_TOOL = "(none)"

_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]
WORD_NUMBERS = {w: i for i, w in enumerate(_WORDS)} | {"a couple": 2, "a few": 3, "twenty": 20, "thirty": 30,
                                                       "forty": 40, "fifty": 50, "once": 1, "twice": 2}

BOOKS = ["Iron Coast", "The Last Orchard", "Midnight Ledger", "Glass River", "Atlanta Nights", "The Copper Key",
         "Saints of Peachtree", "Hollow Creek", "Starfall", "My Memoir", "The Quiet War", "Blue Flame",
         "Kings of Decatur", "The Salt Road", "Night Shift", "Ember and Ash", "The Long Game", "River of Glass",
         "Dragon Heart", "Southern Fire"]
TEXTS = ["the villain is the mayor's brother", "my wife's birthday is in March", "I like short chapters",
         "the city is called Port Ash", "never use the word suddenly", "Marcus is left handed",
         "chapter ten needs a storm", "the kids are named Ava and Leo", "I work nights at the warehouse",
         "keep the tone dark but hopeful", "my favorite color is green", "the ship sinks in book two",
         "call the plumber", "pay the light bill", "pick up the kids", "check the tax sale list",
         "feed the dog", "back up my laptop", "take my medicine", "email the editor"]
QUERIES = ["cheap flights to Miami", "how to fix a leaky faucet", "Georgia tax deed laws", "best free video editor",
           "weather in Atlanta this weekend", "Python list comprehension examples", "used trucks under 10000",
           "how to start an LLC in Georgia", "Fulton County tax sale dates", "local speech to text models",
           "how to write a thriller plot", "Nvidia stock news", "Windows 11 keyboard shortcuts",
           "best small coding models", "MARTA train schedule", "how to make sourdough bread",
           "home renovation costs per square foot", "Ollama model list", "Micro Nasdaq futures margin",
           "audiobook narration tips"]
URLS = ["https://example.com/report.pdf", "https://news.ycombinator.com", "https://github.com/ollama/ollama",
        "https://www.fultoncountyga.gov/tax-sale", "https://en.wikipedia.org/wiki/Atlanta",
        "https://api.github.com/repos/python/cpython", "https://huggingface.co/Qwen/Qwen2.5-Coder-0.5B",
        "https://www.dekalbtax.org/property-tax", "http://localhost:11434/api/tags", "https://example.org/data.csv",
        "https://www.python.org/downloads/", "https://site.example.com/setup.exe"]
PATHS = ["C:\\Users\\hawpe\\Desktop\\report.docx", "notes.txt", "C:\\Users\\hawpe\\Documents\\budget.xlsx",
         "todo.md", "D:\\Backups", "C:\\Users\\hawpe\\Downloads\\setup.exe", "my_script.py",
         "C:\\Projects\\jarvis\\main.py", "Desktop\\photos", "Documents\\Taxes", "draft_v2.txt", "Downloads",
         "Desktop", "Documents", "C:\\Temp\\log.txt", "invoice_2026.pdf"]
FOLDERS = ["Downloads", "Desktop", "Documents", "Pictures", "Music", "Videos", "C:\\Projects", "D:\\Backups",
           "Documents\\Taxes", "Desktop\\Work"]
NAMES = ["Taxes 2026", "Old Drafts", "Photos", "Work Stuff", "Game Saves", "Recipes", "Scripts", "Invoices"]
COMMANDS = ["pip list", "git status", "ipconfig", "dir", "python --version", "winget upgrade --all",
            "ollama list", "pip install requests", "git pull", "npm install", "tasklist", "systeminfo"]
PROGRAMS = ["notepad", "chrome", "edge", "spotify", "calculator", "word", "excel", "vlc", "discord",
            "file explorer", "steam", "vs code"]
CHARACTERS = ["Marcus", "Ava", "the captain", "Leo", "Grandma Rose", "Detective Hale", "the narrator", "Mia",
              "King Aldric", "Sarah"]
GENRES = ["thriller", "romance", "fantasy", "mystery", "horror", "sci-fi", "memoir", "western", "young adult",
          "historical fiction"]
IDEAS = ["a girl who can hear the dead", "a heist at the Atlanta airport", "two brothers running a tax sale empire",
         "a dragon that wants to be a chef", "a detective who lost her memory", "a space trucker stranded on Mars",
         "a haunted farmhouse in Georgia", "a boxer's comeback", "a kid who finds a time machine",
         "a family secret buried in the backyard"]
INSTRUCTIONS = ["make the ending happier", "add more dialogue", "cut it down by half", "make it scarier",
                "fix the pacing", "give Marcus a bigger role", "make the fight longer", "add a plot twist",
                "use simpler words", "make it funnier"]
TITLES = ["dentist appointment", "car inspection", "writing session", "team meeting", "doctor visit",
          "call with the bank", "pay rent", "oil change", "school pickup", "tax sale research"]
WHENS = ["tomorrow at 9 am", "at 5 pm", "in 20 minutes", "Friday at 2", "next Monday", "tonight at 8",
         "in an hour", "on December 3", "tomorrow morning", "at noon"]
AREAS = ["DeKalb County", "Fulton County", "Atlanta", "Decatur", "Maricopa County", "Phoenix", "Hennepin County",
         "Cobb County", "Georgia", "Minneapolis"]
STATES = ["Florida", "Georgia", "Texas", "Arizona", "Ohio", "Alabama", "Tennessee", "Michigan"]
TOPICS = ["Georgia tax deed laws", "audiobook publishing", "small coding models", "Atlanta real estate prices",
          "Micro Nasdaq trading", "YouTube growth", "quiet title cases", "home renovation costs"]
REPOS = ["ollama/ollama", "python/cpython", "HazyResearch/ncm", "microsoft/vscode", "psf/requests"]
HF = ["Qwen/Qwen2.5-Coder-0.5B", "LiquidAI/LFM3-1.7B-GGUF", "openai/whisper-small", "microsoft/phi-4"]
GAME_IDS = ["P1", "P2", "P3", "P7", "L4", "L5", "L12", "T3", "T12", "T20", "D1", "D2"]
COMPANY_IDS = ["C1", "C2", "C3"]
CALL_IDS = ["1", "2", "3", "4", "7"]
LOAN_IDS = ["B1", "B2", "PL1"]
COMPANIES = ["Peach State Holdings", "ATL Flips", "Southern Renovations", "Midtown Ventures", "Five Points Cars"]
INDUSTRIES = ["construction", "property management", "trucking", "restaurant", "software", "cleaning"]
TRANSIT = ["the streetcar", "MARTA rail", "MARTA buses", "the Atlanta Streetcar"]
CAR_MODELS = ["sedans", "SUVs", "pickups", "luxury cars", "electric cars", "sports cars"]
GAME_TARGETS = ["the next tax sale", "the end of the renovation", "next month", "next year", "March 1, 2027",
                "the title decree", "the next buyer call", "January 1"]
DAYS = ["2026-12-01", "November 30", "last Tuesday", "the start of the game", "day one"]
CHIT_CHAT = ["hi Jarvis", "good morning", "how are you", "thank you", "that's all for now", "you're awesome",
             "tell me a joke", "what does ubiquitous mean", "give me a synonym for brave", "goodnight Jarvis",
             "what's the capital of France", "explain what a tax lien is", "how do I write a good hook",
             "what's 15 percent of 80", "okay cool", "never mind", "who are you", "I'm tired today",
             "what can you do", "lol that's funny", "how are you doing tonight", "how's it going",
             "what's up jarvis", "how was your day", "are you there", "good evening jarvis", "nice work"]


_CHARS_ALL = CHARACTERS
_TRAITS = ["twin brother", "secret", "limp", "gun", "daughter"]
_EXTRAS = ["2026", "near me", "for beginners", "reviews", "cheap"]
_ADJ = ["Dark", "Silver", "Hidden", "Broken", "Golden", "Lost", "Silent", "Burning", "Frozen", "Wild", "Crimson",
        "Last", "Midnight", "Shadow", "Iron", "Secret", "Endless", "Fallen", "Little", "Bitter"]
_NOUN = ["Harbor", "Crown", "River", "Garden", "Empire", "Promise", "City", "Road", "Kingdom", "Forest", "Signal",
         "Tide", "Mountain", "Letter", "Key", "Storm", "House", "Island", "Engine", "Song"]
_SUBJ = ["the meeting", "my sister", "the landlord", "the new chapter", "the villain", "my truck", "the dentist",
         "the tax sale", "the game", "my laptop", "the hero", "grandma", "the deadline", "the editor", "my boss"]
_PRED = ["is at 5", "moved to Friday", "needs a rewrite", "is from Ohio", "has a scar on his face", "is broken",
         "starts at noon", "is due next week", "hates the rain", "owes me money", "is on Peachtree Street",
         "should be funnier", "comes back in chapter 9", "costs 300 dollars", "was a great idea"]
_TOPIC = ["budget laptops", "lighthouse keepers", "Atlanta history", "fixing a roof", "coding models", "used cars",
          "gold prices", "tax liens in Ohio", "how to bake bread", "World War 2 tanks", "solar panels",
          "best fishing spots", "voice actors", "dragon myths", "home loans", "stock market news",
          "the 1996 Olympics", "Python errors", "Windows updates", "brain tumors in dogs"]


_RELATION = ["daughter", "son", "wife", "husband", "mom", "dad", "dog", "cat", "boss", "best friend", "brother"]
_PERSON = ["Maya", "Jordan", "Tasha", "Big Mike", "Lucy", "Andre", "Kim", "Rex", "Bella", "Terrence"]
_CLAUSE = ["the storm hits earlier", "the captain dies", "Ava wins the fight", "it ends on a cliffhanger",
           "the villain gets away", "it happens at night", "the twins meet sooner", "there's more action",
           "the dialogue is sharper", "the reveal comes last"]
_ERRAND = ["call the bank", "pay the water bill", "pick up my check", "email the county", "call my mom",
           "renew my tags", "buy dog food", "check on the house", "send the invoice", "take out the trash"]


def _fact(rng: random.Random):
    t = rng.choice([f"{rng.choice(_LIKES)} {pseudo_word(rng, False)}", f"{rng.choice(_LIKES)} {rng.choice(_TOPIC)}",
                    f"my {rng.choice(_RELATION)}'s name is {pseudo_word(rng)}",
                    f"my {rng.choice(_RELATION)}'s name is {rng.choice(_PERSON)}",
                    f"{rng.choice(_PERSON)} is my {rng.choice(_RELATION)}",
                    f"{rng.choice(_CHARS_ALL)} has a {rng.choice(_TRAITS)}",
                    f"my {rng.choice(_RELATION)} likes {rng.choice(_TOPIC)}", rng.choice(TEXTS)])
    return t, t


def _change(rng: random.Random):
    t = rng.choice([rng.choice(INSTRUCTIONS), rng.choice(_CLAUSE)])
    return t, t


def _errand(rng: random.Random):
    t = rng.choice([rng.choice(_ERRAND), rng.choice(TEXTS[10:])])
    return t, t


_SYLLABLES = ["ka", "ro", "mi", "zen", "tal", "ver", "dor", "lin", "sha", "bel", "cor", "vu", "nes", "tri", "pax",
              "lo", "ra", "quin", "del", "mar", "ost", "fen", "gri", "ul", "sa", "wex", "bru", "ch", "ny", "op"]
_LIKES = ["I hate", "I love", "I'm allergic to", "I can't stand", "I always forget", "I prefer", "I'm scared of"]


def pseudo_word(rng: random.Random, cap: bool = True) -> str:
    """A made-up word, so the brain learns where a name sits in a request instead of memorizing names."""
    w = "".join(rng.choice(_SYLLABLES) for _ in range(rng.randint(2, 3)))
    return w.capitalize() if cap else w


def _words(rng: random.Random, n: int, cap: bool = True) -> str:
    return " ".join(pseudo_word(rng, cap) for _ in range(n))


def _title(rng: random.Random):
    if rng.random() < 0.35:
        t = rng.choice([_words(rng, rng.randint(1, 3)), f"The {_words(rng, rng.randint(1, 2))}",
                        f"{rng.choice(_ADJ)} {pseudo_word(rng)}", f"{pseudo_word(rng)} {rng.choice(_NOUN)}"])
        return t, t
    t = rng.choice([f"The {rng.choice(_ADJ)} {rng.choice(_NOUN)}", f"{rng.choice(_ADJ)} {rng.choice(_NOUN)}",
                    f"{rng.choice(_NOUN)} of {rng.choice(_NOUN)}s", rng.choice(BOOKS)])
    return t, t


def _sentence(rng: random.Random):
    t = rng.choice([f"{rng.choice(_SUBJ)} {rng.choice(_PRED)}", rng.choice(TEXTS), _fact(rng)[0]])
    return t, t


def _topic(rng: random.Random):
    t = rng.choice([f"{rng.choice(['cheap', 'best', 'used', 'free', 'local'])} {pseudo_word(rng, False)} "
                    f"{rng.choice(['in', 'near', 'for'])} {rng.choice(AREAS + _PERSON)}",
                    f"how to {rng.choice(['fix', 'replace', 'install', 'clean', 'build'])} a {pseudo_word(rng, False)}",
                    rng.choice(_TOPIC), rng.choice(QUERIES), f"{rng.choice(_TOPIC)} {rng.choice(_EXTRAS)}"])
    return t, t


def _named(items: list[str]):
    def pick(rng: random.Random):
        r = rng.random()
        if r < 0.3:
            t = _words(rng, rng.randint(1, 3))
        elif r < 0.45:
            t = f"{rng.choice(items)} {pseudo_word(rng)}"
        else:
            t = rng.choice(items)
        return t, t
    return pick


def _address(rng: random.Random):
    t = rng.choice([f"{rng.randint(1, 999)} {pseudo_word(rng)} {rng.choice(['St', 'Ave', 'Rd', 'Dr'])}",
                    f"{pseudo_word(rng, False)} at {rng.randint(1, 12)}", _sentence(rng)[0]])
    return t, t


def _pairs(items: list[tuple[str, str]]):
    return lambda rng: rng.choice(items)


def _num(lo: int, hi: int, words: bool = True):
    def pick(rng: random.Random):
        n = rng.randint(lo, hi)
        if words and n < len(_WORDS) and rng.random() < 0.35:
            return _WORDS[n], n
        return str(n), n
    return pick


def _money(lo: int, hi: int):
    def pick(rng: random.Random):
        n = rng.randrange(lo, hi, max(1, (hi - lo) // 200)) // 100 * 100 or lo
        style = rng.random()
        if style < 0.35:
            return f"${n:,}", n
        if style < 0.6:
            return str(n), n
        if style < 0.8 and n % 1000 == 0:
            return f"{n // 1000}k", n
        return f"{n:,}", n
    return pick


def _list(items: list[str]):
    return lambda rng: (lambda v: (v, v))(rng.choice(items))


POOLS = {
    "fact": _fact, "change": _change, "errand": _errand,
    "book": _title, "text": _sentence, "query": _topic, "url": _list(URLS), "path": _list(PATHS),
    "folder": _list(FOLDERS), "name": _named(NAMES), "clip": _address, "command": _list(COMMANDS),
    "program": _list(PROGRAMS),
    "character": _named(CHARACTERS), "genre": _list(GENRES), "idea": _list(IDEAS), "instructions": _list(INSTRUCTIONS),
    "title": _list(TITLES), "when": _list(WHENS), "area": _list(AREAS), "state": _list(STATES),
    "topic": _list(TOPICS), "repo": _list(REPOS), "hf": _list(HF), "gid": _list(GAME_IDS),
    "cid": _list(COMPANY_IDS), "callid": _list(CALL_IDS), "loanid": _list(LOAN_IDS), "company": _list(COMPANIES),
    "industry": _list(INDUSTRIES), "transit": _list(TRANSIT), "carmodel": _list(CAR_MODELS),
    "gtarget": _list(GAME_TARGETS), "gday": _list(DAYS), "n": _num(1, 12), "big": _num(10, 400, words=False),
    "id": _num(1, 60, words=False), "xy": _num(10, 1900, words=False), "minutes": _num(5, 60),
    "speedword": _list(["faster", "slower", "a little faster", "a bit slower", "normal speed"]),
    "format": _list(["docx", "epub", "markdown", "mp3", "m4b"]),
    "tab": _list(["planner", "research", "game", "library", "settings", "write", "listen", "publish", "workshop",
                  "war room", "chat"]),
    "brainname": _pairs([("Gemini", "gemini"), ("Groq", "groq"), ("LFM3", "lfm3"), ("the local model", "lfm3")]),
    "brainuse": _pairs([("Gemini", "gemini"), ("Groq", "groq"), ("LFM3", "lfm3"), ("the local model", "lfm3")]),
    "keys": _list(["ctrl+s", "alt+tab", "ctrl+c", "enter", "win+d", "ctrl+shift+esc"]),
    "repair": _list(["roof", "foundation", "HVAC", "kitchen", "plumbing", "electrical"]),
    "chat": _list(CHIT_CHAT),
    "jobid": _list(["a1b2c3d4e5f6", "9f8e7d6c5b4a", "3c5e7a9b1d2f", "job7", "12"]),
    "money": _money(1000, 9000), "price": _money(50000, 400000), "budget": _money(10000, 100000),
}

_SLOT = re.compile(r"\{(\w+):(\w+)\}")
_CHOICE = re.compile(r"\[([^\[\]]*)\]")
PREFIXES = ["", "", "", "jarvis ", "jarvis, ", "hey jarvis ", "can you ", "please ", "could you ", "i need you to ",
            "go ahead and ", "yo jarvis ", "ok ", "now "]
SUFFIXES = ["", "", "", " please", " for me", " now", " real quick", " thanks", "?", "."]


@dataclass(frozen=True)
class Template:
    tool: Optional[str]
    pattern: str
    fixed: dict = field(default_factory=dict)


@dataclass
class Example:
    request: str
    tool: Optional[str]
    args: dict
    spans: dict  # argument -> (start, end) in request, for values copied from the request
    template: int = 0


def load_templates(path: Path = TEMPLATES_PATH) -> list[Template]:
    out = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(" | ")]
        if len(parts) not in (2, 3):
            raise ValueError(f"{path} line {n}: want 'tool | pattern | fixed args'")
        try:
            fixed = json.loads(parts[2]) if len(parts) == 3 else {}
        except ValueError as exc:
            raise ValueError(f"{path} line {n}: {exc}") from exc
        tool = None if parts[0] == NO_TOOL else parts[0]
        for _, pool in _SLOT.findall(parts[1]):
            if pool not in POOLS:
                raise ValueError(f"{path} line {n}: unknown pool {pool!r}")
        out.append(Template(tool, parts[1], fixed))
    return out


def _typo(word: str, rng: random.Random) -> str:
    swaps = {"and": "an", "you": "u", "are": "r", "your": "ur", "please": "plz", "the": "teh", "going": "gonna",
             "properties": "phropertys", "want": "wont", "about": "abot", "money": "monney"}
    if word in swaps and rng.random() < 0.5:
        return swaps[word]
    if len(word) > 4 and rng.random() < 0.5:
        i = rng.randrange(1, len(word) - 1)
        return word[:i] + word[i + 1:]
    return word


def _join(text: str, piece: str) -> str:
    if text.endswith(" ") and piece.startswith(" "):
        piece = piece.lstrip(" ")
    return text + piece


def render(template: Template, rng: random.Random, noise: float = 0.0) -> Example:
    pattern = _CHOICE.sub(lambda m: rng.choice(m.group(1).split("|")), template.pattern)
    pattern = re.sub(r"  +", " ", pattern).strip()
    text, spans, args = rng.choice(PREFIXES), {}, dict(template.fixed)

    def literal(piece: str) -> str:
        if not noise:
            return piece
        return " ".join(_typo(w, rng) if w and rng.random() < noise else w for w in piece.split(" "))

    pos = 0
    for m in _SLOT.finditer(pattern):
        text = _join(text, literal(pattern[pos:m.start()]))
        surface, value = POOLS[m.group(2)](rng)
        if isinstance(value, str) and value == surface and rng.random() < 0.3:
            surface = value = surface.lower()
        spans[m.group(1)] = (len(text), len(text) + len(surface))
        args[m.group(1)] = value
        text += surface
        pos = m.end()
    text = _join(text, literal(pattern[pos:]))
    if not text.endswith(("?", ".")):
        text += rng.choice(SUFFIXES)
    if template.tool is None:
        args, spans = {}, {}
    return Example(text, template.tool, args, spans)


def _find_number(text: str, value) -> Optional[tuple[int, int]]:
    for m in re.finditer(r"\$?\d[\d,]*(?:\.\d+)?k?|\b[a-z]+\b", text.lower()):
        if parse_number(m.group(0)) == value:
            return m.start(), m.end()
    return None


def parse_number(text: str) -> Optional[float]:
    t = str(text).strip().lower().replace("$", "").replace(",", "")
    if t in WORD_NUMBERS:
        return WORD_NUMBERS[t]
    mult = 1
    if t.endswith("k"):
        t, mult = t[:-1], 1000
    elif t.endswith(("m", " million")):
        t, mult = t.rstrip("m").replace(" million", ""), 1_000_000
    try:
        n = float(t) * mult
    except ValueError:
        return None
    return int(n) if n == int(n) else n


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def generate(templates: list[Template], per_template: int = 30, seed: int = 7, noise: float = 0.08,
             held_out: Optional[list[ToolCase]] = None) -> list[Example]:
    rng = random.Random(seed)
    banned = {_norm(c.request) for c in held_out or []}
    seen, out = set(), []
    for n, template in enumerate(templates):
        for _ in range(per_template * 4):
            if sum(1 for e in out[-per_template:] if e.template == n) >= per_template:
                break
            ex = render(template, rng, noise if rng.random() < 0.5 else 0.0)
            key = _norm(ex.request)
            if key in banned or key in seen or not key:
                continue
            seen.add(key)
            ex.template = n
            out.append(ex)
    return out
