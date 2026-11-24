"""The Atlanta real estate game: money, time, tax sales, the property market, flips, rentals, companies and
the office secretary. All state is one JSON file, so a game survives restarts and Jarvis can change anything.

Time only moves when the player says so (advance / go to the tax sale), so nothing happens behind their back.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import shutil
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from jarvis_books.errors import NotFound, ValidationError
from jarvis_books.game import rules
from jarvis_books.game.catalog import (AREAS, INDUSTRIES, KINDS, KINDS_BY_KEY, RENOVATIONS_BY_KEY, area_for,
                                       kind_for, renovations_for, size_factor)

log = logging.getLogger(__name__)

TUNABLES = {
    "starting_cash": 8000.0,
    "tax_house_min": 100_000.0, "tax_house_max": 200_000.0,  # tax sale houses are always worth this much
    "rival_bidders": 3.0,
    "rival_cap": 4000.0,  # most rival bidders won't go past this at a tax sale
    "rival_big_spender_chance": 0.12,  # the few who will
    "redemption_rate": 0.55,  # share of owners who redeem within the first year
    "market_heat": 1.0,  # how fast buyers make offers
    "price_multiplier": 1.0,
    "listings_per_month": 10.0,
    "parcels_per_sale": 8.0,
    "bid_increment": 100.0,
}
MAX_DAYS_PER_ADVANCE = 3660
CALL_KINDS_TO_ANSWER = ("offer", "buyer", "counter")


def money(x: float) -> str:
    return f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}"


class Game:
    def __init__(self, path: Path, seed: Optional[int] = None, today: Optional[date] = None):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._listeners: list = []
        self.state: dict[str, Any] = {}
        self.backup = self.path.with_name(self.path.stem + ".backup.json")
        for source in (self.path, self.backup):
            if not source.exists():
                continue
            try:
                self._load(source)
                if source is self.backup:
                    log.warning("the game save was damaged; picked up from the backup save")
                    self.save()
                return
            except (ValueError, KeyError, TypeError, IndexError) as exc:  # a damaged save is kept, not lost
                broken = source.with_name(f"{source.stem}.broken-{int(time.time())}.json")
                source.replace(broken)
                log.warning("game save %s was damaged (%s); kept it as %s", source.name, exc, broken.name)
        self.new_game(seed=seed, today=today)

    # ---- saving --------------------------------------------------------------------------------------
    def _load(self, source: Path) -> None:
        self.state = json.loads(source.read_text(encoding="utf-8"))
        self.state.setdefault("tunables", {})
        for k, v in TUNABLES.items():
            self.state["tunables"].setdefault(k, v)
        r = self.state.pop("rng")
        self.rng = random.Random()
        self.rng.setstate((r[0], tuple(r[1]), r[2]))
        self.state["rng"] = r

    def save(self) -> None:
        with self._lock:
            st = self.rng.getstate()
            self.state["rng"] = [st[0], list(st[1]), st[2]]
            self.state["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            for attempt in range(8):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:  # Windows: antivirus or indexing holding the file for a moment
                    if attempt == 7:
                        raise
                    time.sleep(0.25)
            try:  # a second copy of the same save, in case the first is ever damaged
                spare = self.backup.with_suffix(".tmp")
                shutil.copyfile(self.path, spare)
                os.replace(spare, self.backup)
            except OSError as exc:
                log.warning("couldn't write the backup game save: %s", exc)
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                log.exception("game listener failed")

    def subscribe(self, fn) -> None:
        self._listeners.append(fn)

    def new_game(self, seed: Optional[int] = None, today: Optional[date] = None,
                 tunables: Optional[dict] = None) -> dict:
        with self._lock:
            seed = seed if seed is not None else random.randrange(1 << 30)
            self.rng = random.Random(seed)
            t = dict(TUNABLES, **(tunables or self.state.get("tunables") or {}))
            start = today or date.today()
            self.state = {"version": 1, "seed": seed, "today": start.isoformat(), "started": start.isoformat(),
                          "cash": t["starting_cash"], "tunables": t, "next_id": 1, "properties": {},
                          "deeds": {}, "listings": {}, "sale": None, "auction": None, "companies": {},
                          "calls": [], "log": [], "dev_changes": []}
            self._refresh_market()
            self._post_sale()
            self._note(f"New game in Atlanta with {money(t['starting_cash'])}.")
            self.save()
            return self.status()

    # ---- helpers -------------------------------------------------------------------------------------
    @property
    def today(self) -> date:
        return date.fromisoformat(self.state["today"])

    def t(self, key: str) -> float:
        return float(self.state["tunables"].get(key, TUNABLES.get(key, 0)))

    def _id(self, prefix: str) -> str:
        n = self.state["next_id"]
        self.state["next_id"] = n + 1
        return f"{prefix}{n}"

    def _note(self, text: str) -> None:
        self.state["log"].append({"day": self.state["today"], "text": text})
        del self.state["log"][:-300]

    def _call(self, kind: str, who: str, text: str, **extra) -> dict:
        call = {"id": self._id("C"), "day": self.state["today"], "kind": kind, "from": who, "text": text,
                "status": "new", **extra}
        self.state["calls"].append(call)
        del self.state["calls"][:-200]
        return call

    def _spend(self, amount: float, what: str) -> None:
        if amount > self.state["cash"] + 1e-6:
            raise ValidationError(f"not enough money for {what}: it costs {money(amount)} and you have "
                                  f"{money(self.state['cash'])}")
        self.state["cash"] = round(self.state["cash"] - amount, 2)

    def _earn(self, amount: float) -> None:
        self.state["cash"] = round(self.state["cash"] + amount, 2)

    def _perk(self, industry: str) -> bool:
        return any(c["industry"] == industry and c["status"] == "active" for c in self.state["companies"].values())

    # ---- properties and their problems ---------------------------------------------------------------
    def _make_property(self, kind_key: str, area_name: str, condition_bias: float = 0.0,
                       target_value: Optional[float] = None) -> dict:
        kind, area = KINDS_BY_KEY[kind_key], area_for(area_name)
        issues = []
        for r in renovations_for(kind):
            if r.fixes and self.rng.random() < 0.28 + condition_bias:
                issues.append(r.key)
        p = {"kind": kind.key, "area": area.name, "county": area.county,
             "address": f"{self.rng.randint(100, 2999)} {self.rng.choice(area.streets)}",
             "issues": issues, "done": [], "scale": 1.0, "wear": self.rng.randint(0, 8)}
        if target_value:
            p["scale"] = target_value / self.value(p)
        return p

    def condition(self, p: dict) -> int:
        lost = sum(RENOVATIONS_BY_KEY[i].fixes for i in p["issues"]) + p.get("wear", 0)
        return max(5, 100 - lost)

    def value(self, p: dict, extra_done: tuple[str, ...] = ()) -> float:
        kind, area = KINDS_BY_KEY[p["kind"]], area_for(p["area"])
        issues = [i for i in p["issues"] if i not in extra_done]
        done = list(p["done"]) + list(extra_done)
        lost = sum(RENOVATIONS_BY_KEY[i].fixes for i in issues) + p.get("wear", 0)
        cond = max(5, 100 - lost)
        premium = sum(RENOVATIONS_BY_KEY[d].premium for d in done)
        v = kind.base * area.index * p.get("scale", 1.0) * (0.40 + 0.60 * cond / 100) * (1 + premium)
        return round(v * self.t("price_multiplier"), -2)

    def inspection(self, p: dict) -> dict:
        """Everything wrong with a property and what each fix costs, before anyone buys it."""
        found = []
        for key in p["issues"]:
            r = RENOVATIONS_BY_KEY[key]
            found.append({"problem": _PROBLEMS.get(key, f"needs {r.label}"), "fix": r.label,
                          "fix_cost": self._reno_cost(p, r.key), "renovation": key})
        return {"condition": f"{self.condition(p)} out of 100", "problems": found,
                "fix_everything_costs": round(sum(f["fix_cost"] for f in found), -1),
                "worth_now": self.value(p), "worth_after_fixing_everything": self.value(p, tuple(p["issues"]))}

    def _describe(self, p: dict, pid: str = "") -> dict:
        kind = KINDS_BY_KEY[p["kind"]]
        out = {"id": pid, "type": kind.label, "address": f"{p['address']}, {p['area']}", "county": p["county"],
               "worth": self.value(p), "condition": self.condition(p),
               "problems": [_PROBLEMS.get(i, i) for i in p["issues"]]}
        return out

    # ---- the market ----------------------------------------------------------------------------------
    def _refresh_market(self) -> None:
        self.state["listings"] = {}
        weights = [6 if k.key in ("house", "duplex", "condo", "townhouse") else
                   3 if k.key in ("lot", "store", "apartments", "restaurant") else 1 for k in KINDS]
        for _ in range(int(self.t("listings_per_month"))):
            kind = self.rng.choices(KINDS, weights)[0]
            area = self.rng.choice(AREAS)
            p = self._make_property(kind.key, area.name, self.rng.uniform(-0.15, 0.25))
            p["asking"] = round(self.value(p) * self.rng.uniform(0.92, 1.12), -2)
            p["lowest"] = round(p["asking"] * self.rng.uniform(0.86, 0.97), -2)
            self.state["listings"][self._id("L")] = p

    def market(self, kind: str = "", area: str = "", max_price: float = 0) -> dict:
        with self._lock:
            rows = []
            for lid, p in self.state["listings"].items():
                if kind and p["kind"] != kind_for(kind).key:
                    continue
                if area and p["area"] != area_for(area).name:
                    continue
                if max_price and p["asking"] > max_price:
                    continue
                rows.append({**self._describe(p, lid), "asking": p["asking"]})
            return {"for_sale": sorted(rows, key=lambda r: r["asking"]), "cash": self.state["cash"],
                    "note": "every listing shows what's wrong with it; inspect for the repair costs"}

    def _find(self, pid: str) -> tuple[str, dict]:
        pid = (pid or "").strip().upper()
        for where in ("properties", "listings", "deeds"):
            if pid in self.state[where]:
                return where, self.state[where][pid]
        sale = self.state.get("sale") or {}
        for parcel in sale.get("parcels", []):
            if parcel["id"] == pid:
                return "sale", parcel["property"]
        raise NotFound(f"nothing with id {pid}")

    def inspect(self, pid: str) -> dict:
        with self._lock:
            where, p = self._find(pid)
            return {**self._describe(p, pid.upper()), **self.inspection(p)}

    def make_offer(self, listing_id: str, amount: float) -> dict:
        with self._lock:
            lid = listing_id.strip().upper()
            p = self.state["listings"].get(lid)
            if p is None:
                raise NotFound(f"no listing {listing_id}")
            if amount >= p["lowest"]:
                closing = round(amount * rules.BUYER_CLOSING_RATE, 2)
                self._spend(amount + closing, f"buying {p['address']} (price plus {money(closing)} closing)")
                del self.state["listings"][lid]
                prop = {k: v for k, v in p.items() if k not in ("asking", "lowest")}
                prop.update(paid=amount + closing, bought=self.state["today"], how="bought on the market")
                pid = self._id("P")
                self.state["properties"][pid] = prop
                self._note(f"Bought {prop['address']} for {money(amount)} plus {money(closing)} closing.")
                self.save()
                return {"accepted": True, "property_id": pid, "paid": amount, "closing_costs": closing,
                        "cash": self.state["cash"], "inspection": self.inspection(prop)}
            if amount >= p["asking"] * 0.80:
                counter = round((amount + p["asking"]) / 2, -2)
                p["counter"] = counter
                self.save()
                return {"accepted": False, "counter_offer": max(counter, p["lowest"]),
                        "note": "the seller countered; offer that amount to take it"}
            return {"accepted": False, "note": "the seller turned it down as too low"}

    # ---- renovations and flipping --------------------------------------------------------------------
    def _reno_cost(self, p: dict, key: str) -> float:
        cost = RENOVATIONS_BY_KEY[key].cost * size_factor(KINDS_BY_KEY[p["kind"]])
        if self._perk("renovation crew"):
            cost *= 0.85
        return round(cost, -1)

    def renovation_quotes(self, pid: str) -> dict:
        """Each renovation's cost, days and the sale price right after it, worked out before doing it."""
        with self._lock:
            where, p = self._find(pid)
            now = self.value(p)
            rows = []
            for r in renovations_for(KINDS_BY_KEY[p["kind"]]):
                if r.key in p["done"]:
                    continue
                after = self.value(p, (r.key,))
                cost = self._reno_cost(p, r.key)
                gain = after - now
                rows.append({"renovation": r.key, "what": r.label, "cost": cost,
                             "days": max(1, round(r.days * (0.8 if self._perk("renovation crew") else 1))),
                             "needed": r.key in p["issues"], "sale_price_after": after, "adds": gain,
                             "profit": round(gain - cost, -1),
                             "verdict": "worth it" if gain > cost * 1.1 else "about even" if gain >= cost * 0.9
                             else "loses money"})
            rows.sort(key=lambda r: -r["profit"])
            return {"property": self._describe(p, pid.upper()), "sale_price_now": now, "renovations": rows,
                    "best": [r["renovation"] for r in rows if r["verdict"] == "worth it"]}

    def renovate(self, pid: str, items: list[str]) -> dict:
        with self._lock:
            where, p = self._find(pid)
            if where != "properties":
                raise ValidationError("you can only renovate a property you own outright")
            if p.get("tenant"):
                raise ValidationError("it has a tenant; end the lease before renovating")
            allowed = {r.key for r in renovations_for(KINDS_BY_KEY[p["kind"]])}
            keys = [k.strip().lower() for k in items if k.strip()]
            bad = [k for k in keys if k not in allowed or k in p["done"]]
            if bad or not keys:
                raise ValidationError(f"can't do {', '.join(bad) or 'nothing'}; choose from "
                                      f"{', '.join(sorted(allowed - set(p['done'])))}")
            before = self.value(p)
            cost = sum(self._reno_cost(p, k) for k in keys)
            self._spend(cost, "the renovations")
            days = max(round(RENOVATIONS_BY_KEY[k].days * (0.8 if self._perk("renovation crew") else 1))
                       for k in keys)
            p["renovating"] = {"items": keys, "until": (self.today + timedelta(days=days)).isoformat()}
            p["paid"] = p.get("paid", 0) + cost
            self._note(f"Started {', '.join(RENOVATIONS_BY_KEY[k].label for k in keys)} at {p['address']} "
                       f"for {money(cost)}.")
            self.save()
            return {"started": keys, "cost": cost, "ready_on": p["renovating"]["until"], "worth_before": before,
                    "worth_when_done": self.value(p, tuple(keys)), "cash": self.state["cash"]}

    def list_for_sale(self, pid: str, price: float = 0, agent: bool = True) -> dict:
        with self._lock:
            where, p = self._find(pid)
            if where != "properties":
                raise ValidationError("you can only list a property you own outright")
            if p.get("renovating"):
                raise ValidationError(f"the renovation finishes on {p['renovating']['until']}; list it after")
            worth = self.value(p)
            p["listed"] = {"price": round(price or worth, -2), "agent": bool(agent), "since": self.state["today"]}
            self.save()
            return {"listed_at": p["listed"]["price"], "market_value": worth,
                    "note": "your secretary will call when offers come in; advance time to wait for them"}

    def _sell(self, pid: str, price: float, buyer: str) -> dict:
        p = self.state["properties"].pop(pid)
        agent = (p.get("listed") or {}).get("agent") and not self._perk("real estate brokerage")
        commission = round(price * rules.AGENT_COMMISSION, 2) if agent else 0.0
        transfer = round(price / 1000 * rules.TRANSFER_TAX_PER_1000, 2)
        net = price - commission - transfer
        self._earn(net)
        profit = round(net - p.get("paid", 0), 2)
        self._note(f"Sold {p['address']} to {buyer} for {money(price)}; profit {money(profit)}.")
        return {"sold": p["address"], "price": price, "agent_commission": commission, "transfer_tax": transfer,
                "you_got": net, "profit": profit, "cash": self.state["cash"]}

    # ---- renting ------------------------------------------------------------------------------------
    def rent_out(self, pid: str) -> dict:
        with self._lock:
            where, p = self._find(pid)
            if where != "properties":
                raise ValidationError("you can only rent a property you own outright")
            kind = KINDS_BY_KEY[p["kind"]]
            if not kind.rent_yield:
                raise ValidationError("vacant land doesn't rent; build or sell it")
            rent = round(self.value(p) * kind.rent_yield / 12 * (1.1 if self._perk("property management") else 1),
                         -1)
            p["tenant"] = {"rent": rent, "status": "looking"}
            p.pop("listed", None)
            self.save()
            return {"monthly_rent": rent, "note": "your secretary will tell you when a tenant signs"}

    # ---- the tax sale --------------------------------------------------------------------------------
    def _post_sale(self) -> None:
        day = rules.next_sale_day(self.today)
        parcels = []
        for _ in range(int(self.t("parcels_per_sale"))):
            area = self.rng.choice(AREAS)
            if self.rng.random() < 0.8:
                target = self.rng.uniform(self.t("tax_house_min"), self.t("tax_house_max"))
                prop = self._make_property("house", area.name, 0.15, target_value=target)
            else:
                prop = self._make_property(self.rng.choice(("lot", "duplex", "store")), area.name, 0.2)
            taxes = round(rules.yearly_property_tax(self.value(prop)) * self.rng.uniform(0.4, 1.0), 2)
            opening = round(taxes + rules.TAX_SALE_COSTS, -1)
            parcels.append({"id": self._id("T"), "property": prop, "taxes_due": taxes, "opening_bid": opening,
                            "owner": self.rng.choice(_OWNERS), "status": "for sale"})
        self.state["sale"] = {"date": day.isoformat(), "county_places": rules.SALE_PLACES, "parcels": parcels}

    def tax_sale(self) -> dict:
        with self._lock:
            sale = self.state["sale"]
            rows = []
            for parcel in sale["parcels"]:
                if parcel["status"] != "for sale":
                    continue
                p = parcel["property"]
                rows.append({**self._describe(p, parcel["id"]), "owner": parcel["owner"],
                             "taxes_due": parcel["taxes_due"], "opening_bid": parcel["opening_bid"]})
            day = date.fromisoformat(sale["date"])
            return {"sale_day": sale["date"], "today": self.state["today"], "is_today": day == self.today,
                    "where": rules.SALE_PLACES, "parcels": rows, "cash": self.state["cash"],
                    "how_it_works": rules.LESSONS["tax sale"][0]}

    def go_to_tax_sale(self) -> dict:
        days = (date.fromisoformat(self.state["sale"]["date"]) - self.today).days
        if days > 0:
            self.advance(days)
        return self.tax_sale()

    def _parcel(self, parcel_id: str) -> dict:
        for parcel in self.state["sale"]["parcels"]:
            if parcel["id"] == parcel_id.strip().upper():
                return parcel
        raise NotFound(f"no parcel {parcel_id} at this tax sale")

    def _rivals(self, opening: float) -> list[dict]:
        rivals = []
        for name in self.rng.sample(_BIDDERS, min(len(_BIDDERS), int(self.t("rival_bidders")))):
            cap = self.t("rival_cap")
            if self.rng.random() < self.t("rival_big_spender_chance"):
                top = self.rng.uniform(cap, cap * 2.2)
            else:
                top = self.rng.uniform(opening, max(opening, cap))
            rivals.append({"name": name, "max": round(max(opening, top), -1)})
        return rivals

    def start_auction(self, parcel_id: str) -> dict:
        with self._lock:
            sale = self.state["sale"]
            if date.fromisoformat(sale["date"]) != self.today:
                raise ValidationError(f"the tax sale is on {sale['date']}; go to the tax sale first")
            parcel = self._parcel(parcel_id)
            if parcel["status"] != "for sale":
                raise ValidationError(f"{parcel['id']} is already {parcel['status']}")
            rivals = self._rivals(parcel["opening_bid"])
            lead = max(rivals, key=lambda r: r["max"])
            self.state["auction"] = {"parcel": parcel["id"], "high": parcel["opening_bid"], "leader": lead["name"],
                                     "rivals": rivals, "history": [f"{lead['name']} opens at "
                                                                   f"{money(parcel['opening_bid'])}"]}
            self.save()
            return self._auction_view(f"Bidding opens on {parcel['property']['address']} at "
                                      f"{money(parcel['opening_bid'])}. {lead['name']} has the bid.")

    def _auction_view(self, said: str) -> dict:
        a = self.state["auction"]
        return {"auctioneer": said, "high_bid": a["high"], "high_bidder": a["leader"],
                "bidders": [r["name"] for r in a["rivals"]], "parcel": a["parcel"], "cash": self.state["cash"],
                "next": "bid a higher amount, or pass"}

    def bid(self, amount: float) -> dict:
        with self._lock:
            a = self.state.get("auction")
            if not a:
                raise ValidationError("no auction is running; start one on a parcel at the tax sale")
            step = self.t("bid_increment")
            if amount < a["high"] + step and a["leader"] != "you":
                raise ValidationError(f"the bid is {money(a['high'])}; bid at least {money(a['high'] + step)}")
            if amount > self.state["cash"]:
                raise ValidationError(f"you only have {money(self.state['cash'])}; the sheriff wants the full "
                                      "amount by cashier's check on the spot")
            a["high"], a["leader"] = round(amount, 2), "you"
            a["history"].append(f"you bid {money(amount)}")
            still_in = [r for r in a["rivals"] if r["max"] >= amount + step]
            if still_in:
                r = max(still_in, key=lambda r: r["max"])
                jump = min(r["max"], amount + step * self.rng.choice((1, 1, 2, 5)))
                a["high"], a["leader"] = round(jump, -1), r["name"]
                a["history"].append(f"{r['name']} bids {money(a['high'])}")
                self.save()
                return self._auction_view(f"{r['name']} bids {money(a['high'])}. Do I hear more?")
            return self._win()

    def pass_auction(self) -> dict:
        with self._lock:
            a = self.state.get("auction")
            if not a:
                raise ValidationError("no auction is running")
            parcel = self._parcel(a["parcel"])
            if a["leader"] == "you":
                return self._win()
            parcel["status"] = f"sold to {a['leader']}"
            self.state["auction"] = None
            self._note(f"{a['leader']} won {parcel['property']['address']} for {money(a['high'])}.")
            self.save()
            return {"sold_to": a["leader"], "price": a["high"]}

    def _win(self) -> dict:
        a = self.state["auction"]
        parcel = self._parcel(a["parcel"])
        price = a["high"]
        self._spend(price, "the winning bid")
        excess = round(max(0.0, price - parcel["opening_bid"]), 2)
        parcel["status"] = "sold to you"
        did = self._id("D")
        self.state["deeds"][did] = {**parcel["property"], "paid": price, "taxes_paid": 0.0,
                                    "sale_day": self.state["today"], "owner": parcel["owner"],
                                    "excess_funds": excess, "notice": None}
        self.state["auction"] = None
        self._note(f"Won the tax deed on {parcel['property']['address']} for {money(price)}.")
        self.save()
        sale_day = self.today
        return {"you_won": parcel["property"]["address"], "deed_id": did, "paid": price,
                "excess_funds_for_owner": excess, "cash": self.state["cash"],
                "owner_can_redeem_until": "at least " + (sale_day + timedelta(days=365)).isoformat(),
                "if_redeemed_in_first_year_you_get": rules.redemption_amount(price, 0, sale_day, sale_day),
                "explain": rules.LESSONS["redemption"][0]}

    def deeds(self) -> dict:
        with self._lock:
            rows = []
            for did, d in self.state["deeds"].items():
                sale_day = date.fromisoformat(d["sale_day"])
                rows.append({**self._describe(d, did), "paid": d["paid"], "sale_day": d["sale_day"],
                             "redemption_price_today": rules.redemption_amount(d["paid"], d["taxes_paid"],
                                                                               sale_day, self.today),
                             "can_foreclose_from": (sale_day + timedelta(days=rules.REDEMPTION_WAIT_DAYS)).isoformat(),
                             "notice": d["notice"]})
            return {"tax_deeds": rows}

    def foreclose(self, deed_id: str) -> dict:
        """Send the notice that bars the right to redeem (allowed 12 months after the sale)."""
        with self._lock:
            did = deed_id.strip().upper()
            d = self.state["deeds"].get(did)
            if d is None:
                raise NotFound(f"no tax deed {deed_id}")
            ready = date.fromisoformat(d["sale_day"]) + timedelta(days=rules.REDEMPTION_WAIT_DAYS)
            if self.today < ready:
                raise ValidationError(f"Georgia law makes you wait 12 months after the sale; you can send the "
                                      f"notice from {ready.isoformat()}")
            if d["notice"]:
                raise ValidationError(f"the notice is already out; the deadline is {d['notice']}")
            self._spend(rules.NOTICE_COST, "the sheriff's service and newspaper notice")
            d["paid"] += rules.NOTICE_COST
            d["notice"] = (self.today + timedelta(days=rules.NOTICE_LEAD_DAYS)).isoformat()
            self.save()
            return {"notice_sent": True, "redemption_deadline": d["notice"], "cost": rules.NOTICE_COST,
                    "explain": rules.LESSONS["foreclosure"][0]}

    # ---- companies -----------------------------------------------------------------------------------
    def start_company(self, name: str, industry: str, entity: str = "llc", employees: int = 1) -> dict:
        with self._lock:
            name = " ".join((name or "").split())
            if not name:
                raise ValidationError("the company needs a name")
            entity = (entity or "llc").lower()
            if entity not in rules.ENTITY_TYPES:
                raise ValidationError("choose an LLC or a corporation")
            ind = (industry or "").lower().strip()
            match = [i for i in INDUSTRIES if ind and (ind in i or i in ind)]
            if not match:
                raise ValidationError("industries: " + ", ".join(INDUSTRIES))
            if any(c.get("base", c["name"]).lower() == name.lower() for c in self.state["companies"].values()):
                raise ValidationError(f"the Secretary of State already has a company named {name}")
            employees = max(1, int(employees))
            license_fee = rules.occupation_tax(0, employees)
            cost = rules.SOS_ARTICLES + license_fee
            self._spend(cost, "the state filing and the Atlanta business license")
            suffix = ", LLC" if entity == "llc" else ", Inc."
            cid = self._id("B")
            self.state["companies"][cid] = {"name": name + suffix, "base": name, "entity": entity, "industry": match[0],
                                            "employees": employees, "status": "active", "founded": self.state["today"],
                                            "gross_this_year": 0.0, "profit_total": 0.0, "registration_paid": [self.today.year]}
            self._note(f"Started {name + suffix} ({match[0]}).")
            self.save()
            perk = INDUSTRIES[match[0]][3]
            return {"company_id": cid, "name": name + suffix, "paid": cost, "cash": self.state["cash"],
                    "steps_done": [
                        f"Filed Articles of {'Organization' if entity == 'llc' else 'Incorporation'} with the "
                        f"Georgia Secretary of State on eCorp: {money(rules.SOS_ARTICLES)}",
                        "Named a registered agent at your Atlanta office (a Georgia street address)",
                        "Got a free EIN from the IRS",
                        f"Got the City of Atlanta Business Occupational Tax Certificate on ATLBIZ, with notarized "
                        f"E-Verify and SAVE affidavits: {money(license_fee)}"],
                    "every_year": f"State annual registration {money(rules.ANNUAL_REGISTRATION)} by April 1 and "
                                  "the Atlanta license renewal", **({"perk": perk} if perk else {})}

    def staff(self, company_id: str, employees: int) -> dict:
        with self._lock:
            c = self.state["companies"].get(company_id.strip().upper())
            if c is None:
                raise NotFound(f"no company {company_id}")
            c["employees"] = max(1, int(employees))
            self.save()
            rev, cost, _, _ = INDUSTRIES[c["industry"]]
            return {"company": c["name"], "employees": c["employees"],
                    "expected_monthly_profit": round((rev - cost) * c["employees"], -2)}

    def close_company(self, company_id: str) -> dict:
        with self._lock:
            c = self.state["companies"].get(company_id.strip().upper())
            if c is None:
                raise NotFound(f"no company {company_id}")
            c["status"] = "dissolved"
            self.save()
            return {"dissolved": c["name"]}

    # ---- the secretary -------------------------------------------------------------------------------
    def calls(self, only_new: bool = True) -> dict:
        with self._lock:
            rows = [c for c in self.state["calls"] if c["status"] == "new" or not only_new]
            for c in rows:
                if c["kind"] not in CALL_KINDS_TO_ANSWER and c["status"] == "new":
                    c["status"] = "heard"
            self.save()
            return {"calls": rows[-30:], "waiting_for_answer": [c["id"] for c in rows
                                                                if c["kind"] in CALL_KINDS_TO_ANSWER
                                                                and c["status"] == "new"]}

    def answer_call(self, call_id: str, accept: bool) -> dict:
        with self._lock:
            call = next((c for c in self.state["calls"] if c["id"] == call_id.strip().upper()), None)
            if call is None:
                raise NotFound(f"no call {call_id}")
            if call["kind"] not in CALL_KINDS_TO_ANSWER or call["status"] != "new":
                raise ValidationError("that call doesn't need an answer")
            call["status"] = "accepted" if accept else "declined"
            if not accept:
                self.save()
                return {"declined": call["text"]}
            pid = call["property_id"]
            if pid not in self.state["properties"]:
                raise ValidationError("you don't own that property any more")
            out = self._sell(pid, call["amount"], call["from"])
            self.save()
            return out

    # ---- time ---------------------------------------------------------------------------------------
    def advance(self, days: int = 1) -> dict:
        with self._lock:
            days = max(1, min(int(days), MAX_DAYS_PER_ADVANCE))
            cash0, calls0 = self.state["cash"], len(self.state["calls"])
            for _ in range(days):
                self._next_day()
            self.save()
            new_calls = self.state["calls"][calls0:]
            return {"today": self.state["today"], "cash": self.state["cash"],
                    "cash_change": round(self.state["cash"] - cash0, 2),
                    "new_calls": [f"{c['from']}: {c['text']}" for c in new_calls][-12:],
                    "next_tax_sale": self.state["sale"]["date"]}

    def _next_day(self) -> None:
        if self.state.get("auction"):
            self.pass_auction()
        sale_day = date.fromisoformat(self.state["sale"]["date"])
        day = self.today + timedelta(days=1)
        self.state["today"] = day.isoformat()
        if day > sale_day:
            self._post_sale()
        elif day == sale_day:
            self._call("reminder", "Secretary", "The sheriff's tax sale is today. "
                       + " / ".join(f"{k}: {v}" for k, v in rules.SALE_PLACES.items()))
        elif (sale_day - day).days == 7:
            self._call("reminder", "Secretary", f"The tax sale is next week, on {sale_day.isoformat()}. "
                       "The list is out in the paper.")
        if day.day == 1:
            self._month_start(day)
        if (day.month, day.day) == rules.TAX_BILL_DAY:
            self._tax_bills()
        if (day.month, day.day) == (4, 1):
            self._annual_registration(day)
        heat = self.t("market_heat")
        for pid, p in list(self.state["properties"].items()):
            reno = p.get("renovating")
            if reno and day >= date.fromisoformat(reno["until"]):
                for k in reno["items"]:
                    if k in p["issues"]:
                        p["issues"].remove(k)
                    p["done"].append(k)
                p.pop("renovating")
                self._call("info", "Your contractor", f"The work at {p['address']} is finished. It's now worth "
                           f"about {money(self.value(p))}.", property_id=pid)
            listed = p.get("listed")
            if listed and not p.get("renovating"):
                ratio = listed["price"] / max(1.0, self.value(p))
                chance = 0.07 * heat * (1 if listed["agent"] else 0.5) * max(0.03, min(2.0, 3.4 - 2.6 * ratio))
                if self.rng.random() < chance:
                    amount = round(listed["price"] * self.rng.uniform(0.9, 1.0), -2)
                    self._call("offer", self.rng.choice(_BUYERS), f"Offers {money(amount)} for {p['address']} "
                               f"(listed at {money(listed['price'])}).", property_id=pid, amount=amount)
            elif not listed and not p.get("tenant") and self.rng.random() < 0.004 * heat:
                amount = round(self.value(p) * self.rng.uniform(0.85, 1.05), -2)
                self._call("buyer", self.rng.choice(_BUYERS), f"Wants to buy {p['address']} for {money(amount)}.",
                           property_id=pid, amount=amount)
            tenant = p.get("tenant")
            if tenant and tenant["status"] == "looking" and self.rng.random() < 0.08:
                tenant["status"] = "signed"
                self._call("info", "Secretary", f"A tenant signed a lease at {p['address']} for "
                           f"{money(tenant['rent'])} a month.", property_id=pid)
        self._redemptions(day)

    def _redemptions(self, day: date) -> None:
        rate = min(0.99, max(0.0, self.t("redemption_rate")))
        daily = 1 - (1 - rate) ** (1 / 365)
        for did, d in list(self.state["deeds"].items()):
            sale_day = date.fromisoformat(d["sale_day"])
            notice = d.get("notice")
            chance = daily * (6 if notice else 1)
            if self.rng.random() < chance:
                amount = rules.redemption_amount(d["paid"] - (rules.NOTICE_COST if notice else 0), d["taxes_paid"],
                                                 sale_day, day)
                if notice and (day - date.fromisoformat(notice)).days > -15:
                    amount += rules.NOTICE_COST
                del self.state["deeds"][did]
                self._earn(amount)
                self._call("info", "Secretary", f"{d['owner']} redeemed {d['address']}. They paid you "
                           f"{money(amount)}; you put in {money(d['paid'] + d['taxes_paid'])}.")
                self._note(f"{d['owner']} redeemed {d['address']} for {money(amount)}.")
            elif notice and day >= date.fromisoformat(notice):
                del self.state["deeds"][did]
                prop = {k: v for k, v in d.items() if k not in ("sale_day", "owner", "excess_funds", "notice",
                                                                "taxes_paid")}
                prop.update(paid=d["paid"] + d["taxes_paid"], bought=day.isoformat(), how="tax deed foreclosure")
                pid = self._id("P")
                self.state["properties"][pid] = prop
                self._call("info", "Your attorney", f"Nobody redeemed {d['address']} by the deadline. It's yours "
                           f"outright now, property {pid}, worth about {money(self.value(prop))}.", property_id=pid)

    def _month_start(self, day: date) -> None:
        rent = sum(p["tenant"]["rent"] for p in self.state["properties"].values()
                   if (p.get("tenant") or {}).get("status") == "signed")
        if rent:
            self._earn(rent)
            self._note(f"Collected {money(rent)} in rent.")
        for c in self.state["companies"].values():
            if c["status"] != "active":
                continue
            rev, cost, swing, _ = INDUSTRIES[c["industry"]]
            revenue = max(0.0, rev * c["employees"] * (1 + self.rng.gauss(0, swing)))
            profit = round(revenue - cost * c["employees"], 2)
            c["gross_this_year"] = round(c["gross_this_year"] + revenue, 2)
            c["profit_total"] = round(c["profit_total"] + profit, 2)
            self.state["cash"] = round(self.state["cash"] + profit, 2)
            if profit < 0:
                self._call("info", c["name"], f"Lost {money(-profit)} last month.")
        if day.month == 1:
            for c in self.state["companies"].values():
                if c["status"] == "active":
                    fee = rules.occupation_tax(c["gross_this_year"], c["employees"])
                    self.state["cash"] = round(self.state["cash"] - fee, 2)
                    self._call("info", "City of Atlanta", f"Renewed {c['name']}'s business license: {money(fee)}.")
                    c["gross_this_year"] = 0.0
        self._refresh_market()

    def _tax_bills(self) -> None:
        total = sum(rules.yearly_property_tax(self.value(p)) for p in self.state["properties"].values())
        for d in self.state["deeds"].values():
            tax = rules.yearly_property_tax(self.value(d))
            d["taxes_paid"] = round(d["taxes_paid"] + tax, 2)  # added to what the owner must pay to redeem
            total += tax
        if total:
            self.state["cash"] = round(self.state["cash"] - total, 2)
            self._call("info", "Tax Commissioner", f"Paid {money(total)} in property taxes for the year.")

    def _annual_registration(self, day: date) -> None:
        for c in self.state["companies"].values():
            if c["status"] != "active" or day.year in c["registration_paid"]:
                continue
            fee = rules.ANNUAL_REGISTRATION
            if self.state["cash"] < fee:
                fee += rules.LATE_REGISTRATION_PENALTY
            self.state["cash"] = round(self.state["cash"] - fee, 2)
            c["registration_paid"].append(day.year)
            self._call("info", "Secretary", f"Filed {c['name']}'s Georgia annual registration: {money(fee)}.")

    # ---- the money meter and the whole picture ------------------------------------------------------
    def net_worth(self) -> dict:
        props = sum(self.value(p) for p in self.state["properties"].values())
        deeds = sum(d["paid"] + d["taxes_paid"] for d in self.state["deeds"].values())
        companies = sum(max(0.0, c["profit_total"]) * 0.5 for c in self.state["companies"].values()
                        if c["status"] == "active")
        return {"cash": self.state["cash"], "properties": round(props, 2), "tax_deeds": round(deeds, 2),
                "companies": round(companies, 2), "net_worth": round(self.state["cash"] + props + deeds + companies, 2)}

    def status(self) -> dict:
        with self._lock:
            owned = [{**self._describe(p, pid), "paid": p.get("paid", 0),
                      "renovating_until": (p.get("renovating") or {}).get("until"),
                      "listed_at": (p.get("listed") or {}).get("price"),
                      "rent": (p.get("tenant") or {}).get("rent") if (p.get("tenant") or {}).get("status") == "signed"
                      else None} for pid, p in self.state["properties"].items()]
            new_calls = [c for c in self.state["calls"] if c["status"] == "new"]
            return {"today": self.state["today"], "money": self.net_worth(), "properties": owned,
                    "tax_deeds": self.deeds()["tax_deeds"],
                    "companies": [{"id": cid, **{k: c[k] for k in ("name", "industry", "employees", "status",
                                                                     "profit_total")}}
                                  for cid, c in self.state["companies"].items()],
                    "new_calls": len(new_calls), "next_tax_sale": self.state["sale"]["date"],
                    "auction_running": bool(self.state.get("auction")),
                    "where_we_left_off": [f"{e['day']}: {e['text']}" for e in self.state["log"][-6:]],
                    "last_saved": self.state.get("saved_at", "")}

    def lesson(self, topic: str) -> dict:
        t = (topic or "").lower()
        for key, (text, src) in rules.LESSONS.items():
            if key in t or t in key:
                return {"topic": key, "explain": text, "source": rules.SOURCES[src]}
        return {"topics": list(rules.LESSONS)}

    # ---- development mode: Jarvis can change the game -----------------------------------------------
    def dev(self, action: str, key: str = "", value: float = 0) -> dict:
        with self._lock:
            if action == "set":
                if key not in TUNABLES:
                    raise ValidationError("game settings: " + ", ".join(TUNABLES))
                self.state["tunables"][key] = float(value)
                if key == "starting_cash" and not self.state["properties"] and not self.state["deeds"]:
                    self.state["cash"] = float(value)
            elif action == "add_money":
                self._earn(float(value))
            elif action == "set_money":
                self.state["cash"] = float(value)
            elif action == "new_tax_sale":
                self._post_sale()
            elif action == "new_listings":
                self._refresh_market()
            elif action != "settings":
                raise ValidationError("dev actions: settings, set, add_money, set_money, new_tax_sale, new_listings")
            if action != "settings":
                self.state["dev_changes"].append({"day": self.state["today"], "action": action, "key": key,
                                                  "value": value})
                self.save()
            return {"settings": self.state["tunables"], "cash": self.state["cash"]}


