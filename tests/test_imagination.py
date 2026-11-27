"""Imagination, Justin's game: the canon from the real Godot world, the story bible Jarvis writes from, and the
voice-played game with its tools."""
import json

import pytest

from jarvis_books.imagination import ImaginationGame, load_canon
from jarvis_books.imagination.bible import BOOK_TITLE, LORE_SOURCE, teach
from jarvis_books.imagination.engine import ImaginationError, MAX_HEALTH
from jarvis_books.imagination.lore import lookup


@pytest.fixture
def canon():
    return load_canon()


@pytest.fixture
def game(tmp_path, canon):
    g = ImaginationGame(tmp_path / "imagination.json", canon=canon, seed=7)
    g.new_game(skip_intro=True)
    return g


def put(g, place):
    g.s["place"] = place
    g._visit(place)


def give(g, *stones):
    for st in stones:
        g.s["loose"].pop(st, None)
        g.s["carried"].append(st)


# ---- the canon ---------------------------------------------------------------------------------------------
def test_the_canon_is_the_real_exported_world(canon):
    assert canon.data["title"] == "Imagination"
    assert len(canon.places) == 115 and len(canon.stones) == 26 and len(canon.wands) == 3
    assert {a["name"] for a in canon.agents.values() if a["role"] == "rival"} == {"Vex", "Kestrel", "Magpie", "Grist"}
    assert canon.find_place("hearthstone house") == "hearth"
    assert canon.find_place("the cliff path") == "cliff_path"
    assert canon.find_stone("abyssal breath") == "pearl"
    assert canon.find_stone("Lantern Spellstone") == "lantern"


def test_nothing_from_locke_and_key_is_in_the_canon(canon):
    raw = json.dumps(canon.data).lower()
    for word in ("locke", "keyhouse", "matheson", "anywhere key", "head key", "shadow key", "omega"):
        assert word not in raw


# ---- the opening -------------------------------------------------------------------------------------------
def test_the_opening_is_an_ordinary_day_a_dream_and_waking_in_imagination(tmp_path, canon):
    g = ImaginationGame(tmp_path / "i.json", canon=canon)
    assert "bedroom" in g.new_game()["say"]
    with pytest.raises(ImaginationError):
        g.go("main street")
    assert "not tired enough" in g.story("go to sleep")["say"]
    g.story("do my homework")
    g.story("eat dinner")
    assert "tired" in g.story("turn off the lights")["say"]
    dream = g.story("go to sleep")["say"]
    assert "ocean" in dream and "cave" in dream and "clock" in dream and "floating" in dream
    woke = g.story("wake up")["say"]
    assert g.s["place"] == "square" and "IMAGINATION" in woke
    assert "IMAGINATION" not in g.look()["say"]  # the sign is read once, then it's just the square


# ---- moving around -----------------------------------------------------------------------------------------
def test_travel_follows_real_paths_and_times_and_meets_the_companions(game):
    out = game.go("hearthstone house")
    assert out["ok"] and game.s["place"] == "hearth"
    assert out["route"] == ["Gullhaven Main Street", "Hearthstone House"]
    assert game.s["now"] > 60
    assert all(v["met"] and v["order"] == "follow" for v in game.s["companions"].values())
    assert "Noah" in out["say"]


def test_the_sea_needs_the_abyssal_breath(game):
    put(game, "pier")
    blocked = game.go("pelagoria sea gate")
    assert not blocked["ok"] and "Abyssal Breath" in blocked["say"] and game.s["place"] == "pier"
    give(game, "pearl")
    assert game.use("abyssal breath")["ok"]
    game.s["effects"]["pearl"] = 1e9  # a long swim: keep the breath on for the whole trip
    assert game.go("pelagoria sea gate")["ok"] and game.s["place"] == "sea_gate"


def test_running_out_of_air_hurts_and_uncle_dominic_brings_you_home(game):
    put(game, "shallows" if "shallows" in game.c.places else "sea_gate")
    game.s["encounter"] = ""
    game.wait(3)
    assert game.s["knockouts"] == 1 and game.s["place"] == "hearth"


def test_you_cant_walk_into_the_deep_cave(game):
    out = game.go("old hollowmere")
    assert not out["ok"] and "rift" in out["say"].lower()


