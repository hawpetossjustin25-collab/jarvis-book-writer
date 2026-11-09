"""Genre profiles: the conventions professional authors and publishers work to.

Word-count ranges follow common agent and publisher guidance for debut novels; chapter-length
ranges follow genre practice (a large published-book scan found a median chapter of ~2,750 words,
middle half 2,061-3,724, median 29 chapters). They are targets, not hard rules.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass(frozen=True)
class GenreProfile:
    key: str
    name: str
    book_words: tuple[int, int]
    chapter_words: tuple[int, int]
    default_pov: str
    default_tense: str
    structure: str
    pacing: str
    conventions: tuple[str, ...]
    reader_promise: str
    aliases: tuple[str, ...] = ()

    @property
    def target_words(self) -> int:
        return (self.book_words[0] + self.book_words[1]) // 2

    @property
    def target_chapter_words(self) -> int:
        return (self.chapter_words[0] + self.chapter_words[1]) // 2

    @property
    def suggested_chapters(self) -> int:
        return max(3, round(self.target_words / self.target_chapter_words))

    def guidance(self) -> str:
        return (
            f"{self.name}: books usually run {self.book_words[0]:,}-{self.book_words[1]:,} words, chapters "
            f"{self.chapter_words[0]:,}-{self.chapter_words[1]:,} words (about {self.suggested_chapters} chapters). "
            f"Typical POV: {self.default_pov}, {self.default_tense} tense. Pacing: {self.pacing}. "
            f"Reader promise: {self.reader_promise}. Conventions: " + "; ".join(self.conventions) + "."
        )


GENRES: dict[str, GenreProfile] = {g.key: g for g in (
    GenreProfile("thriller", "Thriller", (80_000, 100_000), (1_000, 2_500), "third person limited or first person", "past",
                 "save_the_cat", "fast, short chapters, a hook or reversal at every chapter end",
                 ("a ticking clock", "high personal and public stakes", "an antagonist as capable as the hero",
                  "escalating set pieces", "twists that are surprising but fair"),
                 "relentless suspense and a satisfying final confrontation", ("suspense", "action", "spy", "techno thriller")),
    GenreProfile("mystery", "Mystery", (70_000, 90_000), (2_000, 3_500), "first person or close third", "past",
                 "three_act", "steady, clue-driven, with a reveal at each act break",
                 ("crime or puzzle introduced early", "fair-play clues the reader can follow", "red herrings",
                  "a limited suspect pool", "the detective solves it through deduction"),
                 "a puzzle the reader could solve, resolved with justice", ("detective", "crime", "whodunit", "cozy mystery", "noir")),
    GenreProfile("romance", "Romance", (70_000, 90_000), (2_000, 4_000), "dual first or close third (both leads)", "past",
                 "romancing_the_beat", "emotional, alternating between the two leads",
                 ("the love story is the main plot", "meet-cute or inciting encounter", "internal and external obstacles",
                  "a dark moment that threatens the relationship", "a happily ever after or happy for now"),
                 "an emotionally satisfying, optimistic ending", ("romantic comedy", "rom com", "love story", "contemporary romance")),
    GenreProfile("fantasy", "Fantasy", (90_000, 120_000), (3_000, 6_000), "third person limited (one or several POVs)", "past",
                 "heros_journey", "immersive, with breathing room between big sequences",
                 ("a consistent magic system with costs", "worldbuilding revealed through story, not info-dumps",
                  "a quest or rising threat", "found family or mentors", "a climax that pays off the magic rules"),
                 "wonder, a fully realized world, and an epic payoff", ("epic fantasy", "high fantasy", "urban fantasy", "sword and sorcery")),
    GenreProfile("science_fiction", "Science Fiction", (90_000, 120_000), (2_500, 5_000), "third person limited", "past",
                 "three_act", "idea-driven, with tension from the speculative premise",
                 ("one big 'what if' explored with consequences", "consistent technology rules", "the setting shapes the conflict",
                  "human stakes inside the big idea"),
                 "a thought-provoking idea dramatized through characters", ("sci-fi", "scifi", "space opera", "cyberpunk", "dystopian")),
    GenreProfile("horror", "Horror", (70_000, 90_000), (1_500, 3_500), "first person or close third", "past",
                 "seven_point", "slow-building dread that accelerates",
                 ("isolation", "the threat is revealed gradually", "escalating scares", "characters the reader fears for",
                  "a cost paid by the survivors"),
                 "dread, fear and catharsis", ("supernatural", "paranormal", "ghost story", "slasher")),
    GenreProfile("literary", "Literary Fiction", (70_000, 100_000), (3_000, 6_000), "any; voice-driven", "past or present",
                 "three_act", "measured, character-driven",
                 ("rich interiority", "theme expressed through character change", "precise, distinctive prose",
                  "ambiguity is allowed", "subtext over exposition"),
                 "insight into the human condition", ("literary fiction", "upmarket", "book club")),
    GenreProfile("young_adult", "Young Adult", (60_000, 85_000), (1_500, 3_500), "first person", "present or past",
                 "save_the_cat", "brisk, emotionally immediate",
                 ("a teen protagonist (14-18) with agency", "coming-of-age themes", "authentic teen voice",
                  "high emotional stakes", "identity and belonging"),
                 "an intense, relatable coming-of-age journey", ("ya", "teen")),
    GenreProfile("middle_grade", "Middle Grade", (30_000, 55_000), (1_000, 2_500), "first or third person", "past",
                 "heros_journey", "fast and fun",
                 ("a protagonist aged 10-13", "friendship and family", "age-appropriate content", "humor", "a hopeful ending"),
                 "adventure and heart for young readers", ("mg", "kids", "children's novel")),
    GenreProfile("historical", "Historical Fiction", (90_000, 110_000), (2_500, 5_000), "third person limited or first person",
                 "past", "three_act", "immersive, period-authentic",
                 ("accurate period detail", "real events as backdrop", "period-appropriate attitudes and dialogue",
                  "fictional characters with modern resonance"),
                 "living inside another era", ("historical", "period piece")),
    GenreProfile("memoir", "Memoir", (60_000, 90_000), (2_500, 5_000), "first person", "past",
                 "three_act", "reflective, scene-based",
                 ("true events told as scenes", "a central theme or question", "the narrator's growth",
                  "honesty about flaws", "reflection that gives meaning"),
                 "a true story with a hard-won insight", ("autobiography", "life story", "personal story")),
    GenreProfile("nonfiction", "Narrative / Self-help Nonfiction", (50_000, 80_000), (3_000, 6_000), "first or second person",
                 "present", "nonfiction", "clear, one idea per chapter",
                 ("a clear promise to the reader", "one core idea per chapter", "stories and examples", "actionable takeaways",
                  "a summary at the end of each chapter"),
                 "a transformation or understanding the reader can use", ("self-help", "how-to", "business book", "non fiction")),
    GenreProfile("general", "General Fiction", (70_000, 100_000), (2_000, 4_000), "third person limited", "past",
                 "three_act", "balanced", ("a clear protagonist goal", "rising conflict", "a satisfying resolution"),
                 "a compelling story well told", ("general fiction", "novel", "story", "fiction")),
)}


def match_genre(text: str) -> GenreProfile:
    """Map how the user describes a story ('a spooky ghost story') to a genre profile."""
    lowered = (text or "").lower()
    best: tuple[int, GenreProfile] | None = None
    for profile in GENRES.values():
        for term in (profile.key.replace("_", " "), profile.name.lower(), *profile.aliases):
            if term and term in lowered and (best is None or len(term) > best[0]):
                best = (len(term), profile)
    if best:
        return best[1]
    words = lowered.split()
    names = {term: p for p in GENRES.values() for term in (p.key, p.name.lower(), *p.aliases)}
    for word in words:
        close = difflib.get_close_matches(word, list(names), n=1, cutoff=0.85)
        if close:
            return names[close[0]]
    return GENRES["general"]
