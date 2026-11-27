"""Teach Jarvis the Book Writer the whole canon of Imagination: one book called "Imagination" whose story bible
holds every character, place, spellstone, wand, creature, clock, portal, mission and rule from the real game,
plus the story canon as a note. Safe to run again: entries are updated in place, never duplicated."""
from __future__ import annotations

import hashlib
import json

from jarvis_books.imagination.canon import Canon, load_canon, lore_text
from jarvis_books.storage.library import Library

BOOK_TITLE = "Imagination"
META_KEY = "imagination.canon_hash"
LORE_SOURCE = "imagination-canon"


def canon_hash(canon: Canon) -> str:
    raw = json.dumps(canon.data, sort_keys=True) + lore_text()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _places_with(canon: Canon) -> tuple[dict, dict, dict]:
    stones, objects, lairs = {}, {}, {}
    for s in canon.stones.values():
        if s["where"]:
            stones.setdefault(s["where"], []).append(s["name"])
    for o in canon.objects.values():
        objects.setdefault(o["place"], []).append(o["name"])
    for a in canon.agents.values():
        if a["role"] == "creature":
            lairs.setdefault(a["home"], []).append(canon.creature_profiles.get(
                a["id"], canon.creature_profiles.get(a["id"].rsplit("_", 1)[0], {})).get("creature_name", a["name"]))
    return stones, objects, lairs


def _entries(canon: Canon) -> list[tuple[str, str, str, dict]]:
    c, out = canon, []
    for a in c.agents.values():
        if a["role"] in ("companion", "rival", "rescuer"):
            role = {"companion": "companion", "rival": "rival", "rescuer": "the family's rescuer"}[a["role"]]
            home = c.name(a["home"]) if a["home"] in c.places else "wherever he's needed"
            out.append(("character", a["name"], f"{a['name']}, the {a['character']}: a {role}. Based at {home}.",
                        {"role": a["role"], "character": a["character"], "home": home,
                         "personality": a.get("personality", {}), "skills": a.get("skills", {})}))
    out.append(("character", "The player", "An ordinary kid who falls asleep at the end of an ordinary day and wakes "
                "in Imagination. Owner of the Deep Cave Wand. Two more players own the Deep Sea Wand and the Sky Wand.",
                {"wand": "Deep Cave Wand"}))
    for p in c.creature_profiles.values():
        homes = [c.name(a["home"]) for a in c.agents.values()
                 if a["role"] == "creature" and a["id"].startswith(p["creature_id"])]
        out.append(("character", p["creature_name"], f"Creature. {p['lore_description']} Lair: {', '.join(homes)}. "
                    f"Health {round(p['maximum_health'])}, attack {round(p['attack_power'])}, defeated by "
                    f"{p['defeat_mode']}.", {k: p[k] for k in sorted(p) if not isinstance(p[k], (dict, list))}
                    | {"lairs": homes, "senses": p.get("senses"), "abilities": p.get("available_abilities")}))
    stones, objects, lairs = _places_with(c)
    for pid, p in c.places.items():
        links = [f"{c.name(q)} ({round(l['seconds'])} s{'' if l['open'] else ', sealed'}, {c.kind_of(pid, q)})"
                 for q, l in sorted(c.links.get(pid, {}).items())]
        where = p.get("zone") or c.region_name(p.get("region", ""))
        depth = f", {abs(round(p['pos'][1]))} m down" if p.get("pos") and p["pos"][1] < 0 else ""
        bits = [f"{p['name']}: a {p.get('kind', 'place').replace('_', ' ')} in {where}{depth}"
                + (", underwater" if p.get("underwater") else "") + "."]
        if stones.get(pid):
            bits.append("Spellstones here: " + ", ".join(stones[pid]) + ".")
        if objects.get(pid):
            bits.append("Things here: " + ", ".join(objects[pid]) + ".")
        if lairs.get(pid):
            bits.append("Lair of the " + ", ".join(lairs[pid]) + ".")
        bits.append("Leads to " + "; ".join(links) + ".")
        out.append(("location", p["name"], " ".join(bits),
                    {"id": pid, "region": p.get("region"), "zone": p.get("zone"), "area_m2": p.get("area"),
                     "position": p.get("pos"), "tags": p.get("tags", []), "underwater": p.get("underwater", False),
                     "links": links}))
    for s in c.stones.values():
        where = c.name(s["where"]) if s["where"] else "nowhere yet: it must be forged in the Forge Hall"
        out.append(("item", s["name"], f"{s['power']}. Found at {where}. Recharges in {round(s['cooldown_s'])} s"
                    + (f", lasts {round(s['duration_s'])} s" if s["duration_s"] else "") + ".",
                    {"id": s["id"], "targets": s["targets"], "recipe": c.recipes.get(s["id"])}))
    for wid, w in c.wands.items():
        owner = {"player": "the first player", "player2": "the second player", "player3": "the third player"}
        where = c.name(w["place"]) if w["place"] else "the Sky Realm (still being built)"
        out.append(("item", w["name"], f"Legendary wand of {owner.get(w['owner'], w['owner'])}, waiting at {where}. "
                    "Absorbs spellstones for good; can't be stolen or copied.", {"id": wid, **w}))
    for cb in c.combos:
        out.append(("item", f"{cb[3]} (wand combo)", f"{cb[4]}. Needs {c.stone_name(cb[0])} and "
                    f"{c.stone_name(cb[1])} at level 2 in one wand.", {"stones": cb[:2]}))
    for o in c.objects.values():
        if o.get("text"):
            out.append(("item", o["name"], f"Old writing at {c.name(o['place'])}: \"{o['text']}\"",
                        {"recipe": o.get("state", {}).get("recipe")}))
    for cid, clock in c.clocks.items():
        portals = [p for p in c.portals.values() if p["clock"] == cid]
        out.append(("rule", clock["name"], f"A clock at {c.name(clock['place'])} that chimes about every five "
                    "minutes." + "".join(f" Its chime opens the {p['name']} between {c.name(p['place'])} and "
                                         f"{c.name(p['era_place'])} for 150 seconds." for p in portals), {}))
    rules = {
        "No walking into the Deep Cave": "Only the Cliffside Rift and the Drowned Rift lead into the Deep Cave, when "
                                         "their clocks chime. The same rift always leads back out.",
        "Breathing underwater": "Without the Abyssal Breath, air lasts about a minute, then you drown. The Breath "
                                "covers you and everyone with you for five minutes.",
        "Spellstones are one of a kind": "Each spellstone exists once. Forged ones exist only after forging.",
        "Bound spellstones": "A spellstone absorbed into a wand is bound for good and can never be stolen.",
        "Wand ownership": "Each wand has one owner, claimed only by that player where it lies; it changes hands only "
                          "when its holder gives it away.",
        "Ability levels": "Abilities level up every 5 uses, to level 3; each level cuts the recharge by 20%. Combos "
                          "need both abilities at level 2.",
        "The Abyssal Threshold": "Opens only with Lantern and Cipher magic, the Crystal Catacombs and Old Hollowmere "
                                 "found, and The Descent finished.",
        "Rival truce": "When a great creature attacks the player and rivals together, rivals fight it alongside "
                       "the player, then turn back 20 seconds after it's gone.",
        "Knocked out": "Anyone knocked out is carried home to Hearthstone House by Uncle Dominic.",
        "No AI decides the game": "Everything in Imagination happens by rules and seeded dice, so it plays the same "
                                  "with every AI offline.",
    }
    out += [("rule", k, v, {}) for k, v in rules.items()]
    for qid in c.quest_order:
        q = c.quests[qid]
        after = [c.quests[a]["title"] for a in q.get("after", [])]
        out.append(("thread", q["title"], "Mission: " + "; ".join(s["text"] for s in q["steps"]) + "."
                    + (f" Opens after {', '.join(after)}." if after else ""),
                    {"steps": q["steps"], "reward": q.get("reward", {})}))
    out.append(("thread", "The Descent", "Mission (starts on the first visit to the Deep Cave): find the Cliff Mouth, "
                "the Crystal Catacombs, Old Hollowmere, and stand before the Abyssal Threshold.", {}))
    out.append(("event", "The opening: an ordinary day", "Homework, dinner, lights out; sleep; a dream of the ocean, "
                "a cave mouth, a clock with too many hands, an unknown glowing symbol and floating islands; waking in "
                "Gullhaven Square under the sign 'Gullhaven, in the land of IMAGINATION'; then the title.", {}))
    for e in c.data.get("eras", []):
        out.append(("event", e[1], f"A time portal's far side: {e[1]}, reached through the "
                    + ("Old Town Arch." if e[0] == "gullhaven_1893" else "Hour Gate."), {"id": e[0]}))
    for theme, text in (("Wonder", "Curiosity is always rewarded."),
                        ("Family", "The Alloways and Hearthstone House are home and safety."),
                        ("Rivalry", "Rivals play by the same rules and remember."),
                        ("Time", "Clocks and portals: every chime is a door that closes again.")):
        out.append(("theme", theme, text, {}))
    out.append(("style", "Original content only", "Imagination is Justin's own world. Never use characters, names, "
                "places, maps, magic keys or plots from any other book, comic, show or game.", {}))
    return out