def test_the_cliffside_rift_surprises_you_and_always_brings_you_back(game):
    put(game, "cliff_path")
    game.s["encounter"] = ""
    game.wait(10)
    assert game.c.in_cave(game.s["place"])
    assert "the_descent" in game.s["dynamic"]
    while game.s["place"] != "cave_mouth_0" or game.s["portals"]["cliffside_rift"] <= game.s["now"]:
        if game.s["place"] != "cave_mouth_0":
            game.go("ember gallery")
        else:
            game.wait(1)
    assert game.step_through()["ok"] and game.s["place"] == "cliff_path"


# ---- spellstones -------------------------------------------------------------------------------------------
def test_lantern_reveals_the_hidden_tilt_spellstone(game):
    put(game, "hearth")
    assert "Tilt" not in game.look()["say"]
    game.take("lantern")
    out = game.use("lantern")
    assert "Tilt" in out["say"]
    assert game.take("tilt")["ok"] and "tilt" in game.s["carried"]


def test_tide_drains_the_sluice_and_spark_runs_the_sea_winch(game):
    give(game, "tide", "spark")
    put(game, "hearth")
    assert not game._open("hearth", "drowned_caves")
    assert game.use("tide")["ok"] and game._open("hearth", "drowned_caves")
    put(game, "drowned_caves")
    assert game.use("spark")["ok"] and game._open("drowned_caves", "sea_gate")


def test_a_used_spellstone_recharges_and_wrong_targets_do_nothing(game):
    give(game, "tide")
    put(game, "square")
    out = game.use("tide")
    assert not out["ok"] and "tide" not in game.s["cooldowns"]
    put(game, "hearth")
    assert game.use("tide")["ok"]
    assert "recharging" in game.use("tide")["say"]
    assert "don't have" in game.use("gale")["say"]


# ---- wands -------------------------------------------------------------------------------------------------
def test_only_the_owner_claims_a_wand_and_it_binds_the_spellstones(game):
    put(game, "timeless_hollow")
    game.s["effects"]["pearl"] = 1e9
    game.s["encounter"] = ""
    assert not game.take("wand")["ok"] and game.s["wand"] == ""
    give(game, "lantern", "gale")
    put(game, "black_vault_2")
    game.s["encounter"] = ""
    out = game.take("wand")
    assert out["ok"] and game.s["wand"] == "deep_cave_wand"
    assert set(game.s["absorbed"]) == {"lantern", "gale"} and game.s["carried"] == []
    assert not game.take("wand")["ok"]


def test_abilities_level_up_and_unlock_combos(game):
    game.s["wand"] = "deep_cave_wand"
    game.s["absorbed"] = {"fog": {"uses": 0}, "hush": {"uses": 0}}
    base = game._cooldown("fog")
    assert "needs" in game.combo("vanish")["say"]
    for _ in range(5):
        for st in ("fog", "hush"):
            game.s["cooldowns"].pop(st, None)
            assert game.use(st)["ok"]
    assert game._level("fog") == 2 and game._cooldown("fog") == pytest.approx(base * 0.8)
    game.s["cooldowns"].clear()
    assert game.combo("vanish")["ok"] and game._hidden_from_rivals()


def test_rivals_snatch_loose_stones_but_never_wand_abilities(game):
    give(game, "lantern")
    game.s["absorbed"] = {"gale": {"uses": 0}}
    game.s["wand"] = "deep_cave_wand"
    put(game, "harbour")
    game.s["encounter"] = ""
    for r in game.s["rivals"].values():
        r["place"] = "harbour"
    for _ in range(40):
        if not game.s["carried"]:
            break
        game.s["rival_next"] = game.s["now"]
        game._pass(10)
        for r in game.s["rivals"].values():
            r["place"] = "harbour"
    assert game.s["carried"] == [] and "gale" in game.s["absorbed"]
    thief = next(r for r, held in game.s["rival_held"].items() if "lantern" in held)
    game.s["rivals"][thief]["place"] = "harbour"
    held = game.s["rival_held"][thief]
    held.append(held.pop(held.index("lantern")))  # the Gale knocks loose what they grabbed last
    for r, v in game.s["rivals"].items():
        if r != thief:
            v["place"] = "watchtower"
    out = game.use("gale")
    assert "Lantern" in out["say"] and game.s["loose"]["lantern"] == "harbour"
    assert game.take("lantern")["ok"] and "lantern" in game.s["absorbed"]


