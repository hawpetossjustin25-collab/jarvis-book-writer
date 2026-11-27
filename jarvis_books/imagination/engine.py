"""Imagination played by talking to Jarvis.

The world is the real one from the Godot game (canon.json): the same 116 places and paths, travel times, clocks
and portals, spellstones, wands, rivals, creatures and missions. Everything that happens is decided by rules and a
seeded dice roll, never by a language model, so the game plays the same with every brain offline. Every action
returns a short spoken answer ("say") and saves the game.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import os
from pathlib import Path
from typing import Optional

from jarvis_books.imagination.canon import Canon, load_canon, norm

PLAYER = "player"
MAX_HEALTH = 100
STEP_S = 10.0
CLOCK_INTERVAL_S = 300.0
PORTAL_OPEN_S = 150.0
RIVAL_TURN_S = 600.0
XP_PER_LEVEL = 5
MAX_LEVEL = 3
LEVEL_CUT = 0.2
COMBO_LEVEL = 2
HIDDEN_AT_START = {"tilt": "lantern"}
WARD_S = 1800.0
FOOTPRINT_S = 3600.0
SWIMMERS = {"grist"}
WEATHERS = ["clear", "rain", "fog", "storm"]
CAVE_START = "cave_mouth_0"
OPENING_TASKS = {
    "homework": ("homework", "study", "school", "math"),
    "dinner": ("dinner", "eat", "food", "supper"),
    "lights": ("lights", "light", "lamp", "bed ready", "pajamas", "teeth"),
}
DREAM = ("a dark ocean that goes down forever", "the mouth of a cave breathing cold air",
         "a clock with too many hands, all of them turning", "a symbol you have never seen, glowing like a coal",
         "islands floating above the clouds")


def _roll(*parts) -> float:
    h = hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def _key(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))


def _minutes(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return f"{m} minute{'s' if m != 1 else ''}"


class ImaginationError(ValueError):
    """A spoken request the game can't carry out, with the reason Jarvis says out loud."""


