"""Jarvis's tools for Imagination, Justin's game (mixed into the ToolBox). The player talks; Jarvis is the
voice of the game and calls these for every move, the same way the Atlanta game is played."""
from __future__ import annotations

from typing import Optional

from jarvis_books.errors import ValidationError

S = {"type": "STRING"}
B = {"type": "BOOLEAN"}
N = {"type": "NUMBER"}
SAY = (" Read the 'say' text to the player in your own warm voice; never add events, places or powers "
       "that aren't in it.")


def _decl(name: str, description: str, props: Optional[dict] = None, required: Optional[list[str]] = None) -> dict:
    params: dict = {"type": "OBJECT", "properties": props or {}}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


IMAGINATION_DECLARATIONS = [
    _decl("imagination_status", "Imagination (Justin's game): where the player is, health, their wand and "
          "spellstones, materials, active powers and the next clock chime. Use it when they come back to the game."
          + SAY),
    _decl("imagination_new", "Start Imagination over (erases the current game). It begins in the player's ordinary "
          "bedroom unless skip_intro is true.", {"skip_intro": B}),
    _decl("imagination_story", "Play the opening of Imagination: the ordinary day before the dream (do homework, "
          "eat dinner, turn off the lights, go to sleep, wake up, or 'skip').", {"action": S}, ["action"]),
    _decl("imagination_look", "Look around in Imagination: the place, spellstones on the ground, people, rivals, "
          "creatures, portals and open paths." + SAY),
    _decl("imagination_go", "Travel in Imagination to a place by name (walks or swims the real route), or 'back' "
          "to run back where they came from.", {"place": S}, ["place"]),
    _decl("imagination_take", "Pick up a spellstone in Imagination, or claim the wand ('wand').", {"thing": S}),
    _decl("imagination_use", "Use a spellstone power in Imagination (like lantern, tide, gale, abyssal breath).",
          {"spellstone": S, "target": S}, ["spellstone"]),
    _decl("imagination_combo", "Cast a wand combo in Imagination: Vanish, True Sight, Blizzard, Hunter's Eye or "
          "False Alarm.", {"combo": S}, ["combo"]),
    _decl("imagination_wand", "The player's wand in Imagination: the spellstones it holds, their levels, and "
          "combos ready to cast."),
    _decl("imagination_step_through", "Step through the open portal or rift here in Imagination."),
    _decl("imagination_wait", "Let time pass in Imagination (minutes), for example to wait for a clock to chime.",
          {"minutes": N}),
    _decl("imagination_companions", "Noah and Angelic in Imagination: where they are, or give them an order "
          "(follow, stay, guard a place, search for spellstones). companion is 'Noah', 'Angelic' or 'both'.",
          {"companion": S, "order": S, "place": S}),
    _decl("imagination_rivals", "Where the four rivals of Imagination (Vex, Kestrel, Magpie, Grist) were last seen "
          "and which spellstones they hold."),
    _decl("imagination_fight", "Fight the creature or rival here in Imagination."),
    _decl("imagination_search", "Search the place in Imagination for salvage (key blanks, pearl shells, abyss "
          "glass) or old writing."),
    _decl("imagination_forge", "Forge a spellstone at Pelagoria's Forge Hall in Imagination (anchor, beacon, "
          "clockwork, magnet).", {"spellstone": S}, ["spellstone"]),
    _decl("imagination_missions", "The player's missions in Imagination and the next step of each."),
    _decl("imagination_map", "The player's map of Imagination: the places they've been."),
    _decl("imagination_clocks", "When each clock in Imagination chimes next, and which portal it opens."),
    _decl("imagination_lore", "Facts from the canon of Imagination (the real game data and story canon) about a "
          "topic: a character, place, spellstone, wand, creature, mission or rule. Use it to answer questions about "
          "the game or before writing about it.", {"topic": S}, ["topic"]),
    _decl("imagination_teach_book", "Write the whole canon of Imagination (every place, character, spellstone, "
          "wand, creature, clock, portal, mission and rule) into the Imagination book's story bible, so Jarvis can "
          "write the book."),
]
IMAGINATION_TOOLS = tuple(d["name"] for d in IMAGINATION_DECLARATIONS)

IMAGINATION_WORDS = ("imagination", "spellstone", "wand", "portal", "rift", "clock", "chime", "noah", "angelic",
                     "rival", "vex", "kestrel", "magpie", "grist", "creature", "gullhaven", "hearthstone",
                     "pelagoria", "deep cave", "underwater", "mission", "go to", "travel", "walk to", "swim",
                     "look around", "where am i", "pick up", "take the", "use the", "use my", "step through",
                     "fight", "forge", "salvage", "search", "homework", "dinner", "lights", "sleep", "wake up",
                     "map", "combo", "vanish", "true sight", "blizzard", "hunter's eye", "false alarm", "lantern",
                     "abyssal", "breath", "wait", "follow me", "guard", "lore", "story bible", "the book",
                     "dream", "skip")