# ---- creatures ---------------------------------------------------------------------------------------------
def test_creatures_wake_fight_and_are_defeated_their_own_way(game):
    game.s["effects"]["pearl"] = 1e9
    put(game, "frontier_ruins" if "frontier_ruins" in game.c.places else "sea_gate")
    game.go("heart of the deep")
    assert game.s["encounter"] == "tidewraith"
    game.s["health"] = 1e6
    for _ in range(30):
        if not game.s["encounter"]:
            break
        game.fight()
    assert game.s["creatures"]["tidewraith"]["state"] == "defeated"


def test_a_creature_can_knock_you_out(game):
    game.s["effects"]["pearl"] = 1e9
    put(game, "abyss_trench")
    game.s["creatures"]["abyssal_leviathan"]["state"] = "awake"
    game.s["encounter"] = "abyssal_leviathan"
    for _ in range(10):
        game.fight()
        if game.s["place"] == "hearth":
            break
    assert game.s["place"] == "hearth" and game.s["knockouts"] == 1 and game.s["health"] > 0


# ---- missions, salvage and the forge -----------------------------------------------------------------------
def test_reading_salvaging_and_forging_finish_the_forgemasters_secret(game):
    game.s["quests"]["breath_of_the_deep"]["done"] = True
    game.s["effects"]["pearl"] = 1e9
    give(game, "cipher")
    put(game, "archive")
    game.s["encounter"] = ""
    assert "Anchor" in game.use("cipher")["say"] and "anchor" in game.s["recipes"]
    put(game, "wreck_field")
    game.s["encounter"] = ""
    assert game.search()["ok"] and game.s["materials"]["blank"] == 2
    put(game, "forge_hall")
    game.s["materials"]["pearl_shell"] = 1
    out = game.forge("anchor")
    assert out["ok"] and "anchor" in game.s["carried"]
    assert game.s["quests"]["forgemasters_secret"]["done"]
    assert "already exists" in game.forge("anchor")["say"]


def test_missions_follow_what_you_actually_did(game):
    first = game.missions()["missions"]
    assert first[0]["title"] == "The Breath of the Deep" and first[0]["step"] == 1
    give(game, "pearl")
    put(game, "pearl_grotto")
    game._check_quests()
    assert game.s["quests"]["breath_of_the_deep"]["step"] == 2


def test_the_abyssal_threshold_opens_only_when_everything_is_done(game):
    put(game, "threshold_halls_2")
    game.s["encounter"] = ""
    assert not game._threshold_ready()
    give(game, "lantern", "cipher")
    for p in ("crystal_catacombs_0", "hollowmere_0"):
        game._visit(p)
    game.s["dynamic"]["the_descent"] = {"step": 3, "done": False}
    game._check_quests()
    assert game.s["dynamic"]["the_descent"]["done"]
    assert game._open("threshold_halls_2", "beyond_threshold")