class ImaginationGame:
    def __init__(self, path: Optional[Path] = None, canon: Optional[Canon] = None, seed: Optional[int] = None):
        self.c = canon or load_canon()
        self.path = Path(path) if path else None
        self.seed = int(seed if seed is not None else self.c.data.get("seed", 1))
        self.s: dict = {}
        if self.path and self.path.exists():
            self._load()
        else:
            self.s = self._fresh()

    # ---------- state ----------

    def _fresh(self, skip_intro: bool = False) -> dict:
        c = self.c
        closed = {_key(l["a"], l["b"]): False for l in c.data["links"] if not l["open"]}
        loose = {sid: s["where"] for sid, s in c.stones.items() if s["where"] in c.places}
        creatures = {}
        for a in c.agents.values():
            if a["role"] == "creature":
                prof = c.creature_profiles.get(a["id"]) or c.creature_profiles.get(a["id"].rsplit("_", 1)[0])
                creatures[a["id"]] = {"profile": prof["creature_id"], "place": a["home"], "lair": a["home"],
                                      "health": prof["maximum_health"], "state": "dormant", "frozen_until": 0.0}
        s = {
            "version": 1, "seed": self.seed, "now": 0.0, "opening": "bedroom", "tasks_done": [],
            "place": "", "prev_place": "", "health": MAX_HEALTH, "visited": [], "sign_seen": False,
            "carried": [], "wand": "", "absorbed": {}, "cooldowns": {}, "effects": {},
            "loose": loose, "hidden": dict(HIDDEN_AT_START), "rival_held": {}, "history": {},
            "materials": {}, "recipes": [], "salvaged": [], "read": [], "forged": [], "portals_used": [],
            "links": closed, "weather": "clear",
            "clocks": {k: {"next_at": v["next_at"], "chimes": 0} for k, v in c.clocks.items()},
            "portals": {k: -1.0 for k in c.portals},
            "companions": {a["id"]: {"place": a["home"], "order": "stay", "met": False}
                           for a in c.agents.values() if a["role"] == "companion"},
            "rivals": {a["id"]: {"place": a["home"], "stunned_until": 0.0}
                       for a in c.agents.values() if a["role"] == "rival"},
            "footprints": [], "creatures": creatures, "encounter": "",
            "quests": {q: {"step": 0, "done": False} for q in c.quest_order}, "dynamic": {},
            "wands": {w: {"holder": ""} for w in c.wands}, "rival_next": RIVAL_TURN_S,
            "news": [], "beats": [], "knockouts": 0,
        }
        self.s = s
        if skip_intro:
            self._wake()
        return s

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ImaginationError(f"The saved Imagination game can't be read ({e}). Start a new one.") from e
        if data.get("version") != 1:
            raise ImaginationError("That save is from a different version of Imagination.")
        fresh = self._fresh()
        fresh.update(data)
        self.s = fresh

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def _done(self, say: str, ok: bool = True, **extra) -> dict:
        news, self.s["news"] = self.s["news"], []
        if news:
            say = say + " " + " ".join(news)
        self.save()
        return {"ok": ok, "say": say, "place": self.c.name(self.s["place"]) if self.s["place"] else "your bedroom",
                "health": round(self.s["health"]), "time": _minutes(self.s["now"]) + " in", **extra}

    def _news(self, text: str) -> None:
        self.s["news"].append(text)

    def _awake(self) -> None:
        if self.s["opening"] != "done":
            raise ImaginationError("You're still at home in the ordinary world. Finish your day and go to sleep "
                                   "first: say things like 'do my homework', 'eat dinner', 'turn off the lights' "
                                   "and 'go to sleep'.")

    # ---------- new game and the opening ----------

    def new_game(self, skip_intro: bool = False) -> dict:
        self._fresh(skip_intro)
        if skip_intro:
            return self._done("New game of Imagination. You wake up in the town square of Gullhaven. "
                              + self._look_text())
        return self._done("New game of Imagination. It's the end of an ordinary day, in your ordinary bedroom. "
                          "There's homework on the desk, dinner waiting downstairs, and the lights are still on.")

    def story(self, action: str = "") -> dict:
        s, a = self.s, norm(action)
        if s["opening"] == "done":
            return self._done("The opening is over. You're in Imagination now. " + self._look_text())
        if "skip" in a:
            self._wake()
            return self._done("Skipping ahead. " + self._look_text())
        if s["opening"] == "bedroom":
            for task, words in OPENING_TASKS.items():
                if any(w in a for w in words) and task not in s["tasks_done"]:
                    s["tasks_done"].append(task)
                    left = [t for t in OPENING_TASKS if t not in s["tasks_done"]]
                    lines = {"homework": "You finish your homework. Your eyes are getting heavy.",
                             "dinner": "You eat dinner. It's warm and you're getting sleepy.",
                             "lights": "You turn off the lights. Only the moon is left in the window."}
                    if not left:
                        s["opening"] = "tired"
                        return self._done(lines[task] + " You're so tired you can barely stand. Time to sleep.")
                    return self._done(lines[task] + " Still to do: " + ", ".join(left) + ".")
            if "sleep" in a or "bed" in a:
                left = [t for t in OPENING_TASKS if t not in s["tasks_done"]]
                return self._done("You're not tired enough yet. Still to do: " + ", ".join(left) + ".")
            return self._done("Your bedroom: a desk with homework, a bed, a window with the moon in it. "
                              "Dinner is downstairs and the lights are on.")
        if s["opening"] == "tired":
            if "sleep" in a or "bed" in a or "lie" in a:
                s["opening"] = "dream"
                return self._done("You lie down and fall asleep. The room fades. You dream: " + "; ".join(DREAM)
                                  + ". Say 'wake up'.")
            return self._done("You can barely keep your eyes open. Say 'go to sleep'.")
        if s["opening"] == "dream":
            if any(w in a for w in ("wake", "open", "up", "get")):
                self._wake()
                return self._done("You open your eyes, and this isn't your bedroom. " + self._look_text())
            return self._done("The dream goes on: " + DREAM[int(_roll(self.seed, a) * len(DREAM)) % len(DREAM)]
                              + ". Say 'wake up'.")
        return self._done(self._look_text())

    def _wake(self) -> None:
        s = self.s
        s["opening"], s["place"] = "done", "square"
        self._visit("square")

    # ---------- time ----------

    def _effect(self, name: str) -> bool:
        return self.s["effects"].get(name, -1.0) > self.s["now"]

    def _hidden_from_rivals(self) -> bool:
        return self._effect("fog") or self._effect("hush") or self._effect("vanish")

    def _pass(self, seconds: float) -> None:
        end = self.s["now"] + max(0.0, seconds)
        while self.s["now"] < end - 1e-6:
            dt = min(STEP_S, end - self.s["now"])
            self.s["now"] += dt
            self._tick(dt)

    def _tick(self, dt: float) -> None:
        s, c, now = self.s, self.c, self.s["now"]
        for cid, clock in s["clocks"].items():
            if now >= clock["next_at"]:
                clock["chimes"] += 1
                clock["next_at"] += CLOCK_INTERVAL_S * (0.75 + 0.5 * _roll(self.seed, cid, clock["chimes"]))
                self._chime(cid)
        here = s["place"]
        if here and c.underwater(here) and not self._effect("pearl"):
            s["health"] -= 1.5 * dt
            if "drowning" not in s["effects"]:
                s["effects"]["drowning"] = now
                self._news("Jarvis: you have no air! Use the Abyssal Breath or get out of the water.")
        else:
            s["effects"].pop("drowning", None)
        enc = s["encounter"]
        if enc:
            cr = s["creatures"][enc]
            if cr["state"] == "awake" and cr["place"] == here and cr["frozen_until"] <= now:
                power = c.creature_profiles[cr["profile"]]["attack_power"]
                s["health"] -= power * dt / 30.0 * (0.5 if self._effect("anchor") else 1.0)
        elif s["health"] < MAX_HEALTH and "drowning" not in s["effects"]:
            s["health"] = min(MAX_HEALTH, s["health"] + dt / 15.0)
        if s["health"] <= 0:
            self._knockout()
        if now >= s["rival_next"]:
            s["rival_next"] += RIVAL_TURN_S
            self._rivals_turn()

    def _chime(self, cid: str) -> None:
        s, c = self.s, self.c
        clock = c.clocks[cid]
        heard = s["place"] in (clock["place"],) or s["place"] in c.links.get(clock["place"], {})
        for pid, p in c.portals.items():
            if p["clock"] == cid:
                s["portals"][pid] = s["now"] + PORTAL_OPEN_S
                if heard or s["place"] in (p["place"], p["era_place"]):
                    self._news(f"The {clock['name']} chimes. The {p['name']} is open for "
                               f"{_minutes(PORTAL_OPEN_S)}.")
                if (pid == "cliffside_rift" and s["place"] == p["place"] and not self._been_in_cave()
                        and CAVE_START in c.places):
                    self._news("The ground opens under your feet! The Cliffside Rift pulls you down into the "
                               "Deep Cave. The rift will bring you back up when it opens again.")
                    self._arrive(CAVE_START, via="rift")
        if heard and not any(p["clock"] == cid for p in c.portals.values()):
            self._news(f"The {clock['name']} chimes.")
        self._beat("clocks", "Jarvis: that was one of the clocks of Imagination. When a clock chimes, the "
                             "portal tied to it opens for a short time. Ask me 'when do the clocks chime?'")

    def _knockout(self) -> None:
        s = self.s
        s["knockouts"] += 1
        s["health"] = 60
        if s["encounter"]:
            s["creatures"][s["encounter"]]["state"] = "dormant"
            s["creatures"][s["encounter"]]["place"] = s["creatures"][s["encounter"]]["lair"]
        s["encounter"] = ""
        s["effects"].pop("drowning", None)
        rescuer = self.c.agents.get("rescuer", {}).get("name", "The rescuer")
        s["place"] = "hearth"
        self._visit("hearth")
        self._news(f"Everything goes black. {rescuer} finds you and carries you home to Hearthstone House. "
                   "You wake up on the parlour sofa, sore but safe.")

    def wait(self, minutes: float = 5) -> dict:
        self._awake()
        minutes = max(0.5, min(float(minutes or 5), 60.0))
        self._pass(minutes * 60)
        return self._done(f"You wait {_minutes(minutes * 60)}.")

    # ---------- the map ----------

    def _open(self, a: str, b: str) -> bool:
        k = _key(a, b)
        if k in self.s["links"]:
            if self.s["links"][k]:
                return True
            for pid, p in self.c.portals.items():
                if _key(p["place"], p["era_place"]) == k and self.s["portals"][pid] > self.s["now"]:
                    return True
            return False
        return b in self.c.links.get(a, {})

    def _leg_s(self, a: str, b: str) -> float:
        t = float(self.c.links[a][b]["seconds"])
        if self._effect("pelagic") and self.c.underwater(a) and self.c.underwater(b):
            t /= 3
        return t

    def _route(self, start: str, goal: str, allowed=None) -> list:
        dist, prev, todo = {start: 0.0}, {}, [(0.0, start)]
        while todo:
            d, p = heapq.heappop(todo)
            if p == goal:
                break
            if d > dist.get(p, 1e18):
                continue
            for q in self.c.links.get(p, {}):
                if not self._open(p, q) or (allowed and not allowed(q)):
                    continue
                nd = d + float(self.c.links[p][q]["seconds"])
                if nd < dist.get(q, 1e18):
                    dist[q], prev[q] = nd, p
                    heapq.heappush(todo, (nd, q))
        if goal not in dist:
            return []
        path = [goal]
        while path[-1] != start:
            path.append(prev[path[-1]])
        return path[::-1]

    def _visit(self, place: str) -> None:
        if place not in self.s["visited"]:
            self.s["visited"].append(place)

    def _been_in_cave(self) -> bool:
        return any(self.c.in_cave(p) for p in self.s["visited"])

    def _here_things(self, place: str) -> dict:
        s, c = self.s, self.c
        stones = [st for st, p in s["loose"].items() if p == place and st not in s["hidden"]]
        rivals = [r for r, v in s["rivals"].items() if v["place"] == place]
        creatures = [k for k, v in s["creatures"].items() if v["place"] == place and v["state"] != "defeated"]
        friends = [k for k, v in s["companions"].items() if v["place"] == place and v["met"]]
        objects = [o for o in c.objects.values() if o["place"] == place and not o.get("hidden")]
        return {"stones": stones, "rivals": rivals, "creatures": creatures, "friends": friends, "objects": objects}

    def _look_text(self) -> str:
        s, c = self.s, self.c
        here = s["place"]
        p = c.places[here]
        where = c.region_name(p.get("region", ""))
        bits = [f"You're at {p['name']}" + (f", in {p['zone']}" if p.get("zone") else f", {where}") + "."]
        if c.underwater(here):
            bits.append("You're underwater" + (", breathing through the Abyssal Breath." if self._effect("pearl")
                                               else " with no air!"))
        if here == "square" and not s["sign_seen"]:
            s["sign_seen"] = True
            bits.append("An old painted sign over the square reads: 'Gullhaven, in the land of IMAGINATION'. "
                        "Somehow you know the name was always there. IMAGINATION.")
            self._beat("look", "Jarvis: I'm here with you. You can talk to me the whole time. Try 'where can I "
                               "go?' or 'go to Main Street'.")
        t = self._here_things(here)
        if t["stones"]:
            bits.append("Glowing on the ground: " + ", ".join(c.stone_name(x) for x in t["stones"]) + ".")
        for o in t["objects"]:
            if o["type"] in ("salvage",) and o["id"] in s["salvaged"]:
                continue
            if o["type"] not in ("room",):
                bits.append(f"There's the {o['name']}.")
        for w, v in c.wands.items():
            if v["place"] == here and not s["wands"][w]["holder"]:
                bits.append(f"The {v['name']} rests here" + ("." if v["owner"] == PLAYER
                                                              else ", but it belongs to another player."))
        if t["friends"]:
            bits.append(" and ".join(c.agents[f]["name"] for f in t["friends"]) + " are with you."
                        if len(t["friends"]) > 1 else c.agents[t["friends"][0]]["name"] + " is with you.")
        if t["rivals"]:
            bits.append("Rival here: " + ", ".join(c.agents[r]["name"] for r in t["rivals"]) + "!")
        for k in t["creatures"]:
            v = s["creatures"][k]
            name = c.creature_profiles[v["profile"]]["creature_name"]
            bits.append(f"The {name} is awake and coming for you!" if v["state"] == "awake"
                        else f"Something sleeps here: a {name}. Careful.")
        exits = [q for q in c.links.get(here, {}) if self._open(here, q)]
        bits.append("Paths to " + ", ".join(c.name(q) for q in exits[:8]) + "." if exits else "No open paths.")
        for pid, pr in c.portals.items():
            if here in (pr["place"], pr["era_place"]):
                bits.append(f"The {pr['name']} is " + ("open! Say 'step through'." if s["portals"][pid] > s["now"]
                                                        else "closed until its clock chimes."))
        return " ".join(bits)

    def look(self) -> dict:
        self._awake()
        t = self._here_things(self.s["place"])
        return self._done(self._look_text(), exits=[self.c.name(q) for q in self.c.links.get(self.s["place"], {})
                                                     if self._open(self.s["place"], q)],
                          spellstones=[self.c.stone_name(x) for x in t["stones"]])

    def go(self, place: str) -> dict:
        self._awake()
        s, c = self.s, self.c
        if norm(place) in ("back", "go back", "previous", "away", "run", "flee") and s["prev_place"]:
            goal = s["prev_place"]
        else:
            goal = c.find_place(place)
        if not goal:
            return self._done(f"I don't know a place called '{place}' in Imagination.", ok=False)
        if goal == s["place"]:
            return self._done(f"You're already at {c.name(goal)}.")
        route = self._route(s["place"], goal)
        if not route:
            return self._done(self._blocked_reason(goal), ok=False)
        if s["encounter"]:
            cr = s["creatures"][s["encounter"]]
            name = c.creature_profiles[cr["profile"]]["creature_name"]
            s["health"] -= c.creature_profiles[cr["profile"]]["attack_power"] * 0.5
            s["encounter"] = ""
            self._news(f"The {name} strikes at you as you run.")
            if s["health"] <= 0:
                self._knockout()
                return self._done("You didn't get away.", ok=False)
        walked, total = [], 0.0
        for a, b in zip(route, route[1:]):
            if c.underwater(b) and not c.underwater(a) and not self._effect("pearl"):
                return self._done((f"You go {self._said(walked)} and " if walked else "")
                                  + f"stop at the water's edge at {c.name(a)}. {c.name(b)} is underwater, and "
                                  "you can't breathe down there. Use the Abyssal Breath first.", ok=False)
            if self._threshold_link(a, b) and not self._threshold_ready():
                return self._done(f"You reach the Abyssal Threshold. It won't open. Still missing: "
                                  + ", ".join(self._threshold_missing()) + ".", ok=False)
            leg = self._leg_s(a, b)
            self._pass(leg)
            total += leg
            if s["place"] != a:
                return self._done("Your trip was cut short.", ok=False)
            self._arrive(b)
            walked.append(b)
            if s["encounter"] or s["place"] != b:
                break
        return self._done(f"You travel {self._said(walked)}. It took {_minutes(total)}. " + self._look_text(),
                          route=[c.name(p) for p in walked])

    def _said(self, walked: list) -> str:
        if not walked:
            return "nowhere"
        if len(walked) <= 3:
            return "to " + ", then ".join(self.c.name(p) for p in walked)
        return f"through {len(walked) - 1} places to {self.c.name(walked[-1])}"

    def _blocked_reason(self, goal: str) -> str:
        c = self.c
        if c.in_cave(goal) and not c.in_cave(self.s["place"]):
            return (f"{c.name(goal)} is in the Deep Cave. You can't walk there: the only ways in are the "
                    "Cliffside Rift at the Cliff Path and the Drowned Rift in the Drowned Caves, when their clocks "
                    "chime.")
        for pid, p in c.portals.items():
            if goal == p["era_place"]:
                return f"{c.name(goal)} is only reachable through the {p['name']} at {c.name(p['place'])}."
        return (f"There's no open way to {c.name(goal)} from here. Something is sealed on the way: look for a "
                "gate, a lock or a flooded tunnel a spellstone can open.")

    def _arrive(self, place: str, via: str = "") -> None:
        s, c = self.s, self.c
        s["prev_place"], s["place"] = s["place"], place
        first = place not in s["visited"]
        self._visit(place)
        for f, v in s["companions"].items():
            if v["met"] and v["order"] == "follow" and (not c.underwater(place) or self._effect("pearl")):
                v["place"] = place
        if not all(v["met"] for v in s["companions"].values()) and place in ("hearth", "main_street", "gardens"):
            for v in s["companions"].values():
                v["met"], v["order"], v["place"] = True, "follow", place
            self._news("Two people run up to you. 'There you are! I'm Noah,' says the tall one. 'And I'm Angelic,' "
                       "says the other. 'We've been waiting for you. We'll stick with you.' They're your companions "
                       "now: you can tell them to follow, stay, guard a place or search for spellstones.")
        if place == "hearth":
            self._beat("missions", "Jarvis: this is Hearthstone House, your home in Imagination. You have missions "
                                   "now. Ask me 'what are my missions?'")
        if c.in_cave(place) and "the_descent" not in s["dynamic"]:
            s["dynamic"]["the_descent"] = {"step": 0, "done": False}
            self._news("New mission: The Descent. Find your way through the Deep Cave.")
        for k, v in s["creatures"].items():
            if v["place"] == place and v["state"] == "dormant":
                v["state"] = "awake"
                s["encounter"] = k
                name = c.creature_profiles[v["profile"]]["creature_name"]
                self._news(f"Jarvis: careful! The {name} wakes up! Fight it, use a spellstone on it, or run.")
                break
            if v["place"] == place and v["state"] == "awake":
                s["encounter"] = k
        if first and len(s["visited"]) == 3:
            self._beat("spellstones", "Jarvis: the glowing stones you'll find are spellstones. Each has one power. "
                                      "Pick one up and say 'use the' and its name.")
        if self._threshold_link_here() and self._threshold_ready():
            self._open_threshold()
        self._check_quests()

    def _beat(self, beat: str, text: str) -> None:
        if beat not in self.s["beats"]:
            self.s["beats"].append(beat)
            self._news(text)

    def step_through(self) -> dict:
        self._awake()
        s, c = self.s, self.c
        for pid, p in c.portals.items():
            if s["place"] in (p["place"], p["era_place"]):
                if s["portals"][pid] <= s["now"]:
                    clock = s["clocks"][p["clock"]]
                    return self._done(f"The {p['name']} is closed. Its clock, the {c.clocks[p['clock']]['name']}, "
                                      f"chimes in about {_minutes(clock['next_at'] - s['now'])}.", ok=False)
                to = p["era_place"] if s["place"] == p["place"] else p["place"]
                if pid not in s["portals_used"]:
                    s["portals_used"].append(pid)
                s["encounter"] = ""
                self._arrive(to, via="portal")
                return self._done(f"You step through the {p['name']}. " + self._look_text())
        return self._done("There's no portal here.", ok=False)

    def map(self) -> dict:
        self._awake()
        s, c = self.s, self.c
        known = [p for p in s["visited"]]
        edge = sorted({q for p in known for q in c.links.get(p, {}) if q not in known})
        return self._done(f"You've been to {len(known)} of the {len(c.places)} places in Imagination: "
                          + ", ".join(c.name(p) for p in known[-12:]) + f". {len(edge)} more are right next to "
                          "places you know.", visited=[c.name(p) for p in known])

    # ---------- spellstones and the wand ----------

    def _has(self, stone: str) -> bool:
        return stone in self.s["carried"] or stone in self.s["absorbed"]

    def _level(self, stone: str) -> int:
        a = self.s["absorbed"].get(stone)
        return min(MAX_LEVEL, 1 + a["uses"] // XP_PER_LEVEL) if a else 1

    def _cooldown(self, stone: str) -> float:
        return float(self.c.stones[stone]["cooldown_s"]) * (1 - LEVEL_CUT * (self._level(stone) - 1))

    def take(self, thing: str = "") -> dict:
        self._awake()
        s, c = self.s, self.c
        here, q = s["place"], norm(thing)
        if "wand" in q or not q:
            for w, v in c.wands.items():
                if v["place"] == here and not s["wands"][w]["holder"]:
                    if v["owner"] != PLAYER:
                        return self._done(f"The {v['name']} won't move for you. It belongs to another player, and "
                                          "only they can claim it.", ok=False)
                    if s["wand"]:
                        return self._done("You already have your wand. Each player gets one.", ok=False)
                    s["wands"][w]["holder"], s["wand"] = PLAYER, w
                    for st in list(s["carried"]):
                        self._absorb(st)
                    return self._done(f"You take the {v['name']}. It's warm, and it hums. "
                                      + (f"It drinks in your spellstones: {', '.join(c.stone_name(x) for x in s['absorbed'])}. "
                                         "They're bound to it now, and nobody can steal them." if s["absorbed"] else
                                         "From now on every spellstone you pick up becomes part of it."))
        here_stones = self._here_things(here)["stones"]
        stone = c.find_stone(thing, here_stones) if q else (here_stones[0] if here_stones else "")
        if not stone:
            if here_stones:
                return self._done("Here you can take: " + ", ".join(c.stone_name(x) for x in here_stones) + ".",
                                  ok=False)
            return self._done("There's nothing here to pick up." if not q else f"There's no {thing} here.", ok=False)
        del s["loose"][stone]
        s["history"].setdefault(stone, []).append(["you", round(s["now"])])
        if s["wand"]:
            self._absorb(stone)
            say = f"You pick up the {c.stone_name(stone)} and your wand absorbs it. {c.stones[stone]['power']}."
        else:
            s["carried"].append(stone)
            say = (f"You pick up the {c.stone_name(stone)}. Its power: {c.stones[stone]['power']}. Careful, rivals "
                   "can steal stones you carry until your wand absorbs them.")
        self._check_quests()
        return self._done(say)

    def _absorb(self, stone: str) -> None:
        s = self.s
        if stone in s["carried"]:
            s["carried"].remove(stone)
        s["absorbed"].setdefault(stone, {"uses": 0})

    def wand(self) -> dict:
        self._awake()
        s, c = self.s, self.c
        if not s["wand"]:
            v = c.wands
            return self._done(f"You don't have a wand yet. Yours is the {v['deep_cave_wand']['name']}, hidden in "
                              f"{c.name(v['deep_cave_wand']['place'])} in the Deep Cave. The "
                              f"{v['deep_sea_wand']['name']} belongs to player two and the {v['sky_wand']['name']} "
                              "to player three.", carried=[c.stone_name(x) for x in s["carried"]])
        lines = [f"{c.stone_name(x)} level {self._level(x)}" for x in sorted(s["absorbed"])]
        combos = [cb[3] for cb in c.combos if self._combo_ready(cb)]
        return self._done(f"Your {c.wands[s['wand']]['name']} holds " + (", ".join(lines) or "no spellstones yet")
                          + "." + (f" Combos ready: {', '.join(combos)}." if combos else ""),
                          abilities={c.stone_name(x): self._level(x) for x in s["absorbed"]}, combos=combos)

    def _combo_ready(self, cb: list) -> bool:
        a = self.s["absorbed"]
        return bool(self.s["wand"]) and all(x in a and self._level(x) >= COMBO_LEVEL for x in cb[:2])

    def use(self, ability: str, target: str = "") -> dict:
        self._awake()
        s, c = self.s, self.c
        stone = c.find_stone(ability)
        if not stone:
            cb = self._find_combo(ability)
            if cb:
                return self.combo(ability)
            return self._done(f"There's no spellstone called '{ability}'.", ok=False)
        if not self._has(stone):
            return self._done(f"You don't have the {c.stone_name(stone)}." + self._where_hint(stone), ok=False)
        ready = s["cooldowns"].get(stone, 0.0)
        if ready > s["now"]:
            return self._done(f"The {c.stone_name(stone)} is still recharging, about "
                              f"{_minutes(ready - s['now'])} to go.", ok=False)
        ok, say = getattr(self, "_p_" + stone)(target)
        if ok:
            s["cooldowns"][stone] = s["now"] + self._cooldown(stone)
            if stone in s["absorbed"]:
                before = self._level(stone)
                s["absorbed"][stone]["uses"] += 1
                if self._level(stone) > before:
                    say += f" Your {c.stone_name(stone)} grew to level {self._level(stone)}: it recharges faster."
            self._pass(5)
            self._check_quests()
        return self._done(say, ok=ok)

    def _where_hint(self, stone: str) -> str:
        s, c = self.s, self.c
        if stone in s["loose"] and stone not in s["hidden"] and stone in s["history"]:
            return f" It was dropped at {c.name(s['loose'][stone])}."
        for r, held in s["rival_held"].items():
            if stone in held:
                return f" {c.agents[r]['name']} has it."
        if not c.stones[stone]["where"]:
            return " It doesn't exist yet: it has to be forged in Pelagoria's Forge Hall."
        return ""

    def _effect_on(self, name: str, seconds: float) -> None:
        self.s["effects"][name] = self.s["now"] + seconds

    def _objects(self, kind: str) -> list:
        here = self.s["place"]
        return [o for o in self.c.objects.values() if o["type"] == kind and
                (o["place"] == here or here in o.get("links", []))]

    def _open_link(self, a: str, b: str) -> bool:
        k = _key(a, b)
        if k in self.s["links"] and not self.s["links"][k]:
            self.s["links"][k] = True
            return True
        return False

    def _hostile(self) -> tuple:
        s = self.s
        rivals = [r for r, v in s["rivals"].items() if v["place"] == s["place"]]
        return (s["encounter"] or "", rivals)

    def _hit_creature(self, damage: float) -> str:
        s, c = self.s, self.c
        k = s["encounter"]
        cr = s["creatures"][k]
        prof = c.creature_profiles[cr["profile"]]
        cr["health"] = max(0.0, cr["health"] - damage)
        if cr["health"] <= 0:
            cr["state"], s["encounter"] = "defeated", ""
            word = {"sealing": "sealed away", "banishment": "banished"}.get(prof["defeat_mode"], "defeated")
            return f"The {prof['creature_name']} is {word}!"
        return f"The {prof['creature_name']} has {round(cr['health'])} of {round(prof['maximum_health'])} health left."

    def _p_anchor(self, _t):
        self._effect_on("anchor", self.c.stones["anchor"]["duration_s"])
        return True, "You root yourself to the ground. For a minute nothing can knock you down, and hits hurt half."

    def _p_beacon(self, _t):
        s = self.s
        came = []
        for f, v in s["companions"].items():
            if v["met"] and v["place"] != s["place"] and self._route(v["place"], s["place"]):
                v["place"], v["order"] = s["place"], "follow"
                came.append(self.c.agents[f]["name"])
        self._effect_on("beacon", self.c.stones["beacon"]["duration_s"])
        return True, "Your beacon flares. " + (" and ".join(came) + " come running." if came
                                                else "Everyone who can reach you is already here.")

    def _p_bell(self, _t):
        _, rivals = self._hostile()
        for r in rivals:
            self.s["rivals"][r]["place"] = self.c.agents[r]["home"]
        return True, ("The bell rings out. " + ", ".join(self.c.agents[r]["name"] for r in rivals)
                      + " run back to their hideouts." if rivals else "The bell rings out. No rivals were near.")

    def _p_burrow(self, _t):
        if not self._objects("earth_wall"):
            return False, "There's no earth wall here to dig through."
        return True, "You dig a tunnel through the earth."

    def _p_cipher(self, _t):
        s = self.s
        texts = [o for o in self.c.objects.values() if o["place"] == s["place"] and o.get("text")]
        if not texts:
            return False, "There's no writing here to read."
        out = []
        for o in texts:
            out.append(f"The {o['name']} says: {o['text']}")
            if o["id"] not in s["read"]:
                s["read"].append(o["id"])
            recipe = o.get("state", {}).get("recipe")
            if recipe and recipe not in s["recipes"]:
                s["recipes"].append(recipe)
                out.append(f"You learned how to forge the {self.c.stone_name(recipe)}.")
        return True, " ".join(out)

    def _p_clockwork(self, _t):
        if not self._objects("workbench"):
            return False, "You need a workbench to build a clockwork helper. There's one at the Forge Yard."
        self._effect_on("helper", self.c.stones["clockwork"]["duration_s"])
        return True, "You build a little clockwork helper. It ticks along beside you and fights at your side."

    def _nearest_stone(self) -> tuple:
        s, best = self.s, ("", [])
        for st, p in s["loose"].items():
            if st in s["hidden"]:
                continue
            r = self._route(s["place"], p) if p != s["place"] else [p]
            if r and (not best[0] or len(r) < len(best[1])):
                best = (st, r)
        return best

    def _p_compass(self, _t):
        st, route = self._nearest_stone()
        if not st:
            return True, "The compass spins. There are no unclaimed spellstones you can reach right now."
        p = self.s["loose"][st]
        if len(route) == 1:
            return True, f"The compass points at your feet: the {self.c.stone_name(st)} is right here."
        return True, (f"The compass points toward {self.c.name(route[1])}. The nearest unclaimed spellstone is "
                      f"the {self.c.stone_name(st)} at {self.c.name(p)}, {len(route) - 1} steps away.")

    def _p_ember(self, _t):
        s = self.s
        near = {s["place"], *self.c.links.get(s["place"], {})}
        seen = {}
        for r, p, t in s["footprints"]:
            if p in near and s["now"] - t <= FOOTPRINT_S:
                seen.setdefault(r, p)
        if not seen:
            return True, "No glowing footprints. Nobody has passed this way in the last hour."
        return True, "Glowing footprints: " + "; ".join(f"{self.c.agents[r]['name']} passed {self.c.name(p)}"
                                                        for r, p in seen.items()) + "."

    def _p_fog(self, _t):
        self._effect_on("fog", self.c.stones["fog"]["duration_s"])
        return True, "Thick fog rolls in around you. Rivals can't see you for a minute and a half."

    def _p_frost(self, _t):
        if self.s["encounter"]:
            k = self.s["encounter"]
            self.s["creatures"][k]["frozen_until"] = self.s["now"] + 30
            return True, "You freeze the water around it. " + self._hit_creature(15) + " It's stuck for 30 seconds."
        if not self._objects("water"):
            return False, "There's no open water here to freeze."
        return True, "You freeze the water into a bridge."

    def _p_gale(self, _t):
        s, c = self.s, self.c
        enc, rivals = self._hostile()
        if enc:
            return True, "A gust slams into it. " + self._hit_creature(20)
        if not rivals:
            return False, "There's nobody here to blow away."
        r = rivals[0]
        out = f"A gust of wind throws {c.agents[r]['name']} back."
        held = s["rival_held"].get(r, [])
        if held:
            st = held.pop()
            s["loose"][st] = s["place"]
            out += f" The {c.stone_name(st)} falls out of their hands! Pick it up, quick."
        s["rivals"][r]["stunned_until"] = s["now"] + 120
        nxt = [q for q in c.links.get(s["place"], {}) if self._open(s["place"], q) and not c.underwater(q)]
        if nxt:
            s["rivals"][r]["place"] = sorted(nxt)[0]
        return True, out

    def _p_glass(self, _t):
        s, c = self.s, self.c
        out = []
        for q in c.links.get(s["place"], {}):
            t = self._here_things(q)
            seen = [c.stone_name(x) for x in t["stones"]] + [c.agents[r]["name"] for r in t["rivals"]] + \
                   [c.creature_profiles[s["creatures"][k]["profile"]]["creature_name"] for k in t["creatures"]]
            if seen:
                out.append(f"at {c.name(q)}: {', '.join(seen)}")
        self._effect_on("glass", c.stones["glass"]["duration_s"])
        return True, "The walls turn to glass. " + ("; ".join(out).capitalize() + "." if out
                                                    else "Nothing is hiding next door.")

    def _p_hush(self, _t):
        self._effect_on("hush", self.c.stones["hush"]["duration_s"])
        return True, "Everything goes silent around you. Rivals can't hear you for a minute."

    def _p_lantern(self, _t):
        s, c = self.s, self.c
        found = [st for st, by in s["hidden"].items() if by == "lantern" and s["loose"].get(st) == s["place"]]
        for st in found:
            del s["hidden"][st]
        out = "The lantern's light sweeps the room."
        if found:
            out += " It shows something hidden: the " + ", ".join(c.stone_name(x) for x in found) + "!"
        if self._threshold_link_here():
            out += (" The Threshold's seals light up and open." if self._threshold_ready() and self._open_threshold()
                    else " The Threshold's seals glow. Still missing: " + ", ".join(self._threshold_missing()) + ".")
        elif not found:
            out += " Nothing hidden here."
        return True, out

    def _p_ledger(self, target):
        s, c = self.s, self.c
        st = c.find_stone(target) or (s["carried"] + list(s["absorbed"]) + [""])[0]
        if not st:
            return False, "Tell me which spellstone to look up."
        h = s["history"].get(st, [])
        if not h:
            return True, f"Nobody has held the {c.stone_name(st)} since the game began."
        return True, f"The {c.stone_name(st)} was held by: " + ", then ".join(who for who, _ in h) + "."

    def _p_lure(self, _t):
        s, c = self.s, self.c
        enc, rivals = self._hostile()
        if enc:
            cr = s["creatures"][enc]
            cr["state"], cr["place"], s["encounter"] = "dormant", cr["lair"], ""
            return True, (f"You make a noise far away. The {c.creature_profiles[cr['profile']]['creature_name']} "
                          "goes after it and leaves you alone.")
        nxt = [q for q in c.links.get(s["place"], {}) if self._open(s["place"], q)]
        for r in rivals:
            s["rivals"][r]["place"] = sorted(nxt)[-1] if nxt else c.agents[r]["home"]
        return True, ("A fake noise next door pulls " + ", ".join(c.agents[r]["name"] for r in rivals) + " away."
                      if rivals else "A fake noise echoes next door. Nobody was here to fall for it.")

    def _p_magnet(self, _t):
        s, c = self.s, self.c
        near = {s["place"], *[q for q in c.links.get(s["place"], {}) if self._open(s["place"], q)]}
        got = [st for st, p in s["loose"].items() if p in near and st not in s["hidden"]]
        for st in got:
            s["loose"][st] = s["place"]
            self.take(st)
            self.s["news"] = [n for n in self.s["news"]]
        return True, ("The magnet pulls in: " + ", ".join(c.stone_name(x) for x in got) + "." if got
                      else "Nothing metal close enough to pull in.")

    def _p_map(self, _t):
        s, c = self.s, self.c
        region = c.places[s["place"]].get("region")
        places = [p for p, v in c.places.items() if v.get("region") == region]
        for p in places:
            self._visit(p)
        return True, (f"A map draws itself in the air: {c.region_name(region)} has {len(places)} places: "
                      + ", ".join(c.name(p) for p in places[:10]) + ".")

    def _p_pearl(self, _t):
        self._effect_on("pearl", self.c.stones["pearl"]["duration_s"])
        self.s["effects"].pop("drowning", None)
        return True, "You and everyone with you can breathe underwater for 5 minutes."

    def _p_pelagic(self, _t):
        self._effect_on("pelagic", self.c.stones["pelagic"]["duration_s"])
        return True, "You'll fly through the water three times as fast for 2 minutes."

    def _p_rust(self, _t):
        a, b = "whispering_depths_3", "black_vault_0"
        if self.s["place"] in (a, b) and self._open_link(a, b):
            return True, "The Black Vault's iron gate rusts away and falls open."
        if self.s["place"] in (a, b):
            return True, "The iron gate is already rotted open."
        return False, "There's no metal lock here to rust."

    def _p_spark(self, _t):
        for o in self._objects("machine"):
            if len(o["links"]) == 2 and self._open_link(*o["links"]):
                return True, (f"Sparks jump into the {o['name']}. It groans to life and opens the way to "
                              f"{self.c.name(o['links'][1])}.")
            return True, f"The {o['name']} is already running."
        return False, "There's no old machine here to power."

    def _p_tide(self, _t):
        for o in self._objects("water_gate"):
            if self._open_link(*o["links"]):
                return True, (f"The water drops at the {o['name']}. The flooded way between "
                              f"{self.c.name(o['links'][0])} and {self.c.name(o['links'][1])} is open.")
            return True, f"The {o['name']} is already open."
        return False, "There's no water gate here."

    def _p_tilt(self, _t):
        if not self._objects("room"):
            return False, "You need to be inside a room to tip its gravity."
        self._effect_on("tilt", self.c.stones["tilt"]["duration_s"])
        return True, "The room tips over. For 45 seconds you can walk on the walls and the ceiling."

    def _p_ward(self, _t):
        if not self._objects("door"):
            return False, "There's no door here to seal. The Front Door is at Hearthstone House."
        self._effect_on("ward_hearth", WARD_S)
        return True, "You seal the door. For half an hour no rival can get into Hearthstone House."

    def _p_weathervane(self, _t):
        s = self.s
        s["weather"] = WEATHERS[(WEATHERS.index(s["weather"]) + 1) % len(WEATHERS)]
        if s["weather"] == "storm":
            for v in s["rivals"].values():
                v["stunned_until"] = max(v["stunned_until"], s["now"] + RIVAL_TURN_S)
        return True, f"The weather turns to {s['weather']}." + (" The rivals take shelter." if s["weather"] == "storm"
                                                                 else "")

    def _find_combo(self, name: str):
        q = norm(name)
        for cb in self.c.combos:
            if q and (q == norm(cb[3]) or q == cb[2].replace("_", " ") or norm(cb[3]) in q):
                return cb
        return None

    def combo(self, name: str) -> dict:
        self._awake()
        s, c = self.s, self.c
        cb = self._find_combo(name)
        if not cb:
            return self._done("The combos are: " + ", ".join(f"{x[3]} ({c.stone_name(x[0])} and "
                                                             f"{c.stone_name(x[1])})" for x in c.combos) + ".", ok=False)
        if not self._combo_ready(cb):
            return self._done(f"{cb[3]} needs your wand with the {c.stone_name(cb[0])} and the "
                              f"{c.stone_name(cb[1])} both at level {COMBO_LEVEL}.", ok=False)
        ready = max(s["cooldowns"].get(cb[0], 0), s["cooldowns"].get(cb[1], 0))
        if ready > s["now"]:
            return self._done(f"{cb[3]} is recharging, about {_minutes(ready - s['now'])}.", ok=False)
        say = f"{cb[3]}! {cb[4]}."
        if cb[2] == "vanish":
            self._effect_on("vanish", 120)
        elif cb[2] == "true_sight":
            say += " " + self._p_lantern("")[1] + " " + self._p_glass("")[1]
        elif cb[2] == "blizzard":
            enc, rivals = self._hostile()
            if enc:
                s["creatures"][enc]["frozen_until"] = s["now"] + 30
                say += " " + self._hit_creature(45)
            elif rivals:
                say += " " + self._p_gale("")[1]
        elif cb[2] == "hunters_eye":
            say += " " + self._p_ember("")[1] + " " + self._p_compass("")[1]
        elif cb[2] == "false_alarm":
            say += " " + self._p_bell("")[1]
        for st in cb[:2]:
            s["cooldowns"][st] = s["now"] + self._cooldown(st)
        self._pass(5)
        return self._done(say)

    # ---------- the threshold ----------

    def _threshold_link(self, a: str, b: str) -> bool:
        return _key(a, b) == _key(self.c.threshold.get("gate", "threshold_halls_2"), "beyond_threshold")

    def _threshold_link_here(self) -> bool:
        return self.s["place"] == "threshold_halls_2" and not self._open("threshold_halls_2", "beyond_threshold")

    def _threshold_missing(self) -> list:
        s, c, t = self.s, self.c, self.c.threshold
        out = [f"the {c.stone_name(k)}" for k in t["keys"] if not self._has(k)]
        out += [f"find {c.name(p)}" for p in t["discover"] if p not in s["visited"]]
        q = s["dynamic"].get(t["quest"])
        if not q or (not q["done"] and s["place"] != "threshold_halls_2"):
            out.append("finish The Descent")
        return out

    def _threshold_ready(self) -> bool:
        return not self._threshold_missing()

    def _open_threshold(self) -> bool:
        if self._open_link("threshold_halls_2", "beyond_threshold"):
            self._news("The Abyssal Threshold opens. A stair of black glass leads down into the Beyond.")
            return True
        return False

    # ---------- fights ----------

    def fight(self) -> dict:
        self._awake()
        s, c = self.s, self.c
        enc, rivals = self._hostile()
        if not enc and rivals:
            r = rivals[0]
            if s["rival_held"].get(r):
                st = s["rival_held"][r].pop(0)
                s["loose"][st] = s["place"]
                self._pass(STEP_S)
                return self._done(f"You wrestle with {c.agents[r]['name']}. The {c.stone_name(st)} drops to the "
                                  "ground!")
            return self._done(f"{c.agents[r]['name']} dodges and laughs. Rivals are better beaten with spellstones, "
                              "like the Gale or the Bell.")
        if not enc:
            return self._done("There's nothing here to fight.", ok=False)
        friends = len(self._here_things(s["place"])["friends"])
        damage = 25 + 10 * friends + (15 if s["wand"] else 0) + (10 if self._effect("helper") else 0)
        prof = c.creature_profiles[s["creatures"][enc]["profile"]]
        hit = prof["attack_power"] * (0.5 if self._effect("anchor") else 1.0)
        result = self._hit_creature(damage)
        if s["encounter"]:
            s["health"] -= hit
            result += f" It hits you back. You have {max(0, round(s['health']))} health."
            if s["health"] <= 0:
                self._knockout()
        self._pass(STEP_S)
        self._check_quests()
        return self._done(f"You strike for {damage}" + (f", with {friends} companion{'s' if friends > 1 else ''}"
                                                        " fighting beside you" if friends else "") + ". " + result)

    # ---------- companions ----------

    def order(self, companion: str, order: str, place: str = "") -> dict:
        self._awake()
        s, c = self.s, self.c
        q = norm(companion)
        who = [k for k in s["companions"] if q in ("both", "everyone", "them", "all", "friends", "") or
               norm(c.agents[k]["name"]) in q]
        if not who:
            return self._done("Your companions are Noah and Angelic.", ok=False)
        if not all(s["companions"][k]["met"] for k in who):
            return self._done("You haven't met your companions yet. They're waiting near Hearthstone House.",
                              ok=False)
        o = norm(order)
        names = " and ".join(c.agents[k]["name"] for k in who)
        if any(w in o for w in ("follow", "come", "with me")):
            for k in who:
                if s["companions"][k]["place"] != s["place"] and not self._route(s["companions"][k]["place"],
                                                                                  s["place"]):
                    return self._done(f"{c.agents[k]['name']} can't reach you from "
                                      f"{c.name(s['companions'][k]['place'])}.", ok=False)
                s["companions"][k].update(order="follow", place=s["place"])
            return self._done(f"{names} {'come' if len(who) > 1 else 'comes'} with you.")
        if any(w in o for w in ("stay", "wait", "stop")):
            for k in who:
                s["companions"][k]["order"] = "stay"
            return self._done(f"{names} will wait at {c.name(s['companions'][who[0]]['place'])}.")
        if any(w in o for w in ("guard", "protect", "watch", "go")):
            target = c.find_place(place) if place else s["place"]
            if not target:
                return self._done(f"I don't know the place '{place}'.", ok=False)
            for k in who:
                if not self._route(s["companions"][k]["place"], target, lambda p: not c.underwater(p)) and \
                        s["companions"][k]["place"] != target:
                    return self._done(f"{c.agents[k]['name']} can't get to {c.name(target)} on foot.", ok=False)
                s["companions"][k].update(order="guard", place=target)
            self._check_quests()
            return self._done(f"{names} will guard {c.name(target)}. Rivals there will have to get past them.")
        if any(w in o for w in ("search", "find", "look")):
            st, route = self._nearest_stone()
            if not st:
                return self._done(f"{names} look around but can't find any spellstone they can reach.")
            k = who[0]
            s["companions"][k].update(order="stay", place=s["loose"][st])
            del s["loose"][st]
            s["carried"].append(st) if not s["wand"] else s["absorbed"].setdefault(st, {"uses": 0})
            s["history"].setdefault(st, []).append([c.agents[k]["name"], round(s["now"])])
            s["companions"][k]["place"] = s["place"]
            self._pass(sum(self._leg_s(a, b) for a, b in zip(route, route[1:])))
            self._check_quests()
            return self._done(f"{c.agents[k]['name']} runs off and comes back with the {c.stone_name(st)} from "
                              f"{c.name(route[-1])}. It's yours now.")
        return self._done("You can tell them to follow you, stay, guard a place, or search for a spellstone.",
                          ok=False)

    def companions(self) -> dict:
        self._awake()
        s, c = self.s, self.c
        out = []
        for k, v in s["companions"].items():
            if not v["met"]:
                out.append(f"You haven't met {c.agents[k]['name']} yet")
            else:
                out.append(f"{c.agents[k]['name']} is at {c.name(v['place'])}, "
                           + {"follow": "following you", "stay": "waiting", "guard": "on guard"}[v["order"]])
        return self._done(". ".join(out) + ".")

    # ---------- rivals ----------

    def _rivals_turn(self) -> None:
        s, c = self.s, self.c
        for r, v in s["rivals"].items():
            if v["stunned_until"] > s["now"]:
                continue
            if v["place"] == s["place"] and s["carried"] and not self._hidden_from_rivals():
                if _roll(self.seed, r, round(s["now"])) < 0.35:
                    st = sorted(s["carried"])[int(_roll(self.seed, r, "pick", round(s["now"])) * len(s["carried"]))]
                    s["carried"].remove(st)
                    s["rival_held"].setdefault(r, []).append(st)
                    s["history"].setdefault(st, []).append([c.agents[r]["name"], round(s["now"])])
                    self._news(f"{c.agents[r]['name']} snatched your {c.stone_name(st)}! Use the Gale or fight "
                               "to get it back.")
            allowed = (lambda p, r=r: (not c.in_cave(p)) and (r in SWIMMERS or not c.underwater(p))
                       and not (p == "hearth" and self._effect("ward_hearth"))
                       and not any(cv["order"] == "guard" and cv["place"] == p for cv in s["companions"].values()))
            goals = [(st, p) for st, p in s["loose"].items() if st not in s["hidden"] and allowed(p)]
            best = None
            for st, p in goals:
                route = self._route(v["place"], p, allowed) if p != v["place"] else [p]
                if route and (best is None or len(route) < len(best[1])):
                    best = (st, route)
            if best:
                st, route = best
                v["place"] = route[min(3, len(route) - 1)]
                for p in route[1:min(4, len(route))]:
                    s["footprints"].append([r, p, s["now"]])
                if s["loose"].get(st) == v["place"]:
                    del s["loose"][st]
                    s["rival_held"].setdefault(r, []).append(st)
                    s["history"].setdefault(st, []).append([c.agents[r]["name"], round(s["now"])])
                    self._news(f"Jarvis: {c.agents[r]['name']} just grabbed the {c.stone_name(st)} at "
                               f"{c.name(v['place'])}.")
        s["footprints"] = [f for f in s["footprints"] if s["now"] - f[2] <= FOOTPRINT_S]

    def rivals(self) -> dict:
        self._awake()
        s, c = self.s, self.c
        out = []
        for r, v in s["rivals"].items():
            held = s["rival_held"].get(r, [])
            out.append(f"{c.agents[r]['name']} the {c.agents[r]['character']} was last near {c.name(v['place'])}"
                       + (f", holding the {', '.join(c.stone_name(x) for x in held)}" if held else ""))
        return self._done(". ".join(out) + ".")

    # ---------- salvage and the forge ----------

    def search(self) -> dict:
        self._awake()
        s, c = self.s, self.c
        for o in self.c.objects.values():
            if o["place"] == s["place"] and o["type"] == "salvage" and o["id"] not in s["salvaged"]:
                s["salvaged"].append(o["id"])
                gives = o.get("state", {}).get("gives", {})
                for m, n in gives.items():
                    s["materials"][m] = s["materials"].get(m, 0) + n
                self._pass(30)
                self._check_quests()
                return self._done(f"You search the {o['name']} and find " + ", ".join(
                    f"{n} {m.replace('_', ' ')}" for m, n in gives.items()) + ".")
        texts = [o for o in self.c.objects.values() if o["place"] == s["place"] and o.get("text")]
        if texts:
            return self._done(f"You find old writing on the {texts[0]['name']}, but you can't read it. The "
                              "Cipher Spellstone can.")
        self._pass(30)
        return self._done("You search around but find nothing useful here.")

    def forge(self, stone: str) -> dict:
        self._awake()
        s, c = self.s, self.c
        if not self._objects("key_forge"):
            return self._done("Spellstones are forged in Pelagoria's Forge Hall, under the sea.", ok=False)
        st = c.find_stone(stone, list(c.recipes))
        if not st:
            return self._done("The forge can make: " + ", ".join(c.stone_name(x) for x in c.recipes) + ".",
                              ok=False)
        if st not in s["recipes"]:
            return self._done(f"You don't know how to forge the {c.stone_name(st)} yet. Find and read its recipe.",
                              ok=False)
        if st in s["forged"] or self._has(st):
            return self._done(f"The {c.stone_name(st)} already exists. Each spellstone exists only once.", ok=False)
        need = c.recipes[st]
        short = [f"{n - s['materials'].get(m, 0)} more {m.replace('_', ' ')}" for m, n in need.items()
                 if s["materials"].get(m, 0) < n]
        if short:
            return self._done(f"To forge the {c.stone_name(st)} you need " + ", ".join(short) + ".", ok=False)
        for m, n in need.items():
            s["materials"][m] -= n
        s["forged"].append(st)
        s["history"].setdefault(st, []).append(["you", round(s["now"])])
        if s["wand"]:
            s["absorbed"].setdefault(st, {"uses": 0})
        else:
            s["carried"].append(st)
        self._pass(60)
        self._check_quests()
        return self._done(f"The forge roars. You made the {c.stone_name(st)}: {c.stones[st]['power']}.")

    # ---------- missions ----------

    def _quest(self, qid: str) -> dict:
        if qid in self.c.quests:
            return self.c.quests[qid]
        if qid == "the_descent":
            return {"title": "The Descent", "after": [], "reward": {}, "steps": [
                {"text": "Find the Cliff Mouth, the cave's way back to the surface", "kind": "reach",
                 "arg": "cave_mouth_0"},
                {"text": "Find the Crystal Catacombs", "kind": "reach", "arg": "crystal_catacombs_0"},
                {"text": "Reach the lost city of Old Hollowmere", "kind": "reach", "arg": "hollowmere_0"},
                {"text": "Stand before the Abyssal Threshold", "kind": "reach", "arg": "threshold_halls_2"}]}
        raise KeyError(qid)

    def _all_quests(self) -> dict:
        return {**self.s["quests"], **self.s["dynamic"]}

    def _active(self, qid: str) -> bool:
        q = self._quest(qid)
        prog = self._all_quests()
        return all(prog.get(a, {}).get("done") for a in q.get("after", []))

    def _step_met(self, step: dict) -> bool:
        s, kind, arg = self.s, step["kind"], step["arg"]
        if kind == "reach":
            return s["place"] == arg or arg in s["visited"]
        if kind == "hold":
            return self._has(arg)
        if kind == "salvage":
            return arg in s["salvaged"]
        if kind == "learn":
            return arg in s["recipes"]
        if kind == "forge":
            return arg in s["forged"]
        if kind == "portal":
            return arg in s["portals_used"]
        if kind == "control":
            return (s["place"] == arg or any(v["order"] == "guard" and v["place"] == arg
                                             for v in s["companions"].values())) and \
                not any(v["place"] == arg for v in s["rivals"].values())
        return False

    def _check_quests(self) -> None:
        s = self.s
        for qid, prog in self._all_quests().items():
            if prog["done"] or not self._active(qid):
                continue
            q = self._quest(qid)
            while prog["step"] < len(q["steps"]) and self._step_met(q["steps"][prog["step"]]):
                prog["step"] += 1
            if prog["step"] >= len(q["steps"]):
                prog["done"] = True
                for m, n in q.get("reward", {}).items():
                    s["materials"][m] = s["materials"].get(m, 0) + n
                self._news(f"Mission complete: {q['title']}!")
                if qid == "the_descent" and self._threshold_link_here() and self._threshold_ready():
                    self._open_threshold()

    def missions(self) -> dict:
        self._awake()
        out, rows = [], []
        for qid, prog in self._all_quests().items():
            q = self._quest(qid)
            if prog["done"]:
                rows.append({"title": q["title"], "done": True})
                continue
            if not self._active(qid):
                continue
            step = q["steps"][prog["step"]]["text"]
            rows.append({"title": q["title"], "next": step, "step": prog["step"] + 1, "of": len(q["steps"])})
            out.append(f"{q['title']}: {step}")
        done = sum(1 for r in rows if r.get("done"))
        return self._done(("Your missions. " + ". ".join(out) + "." if out else "No open missions right now.")
                          + (f" You've finished {done}." if done else ""), missions=rows)

    # ---------- status ----------

    def status(self) -> dict:
        s, c = self.s, self.c
        if s["opening"] != "done":
            left = [t for t in OPENING_TASKS if t not in s["tasks_done"]]
            return self._done("Imagination hasn't started yet. You're in your ordinary bedroom at the end of the day."
                              + (f" Still to do before bed: {', '.join(left)}." if left else " Time to sleep."),
                              opening=s["opening"])
        bits = [f"You're at {c.name(s['place'])} with {round(s['health'])} health."]
        if s["wand"]:
            bits.append(f"Your {c.wands[s['wand']]['name']} holds {len(s['absorbed'])} spellstones.")
        if s["carried"]:
            bits.append("You carry " + ", ".join(c.stone_name(x) for x in s["carried"]) + ".")
        if not s["wand"] and not s["carried"]:
            bits.append("You have no spellstones yet.")
        if s["encounter"]:
            cr = s["creatures"][s["encounter"]]
            bits.append(f"The {c.creature_profiles[cr['profile']]['creature_name']} is attacking you!")
        mats = {m: n for m, n in s["materials"].items() if n}
        if mats:
            bits.append("Materials: " + ", ".join(f"{n} {m.replace('_', ' ')}" for m, n in mats.items()) + ".")
        active = [k for k in ("pearl", "pelagic", "fog", "hush", "vanish", "anchor", "helper") if self._effect(k)]
        if active:
            bits.append("Active: " + ", ".join(active) + ".")
        nxt = min(s["clocks"].items(), key=lambda kv: kv[1]["next_at"])
        bits.append(f"Next chime: the {c.clocks[nxt[0]]['name']} in {_minutes(nxt[1]['next_at'] - s['now'])}.")
        return self._done(" ".join(bits), wand=c.wands[s["wand"]]["name"] if s["wand"] else "",
                          spellstones=[c.stone_name(x) for x in list(s["absorbed"]) + s["carried"]],
                          visited=len(s["visited"]), materials=mats)

    def clocks(self) -> dict:
        self._awake()
        s, c = self.s, self.c
        out = []
        for cid, v in sorted(s["clocks"].items(), key=lambda kv: kv[1]["next_at"]):
            portal = [p["name"] for p in c.portals.values() if p["clock"] == cid]
            out.append(f"the {c.clocks[cid]['name']} at {c.name(c.clocks[cid]['place'])} chimes in "
                       f"{_minutes(v['next_at'] - s['now'])}" + (f" and opens the {portal[0]}" if portal else ""))
        return self._done("Clocks: " + "; ".join(out) + ".")
