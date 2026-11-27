"""The canon of Imagination: every place, path, spellstone, wand, person, creature, mission, clock and portal,
exported from the real Godot world (GameStudioFoundation scripts/devtools/imagination_canon.gd) into canon.json."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

CANON_PATH = Path(__file__).with_name("canon.json")
LORE_PATH = Path(__file__).with_name("lore.md")


def norm(text: str) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower().replace("'", ""))
    t = re.sub(r"\b(the|a|an|to|of|my)\b", " ", t)
    return " ".join(t.split())


class Canon:
    def __init__(self, data: dict):
        if data.get("schema") != 1 or data.get("title") != "Imagination":
            raise ValueError("not an Imagination canon file")
        self.data = data
        self.places = {p["id"]: p for p in data["places"] if p["id"] != "_offstage"}
        self.links: dict[str, dict[str, dict]] = {}
        for link in data["links"]:
            for a, b in ((link["a"], link["b"]), (link["b"], link["a"])):
                if a in self.places and b in self.places:
                    self.links.setdefault(a, {})[b] = link
        self.link_kinds: dict = data["systems"].get("deep_cave", {}).get("link_kinds", {})
        self.stones = {s["id"]: s for s in data["spellstones"]}
        self.agents = {a["id"]: a for a in data["agents"]}
        self.creature_profiles = {c["creature_id"]: c for c in data["creatures"]}
        self.objects: dict = data["systems"].get("key_world", {}).get("objects", {})
        self.clocks: dict = data["systems"]["clocks"]["clocks"]
        self.portals: dict = data["systems"]["time_portals"]["portals"]
        self.quests: dict = data["quest_defs"]["quests"]
        self.quest_order: list = list(data["quest_defs"]["order"])
        self.wands: dict = data["wands"]
        self.combos: list = data["wand_combos"]
        self.recipes: dict = data["recipes"]
        self.threshold: dict = data["threshold"]

    def name(self, place: str) -> str:
        return self.places.get(place, {}).get("name", place)

    def stone_name(self, stone: str) -> str:
        return self.stones.get(stone, {}).get("name", stone)

    def tags(self, place: str) -> list:
        return self.places.get(place, {}).get("tags", [])

    def in_cave(self, place: str) -> bool:
        return "cave" in self.tags(place)

    def underwater(self, place: str) -> bool:
        return bool(self.places.get(place, {}).get("underwater", False))

    def region_name(self, region: str) -> str:
        if region in self.data.get("zones", {}):
            return self.data["zones"][region][0]
        for r in self.data.get("cave_regions", []):
            if r[0] == region:
                return r[1]
        return region.replace("_", " ").title()

    def kind_of(self, a: str, b: str) -> str:
        key = "|".join(sorted([a, b]))
        if key in self.link_kinds:
            return self.link_kinds[key]
        return "swim" if self.underwater(a) and self.underwater(b) else "path"

    def find_place(self, text: str, among: Optional[list] = None) -> str:
        """The place a spoken name means: an exact id or name first, then the best word match."""
        q = norm(text)
        if not q:
            return ""
        ids = among if among is not None else list(self.places)
        for pid in ids:
            if q in (norm(pid.replace("_", " ")), norm(self.name(pid))):
                return pid
        regions: dict = {}
        for pid in ids:
            region = self.places.get(pid, {}).get("region", "")
            regions.setdefault(region, []).append(pid)
        for region, members in regions.items():
            if q == norm(self.region_name(region)) and region not in ("gullhaven", "estate"):
                return sorted(members)[0]
        words = set(q.split())
        best, score = "", 0.0
        for pid in ids:
            have = set(norm(self.name(pid)).split()) | set(norm(pid.replace("_", " ")).split())
            hit = len(words & have)
            s = hit / max(len(words), 1) + hit / max(len(have), 1) * 0.5 + (1 if hit == len(words) else 0)
            if hit and s > score:
                best, score = pid, s
        return best if score >= 1.6 or (score >= 0.6 and len(words) == 1) else ""

    def find_stone(self, text: str, among: Optional[list] = None) -> str:
        q = norm(text).replace("spellstone", "").replace("stone", "").replace("key", "").strip()
        ids = among if among is not None else list(self.stones)
        for sid in ids:
            n = norm(self.stone_name(sid)).replace("spellstone", "").strip()
            if q and (q == sid or q == n or q in n.split() or n in q):
                return sid
        return ""


@lru_cache(maxsize=4)
def _read(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_canon(path: Optional[Path] = None) -> Canon:
    return Canon(_read(str(path or CANON_PATH)))


def lore_text() -> str:
    return LORE_PATH.read_text(encoding="utf-8")