# ---- saving ------------------------------------------------------------------------------------------------
def test_the_game_saves_after_every_move_and_loads_the_same(tmp_path, canon):
    g = ImaginationGame(tmp_path / "s.json", canon=canon, seed=3)
    g.new_game(skip_intro=True)
    g.go("hearthstone house")
    g.take("lantern")
    again = ImaginationGame(tmp_path / "s.json", canon=canon)
    assert again.s == g.s
    (tmp_path / "s.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ImaginationError):
        ImaginationGame(tmp_path / "s.json", canon=canon)


def test_the_same_seed_plays_the_same_way(tmp_path, canon):
    runs = []
    for n in range(2):
        g = ImaginationGame(tmp_path / f"r{n}.json", canon=canon, seed=11)
        g.new_game(skip_intro=True)
        give(g, "lantern")
        for place in ("hearthstone house", "the harbour", "the lighthouse", "the cliff path"):
            g.go(place)
        g.wait(20)
        runs.append(json.dumps(g.s, sort_keys=True))
    assert runs[0] == runs[1]


# ---- the Book Writer knows everything ----------------------------------------------------------------------
def test_jarvis_book_writer_gets_the_whole_canon(library, canon):
    out = teach(library, canon)
    book = library.find_book(BOOK_TITLE)
    assert out["updated"] and book.title == "Imagination"
    bible = library.list_bible(book.id)
    names = {e.name for e in bible}
    assert {p["name"] for p in canon.places.values()} <= names
    assert {s["name"] for s in canon.stones.values()} <= names
    assert {"Deep Cave Wand", "Deep Sea Wand", "Sky Wand", "Noah", "Angelic", "Uncle Dominic", "Vex",
            "Abyssal Leviathan", "The Breath of the Deep", "The Descent", "Hour Gate" if False else "Harbour Clock"} <= names
    kinds = {e.kind for e in bible}
    assert {"character", "location", "item", "rule", "thread", "event", "theme", "style"} <= kinds
    notes = [n for n in library.list_notes(book.id) if n.source == LORE_SOURCE]
    assert len(notes) == 1 and "Whispering Wells" in notes[0].text
    again = teach(library, canon)
    assert not again["updated"] and len(library.list_bible(book.id)) == len(bible)
    assert len([b for b in library.list_books() if b.title == "Imagination"]) == 1


def test_lore_answers_questions_from_the_canon(canon):
    out = lookup(canon, "tidewraith")
    assert "drowned watchman" in out["say"]
    assert lookup(canon, "who is vex")["facts"][0]["name"] == "Vex"
    assert lookup(canon, "zzzz qqqq")["facts"] == []


# ---- Jarvis plays it with the player -----------------------------------------------------------------------
@pytest.fixture
def tools(library, make_engine, tmp_path, canon):
    from fakes import FakeGemini
    from jarvis_books.assistant import ToolBox
    from jarvis_books.jobs import JobManager
    from jarvis_books.memory import StoryIndex
    from jarvis_books.writer import BookWriter
    engine = make_engine(FakeGemini())
    index = StoryIndex(library, engine)
    jobs = JobManager(library, workers=1)
    box = ToolBox(library, BookWriter(library, engine, index), index, jobs)
    box.imagination = ImaginationGame(tmp_path / "jarvis_imagination.json", canon=canon, seed=5)
    yield box
    jobs.shutdown(wait=True)


def test_every_imagination_tool_has_a_permission_and_comes_up_when_spoken(tools):
    from jarvis_books.assistant.imagination_tools import IMAGINATION_TOOLS
    from jarvis_books.assistant.permissions import TOOL_LEVELS
    assert set(IMAGINATION_TOOLS) <= set(TOOL_LEVELS)
    offered = {d["name"] for d in tools.select("jarvis, go to the cliff path and use the lantern spellstone")}
    assert {"imagination_go", "imagination_use"} <= offered
    assert "imagination_status" in {d["name"] for d in tools.select("let's play imagination")}


def test_jarvis_plays_imagination_by_tool_calls(tools):
    assert "bedroom" in tools.call("imagination_status")["say"]
    for action in ("do my homework", "eat dinner", "turn off the lights", "go to sleep", "wake up"):
        assert tools.call("imagination_story", {"action": action})["ok"]
    assert tools.call("imagination_go", {"place": "hearthstone house"})["place"] == "Hearthstone House"
    assert tools.call("imagination_take", {"thing": "lantern"})["ok"]
    assert "Tilt" in tools.call("imagination_use", {"spellstone": "lantern"})["say"]
    assert "Noah" in tools.call("imagination_companions", {})["say"]
    assert tools.call("imagination_companions", {"companion": "noah", "order": "stay"})["ok"]
    assert tools.call("imagination_missions")["missions"]
    assert "error" in tools.call("imagination_go", {})
    assert tools.call("imagination_go", {"place": "nowhere land"})["ok"] is False
    asked = tools.call("imagination_new", {"skip_intro": True})
    assert asked.get("needs_approval")


def test_jarvis_tool_fills_the_imagination_book(tools, library):
    out = tools.call("imagination_teach_book")
    assert out["entries"] > 150 and library.find_book("Imagination")


def test_a_game_move_without_a_tool_call_is_asked_again():
    from jarvis_books.assistant.imagination_tools import asks_for_imagination_move
    assert asks_for_imagination_move("Jarvis, go to the lighthouse in Imagination")
    assert asks_for_imagination_move("use the lantern spellstone")
    assert not asks_for_imagination_move("what's the weather like")
