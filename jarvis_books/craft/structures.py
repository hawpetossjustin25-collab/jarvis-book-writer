"""Story structures (beat sheets) with each beat's position as a fraction of the book."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Beat:
    name: str
    position: float  # 0.0 start - 1.0 end
    purpose: str


@dataclass(frozen=True)
class Structure:
    key: str
    name: str
    beats: tuple[Beat, ...]

    def outline_guide(self, chapters: int) -> str:
        lines = [f"Structure: {self.name}. Place these beats at roughly these chapters:"]
        for beat in self.beats:
            ch = max(1, min(chapters, round(beat.position * chapters + 0.5)))
            lines.append(f"- Ch {ch}: {beat.name} - {beat.purpose}")
        return "\n".join(lines)


def _s(key: str, name: str, beats: list[tuple[str, float, str]]) -> Structure:
    return Structure(key, name, tuple(Beat(*b) for b in beats))


STRUCTURES: dict[str, Structure] = {s.key: s for s in (
    _s("three_act", "Three-Act Structure", [
        ("Hook / Setup", 0.0, "introduce the protagonist, their world and what they want"),
        ("Inciting Incident", 0.10, "the event that disrupts normal life"),
        ("Plot Point 1", 0.25, "the protagonist commits to the journey; Act 2 begins"),
        ("Rising Action", 0.37, "obstacles escalate; allies and enemies emerge"),
        ("Midpoint", 0.50, "a revelation or reversal raises the stakes"),
        ("Plot Point 2 / Crisis", 0.75, "the lowest point; all seems lost"),
        ("Climax", 0.90, "the final confrontation with the central conflict"),
        ("Resolution", 0.98, "the new normal; arcs and threads resolved"),
    ]),
    _s("save_the_cat", "Save the Cat (15 beats)", [
        ("Opening Image", 0.0, "a snapshot of the hero's flawed starting world"),
        ("Theme Stated", 0.05, "someone hints at the lesson the hero must learn"),
        ("Set-Up", 0.05, "the hero's life, flaws and what's missing"),
        ("Catalyst", 0.10, "the life-changing event"),
        ("Debate", 0.15, "the hero resists the call"),
        ("Break into Two", 0.20, "the hero chooses to enter the new world"),
        ("B Story", 0.22, "a relationship that carries the theme"),
        ("Fun and Games", 0.30, "the promise of the premise delivered"),
        ("Midpoint", 0.50, "false victory or false defeat; stakes rise"),
        ("Bad Guys Close In", 0.62, "external pressure and internal doubt grow"),
        ("All Is Lost", 0.75, "the worst moment; a 'whiff of death'"),
        ("Dark Night of the Soul", 0.80, "the hero hits bottom and finally learns the theme"),
        ("Break into Three", 0.80, "a new plan combining A and B stories"),
        ("Finale", 0.90, "the hero proves change and defeats the problem"),
        ("Final Image", 1.0, "the mirror of the opening image, showing change"),
    ]),
    _s("heros_journey", "The Hero's Journey (12 stages)", [
        ("Ordinary World", 0.0, "the hero at home, incomplete"),
        ("Call to Adventure", 0.08, "a problem or quest appears"),
        ("Refusal of the Call", 0.12, "fear or duty holds the hero back"),
        ("Meeting the Mentor", 0.17, "guidance, gifts or training"),
        ("Crossing the Threshold", 0.25, "the hero enters the special world"),
        ("Tests, Allies, Enemies", 0.35, "the rules of the new world are learned"),
        ("Approach to the Inmost Cave", 0.45, "preparation for the central ordeal"),
        ("The Ordeal", 0.55, "a life-or-death crisis; symbolic death and rebirth"),
        ("Reward", 0.65, "the hero seizes the prize"),
        ("The Road Back", 0.75, "pursuit; the consequences of the ordeal"),
        ("Resurrection", 0.90, "the final test; the hero is transformed"),
        ("Return with the Elixir", 1.0, "the hero returns home changed"),
    ]),
    _s("seven_point", "Seven-Point Story Structure", [
        ("Hook", 0.0, "the protagonist's starting state (the opposite of the resolution)"),
        ("Plot Turn 1", 0.15, "the call to action that sets the story moving"),
        ("Pinch 1", 0.30, "the antagonist applies pressure; stakes become clear"),
        ("Midpoint", 0.50, "the protagonist shifts from reaction to action"),
        ("Pinch 2", 0.70, "a devastating blow; the plan fails"),
        ("Plot Turn 2", 0.85, "the final piece needed to win is found"),
        ("Resolution", 1.0, "the climax and the hero's completed change"),
    ]),
    _s("romancing_the_beat", "Romancing the Beat (romance)", [
        ("Setup / Meet", 0.05, "both leads introduced; they meet and there is a spark or clash"),
        ("No Way", 0.15, "reasons they can't or won't be together"),
        ("Adhesion", 0.20, "circumstances force them together"),
        ("Deepening Desire", 0.35, "attraction grows despite the obstacles"),
        ("Midpoint of Intimacy", 0.50, "an emotional or physical milestone"),
        ("Retreat", 0.65, "fears and wounds push them apart"),
        ("Break Up / Dark Moment", 0.75, "the relationship seems over"),
        ("Grand Gesture", 0.90, "one or both lead risks everything for love"),
        ("Happily Ever After", 1.0, "the committed relationship"),
    ]),
    _s("nonfiction", "Nonfiction Chapter Arc", [
        ("Promise", 0.0, "the problem the reader has and what the book will give them"),
        ("Foundations", 0.15, "the core concepts and the author's credibility"),
        ("Core Method", 0.30, "the main framework, one idea per chapter"),
        ("Application", 0.65, "stories, examples and exercises"),
        ("Obstacles", 0.80, "common pitfalls and how to overcome them"),
        ("Transformation", 1.0, "the reader's new state and next steps"),
    ]),
)}


def get_structure(key: str) -> Structure:
    return STRUCTURES.get(key or "", STRUCTURES["three_act"])


def beat_for_chapter(structure: Structure, chapter: int, total: int) -> Beat:
    """The beat a chapter falls under (the last beat whose position has been reached)."""
    total = max(1, total)
    position = (chapter - 0.5) / total
    current = structure.beats[0]
    for beat in structure.beats:
        if beat.position <= position + 1e-9:
            current = beat
    return current