def teach(library: Library, canon: Canon | None = None, force: bool = False) -> dict:
    """Write the canon into the Imagination book. Returns what was written; skips the work when nothing changed."""
    canon = canon or load_canon()
    digest = canon_hash(canon)
    book = library.find_book(BOOK_TITLE)
    if book is not None and book.title != BOOK_TITLE:
        book = None
    if book is None:
        book = library.create_book(
            BOOK_TITLE, genre="fantasy adventure",
            premise="An ordinary kid falls asleep and wakes in Imagination, a connected world of a harbour town, a "
                    "drowned kingdom, a Deep Cave and a realm in the sky, where three wands, 26 spellstones, rivals, "
                    "creatures and chiming clocks wait.",
            logline="One ordinary night, one dream, and a whole world called Imagination.",
            audience="middle grade and up", style_notes="Original world only. Wonder first, then danger.")
    if not force and library.get_meta(META_KEY) == f"{book.id}:{digest}":
        return {"book": book.title, "book_id": book.id, "updated": False,
                "entries": len(library.list_bible(book.id))}
    entries = _entries(canon)
    for kind, name, summary, data in entries:
        library.upsert_bible(book.id, kind, name, summary, data)
    if not any(n.source == LORE_SOURCE for n in library.list_notes(book.id)):
        library.add_note(book.id, lore_text(), source=LORE_SOURCE)
    library.set_meta(META_KEY, f"{book.id}:{digest}")
    return {"book": book.title, "book_id": book.id, "updated": True, "entries": len(entries)}