_PROBLEMS = {
    "roof": "roof leaks, with water stains on the ceilings", "hvac": "heating and air don't work",
    "foundation": "foundation is cracked and the floors slope", "plumbing": "old galvanized pipes leak",
    "electrical": "outdated wiring and a failing panel", "windows": "windows are broken or rotted",
    "kitchen": "kitchen is gutted or falling apart", "bathrooms": "bathrooms are worn out",
    "flooring": "floors are damaged", "paint": "walls need patching and paint",
    "exterior": "siding is rotting and the paint is peeling", "curb_appeal": "overgrown yard",
    "appliances": "appliances are missing", "cleanout": "full of trash left behind",
    "facade": "storefront is boarded and damaged", "parking": "parking lot is broken up",
    "sprinklers": "fire sprinklers aren't up to code",
}
_OWNERS = ("Estate of Willie Mae Johnson", "Marcus T. Reed", "Peachtree Holdings LLC", "Darnell Greene",
           "Heirs of Robert Lee Walker", "Gloria Bennett", "Southern Crescent Investments", "Tyrone Mitchell",
           "Annie Pearl Davis", "Lakewood Partners LLC")
_BIDDERS = ("Ray from Westside Capital", "Denise Holloway", "Big Mike Carter", "Peach State Tax Fund",
            "Carlos Ramirez", "Keisha Ward", "Atlanta Redemption Group", "Old man Fred Simmons")
_BUYERS = ("A first-time buyer", "An investor from Buckhead", "A young couple", "A flipper from Decatur",
           "A family moving from Chicago", "A landlord with 20 rentals", "A doctor at Grady")