IMAGINATION_ACTION_WORDS = ("go to", "travel", "walk", "swim", "run", "pick up", "take", "grab", "use ", "cast",
                            "step through", "fight", "attack", "forge", "salvage", "search", "wait", "follow",
                            "stay", "guard", "do my homework", "eat", "turn off", "sleep", "wake", "skip", "claim",
                            "new game", "start over")


def asks_for_imagination_move(text: str) -> bool:
    """Whether the player asked for a move in Imagination, so an answer with no tool call can't be real."""
    t = f" {(text or '').lower()} "
    return any(w in t for w in IMAGINATION_WORDS) and any(w in t for w in IMAGINATION_ACTION_WORDS)


class ImaginationTools:
    """Mixed into ToolBox; `self.imagination` is set by the Studio."""
    imagination = None

    def _imagination(self):
        if self.imagination is None:
            raise ValidationError("Imagination isn't loaded")
        return self.imagination

    def _play_imagination(fn):  # noqa: N805 - a decorator for the tool methods below
        def wrapped(self, *a, **kw):
            try:
                return fn(self, *a, **kw)
            except (ValidationError, ValueError) as exc:
                return {"error": str(exc)}
        wrapped.__name__ = fn.__name__
        return wrapped

    def describe_imagination(self, name: str, args: dict) -> Optional[str]:
        if name == "imagination_new":
            return "Start Imagination over (the current game is erased)"
        return None

    @_play_imagination
    def _t_imagination_status(self) -> dict:
        return self._imagination().status()

    @_play_imagination
    def _t_imagination_new(self, skip_intro: bool = False) -> dict:
        return self._imagination().new_game(skip_intro=bool(skip_intro))

    @_play_imagination
    def _t_imagination_story(self, action: str) -> dict:
        return self._imagination().story(action)

    @_play_imagination
    def _t_imagination_look(self) -> dict:
        return self._imagination().look()

    @_play_imagination
    def _t_imagination_go(self, place: str) -> dict:
        return self._imagination().go(place)

    @_play_imagination
    def _t_imagination_take(self, thing: str = "") -> dict:
        return self._imagination().take(thing)

    @_play_imagination
    def _t_imagination_use(self, spellstone: str, target: str = "") -> dict:
        return self._imagination().use(spellstone, target)

    @_play_imagination
    def _t_imagination_combo(self, combo: str) -> dict:
        return self._imagination().combo(combo)

    @_play_imagination
    def _t_imagination_wand(self) -> dict:
        return self._imagination().wand()

    @_play_imagination
    def _t_imagination_step_through(self) -> dict:
        return self._imagination().step_through()

    @_play_imagination
    def _t_imagination_wait(self, minutes: float = 5) -> dict:
        return self._imagination().wait(minutes)

    @_play_imagination
    def _t_imagination_companions(self, companion: str = "both", order: str = "", place: str = "") -> dict:
        g = self._imagination()
        return g.order(companion, order, place) if order else g.companions()

    @_play_imagination
    def _t_imagination_rivals(self) -> dict:
        return self._imagination().rivals()

    @_play_imagination
    def _t_imagination_fight(self) -> dict:
        return self._imagination().fight()

    @_play_imagination
    def _t_imagination_search(self) -> dict:
        return self._imagination().search()

    @_play_imagination
    def _t_imagination_forge(self, spellstone: str) -> dict:
        return self._imagination().forge(spellstone)

    @_play_imagination
    def _t_imagination_missions(self) -> dict:
        return self._imagination().missions()

    @_play_imagination
    def _t_imagination_map(self) -> dict:
        return self._imagination().map()

    @_play_imagination
    def _t_imagination_clocks(self) -> dict:
        return self._imagination().clocks()

    @_play_imagination
    def _t_imagination_lore(self, topic: str) -> dict:
        from jarvis_books.imagination.lore import lookup
        return lookup(self._imagination().c, topic)

    @_play_imagination
    def _t_imagination_teach_book(self) -> dict:
        from jarvis_books.imagination.bible import teach
        out = teach(self.library, self._imagination().c)
        out["say"] = (f"The book '{out['book']}' now holds the whole canon of Imagination: {out['entries']} story "
                      "bible entries plus the story canon.")
        return out
