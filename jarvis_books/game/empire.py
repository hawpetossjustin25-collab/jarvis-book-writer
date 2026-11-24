"""The road to a trillion dollars: the goal and its milestones, bank loans, Atlanta's transit systems (with
the hyperloop upgrade) and car lots. Mixed into Game; uses its state, dice and helpers."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from jarvis_books.errors import NotFound, ValidationError

GOAL = 1_000_000_000_000.0
MILESTONES = (100_000.0, 1e6, 1e7, 1e8, 1e9, 1e10, 1e11, GOAL)
LOAN_RATE = 0.08  # yearly, interest paid on the 1st of each month (game estimate of a commercial bank rate)
LOAN_TO_VALUE = 0.75
COMPANY_PROFIT_MULTIPLE = 8  # a company is worth about 8 years of its profit

# Atlanta's real transit systems (facts from MARTA and the City of Atlanta); prices and fares are game values.
TRANSIT = {
    "streetcar": {"name": "Atlanta Streetcar (light rail)", "price": 120e6, "monthly": 0.45e6,
                  "about": "2.7-mile downtown loop with 12 stops, from Centennial Olympic Park through Sweet Auburn",
                  "upgrades": {"hyperloop": {"cost": 2.5e9, "days": 540, "multiplier": 9.0,
                                             "about": "hyperloop pods on the loop, extended to the airport"}}},
    "marta_rail": {"name": "MARTA rail", "price": 9e9, "monthly": 48e6,
                   "about": "heavy rail with 38 stations on the Red, Gold, Blue and Green lines, out to the airport",
                   "upgrades": {"hyperloop": {"cost": 70e9, "days": 1095, "multiplier": 7.0,
                                              "about": "a hyperloop network replacing the four rail lines"}}},
    "marta_bus": {"name": "MARTA bus system", "price": 1.4e9, "monthly": 7.5e6,
                  "about": "MARTA's bus routes across Fulton, DeKalb and Clayton counties",
                  "upgrades": {"electric": {"cost": 600e6, "days": 365, "multiplier": 1.6,
                                            "about": "an all-electric fleet: cheaper to run, more riders"}}},
}

# what a car lot buys a car for, what it sells it for (game values)
CARS = {
    "used sedan": (12_000, 15_500), "suv": (28_000, 34_000), "pickup truck": (32_000, 38_500),
    "luxury car": (55_000, 64_000), "electric car": (38_000, 44_500), "sports car": (70_000, 82_000),
}
LOT_CAPACITY = 80
MONTHLY_SELL_CHANCE = 0.35


class Empire:
    state: dict[str, Any]

    # ---- the goal --------------------------------------------------------------------------------------
    def goal(self) -> dict:
        worth = self.net_worth()["net_worth"]
        hit = self.state.setdefault("milestones_hit", [])
        nxt = next((m for m in MILESTONES if m > worth), None)
        return {"target": GOAL, "net_worth": worth, "progress_percent": round(max(0.0, worth) / GOAL * 100, 7),
                "next_milestone": nxt, "milestones_reached": hit, "won_on": self.state.get("won_on")}

    def _check_goal(self) -> None:
        worth = self.net_worth()["net_worth"]
        hit = self.state.setdefault("milestones_hit", [])
        for m in MILESTONES:
            if worth >= m and m not in hit:
                hit.append(m)
                if m >= GOAL:
                    self.state["won_on"] = self.state["today"]
                    self._call("milestone", "Secretary", "YOU DID IT: your net worth passed ONE TRILLION DOLLARS. "
                               "You built an empire from $8,000 in Atlanta.")
                else:
                    self._call("milestone", "Secretary", f"Milestone: your net worth passed ${m:,.0f}. "
                               f"Next stop ${next(x for x in MILESTONES if x > m):,.0f}.")

    # ---- bank loans -----------------------------------------------------------------------------------
    def _loans(self) -> dict:
        return self.state.setdefault("loans", {})

    def loan_total(self, pid: str = "") -> float:
        return round(sum(ln["balance"] for ln in self._loans().values() if not pid or ln["property_id"] == pid), 2)

    def take_loan(self, property_id: str, amount: float) -> dict:
        with self._lock:
            pid = property_id.strip().upper()
            p = self.state["properties"].get(pid)
            if p is None:
                raise ValidationError("the bank only lends against a property you own outright")
            room = round(self.value(p) * LOAN_TO_VALUE - self.loan_total(pid), -2)
            if amount <= 0 or amount > room:
                raise ValidationError(f"the bank will lend up to {room:,.0f} more on {p['address']} "
                                      f"({LOAN_TO_VALUE:.0%} of its value, less what you already owe on it)")
            lid = self._id("N")
            self._loans()[lid] = {"property_id": pid, "balance": round(amount, 2), "rate": LOAN_RATE,
                                  "since": self.state["today"], "address": p["address"]}
            self._earn(amount)
            self._note(f"Borrowed ${amount:,.0f} against {p['address']}.")
            self.save()
            return {"loan_id": lid, "borrowed": amount, "yearly_rate": LOAN_RATE,
                    "monthly_interest": round(amount * LOAN_RATE / 12, 2), "cash": self.state["cash"]}

    def repay_loan(self, loan_id: str, amount: float = 0) -> dict:
        with self._lock:
            ln = self._loans().get(loan_id.strip().upper())
            if ln is None:
                raise NotFound(f"no loan {loan_id}")
            pay = round(min(amount or ln["balance"], ln["balance"]), 2)
            self._spend(pay, "the loan payment")
            ln["balance"] = round(ln["balance"] - pay, 2)
            if ln["balance"] <= 0.005:
                del self._loans()[loan_id.strip().upper()]
            self.save()
            return {"paid": pay, "still_owed": max(0.0, ln["balance"]), "cash": self.state["cash"]}

    def _settle_loans(self, pid: str) -> float:
        owed = self.loan_total(pid)
        for lid in [k for k, ln in self._loans().items() if ln["property_id"] == pid]:
            del self._loans()[lid]
        return owed

    # ---- transit ---------------------------------------------------------------------------------------
    def transit(self) -> dict:
        with self._lock:
            owned = self.state.setdefault("transit", {})
            rows = []
            for key, t in TRANSIT.items():
                mine = owned.get(key)
                row = {"system": key, "name": t["name"], "about": t["about"], "price": t["price"],
                       "monthly_income": self._transit_income(key) if mine else t["monthly"], "owned": bool(mine),
                       "upgrades": {u: {"cost": v["cost"], "months": round(v["days"] / 30), "about": v["about"],
                                        "income_times": v["multiplier"]} for u, v in t["upgrades"].items()}}
                if mine:
                    row.update(done=mine["upgrades"], building=mine.get("building"))
                rows.append(row)
            return {"systems": rows, "cash": self.state["cash"],
                    "note": "the real systems are public; in the game you can buy them"}

    def _transit_income(self, key: str) -> float:
        mine = self.state.get("transit", {}).get(key)
        if not mine:
            return 0.0
        income = TRANSIT[key]["monthly"]
        for u in mine["upgrades"]:
            income *= TRANSIT[key]["upgrades"][u]["multiplier"]
        return round(income, 2)

    def buy_transit(self, system: str) -> dict:
        with self._lock:
            key = self._system(system)
            owned = self.state.setdefault("transit", {})
            if key in owned:
                raise ValidationError(f"you already own {TRANSIT[key]['name']}")
            t = TRANSIT[key]
            self._spend(t["price"], t["name"])
            owned[key] = {"bought": self.state["today"], "paid": t["price"], "upgrades": [], "building": None}
            self._note(f"Bought {t['name']} for ${t['price']:,.0f}.")
            self.save()
            return {"bought": t["name"], "paid": t["price"], "monthly_income": t["monthly"], "cash": self.state["cash"]}

    def upgrade_transit(self, system: str, upgrade: str = "hyperloop") -> dict:
        with self._lock:
            key = self._system(system)
            mine = self.state.setdefault("transit", {}).get(key)
            if not mine:
                raise ValidationError(f"buy {TRANSIT[key]['name']} first")
            ups = TRANSIT[key]["upgrades"]
            if upgrade not in ups:
                raise ValidationError(f"{TRANSIT[key]['name']} upgrades: {', '.join(ups)}")
            if upgrade in mine["upgrades"] or mine.get("building"):
                raise ValidationError("that upgrade is done or already being built")
            u = ups[upgrade]
            self._spend(u["cost"], f"the {upgrade} upgrade")
            mine["paid"] += u["cost"]
            mine["building"] = {"upgrade": upgrade, "until": (self.today + timedelta(days=u["days"])).isoformat()}
            self.save()
            return {"building": upgrade, "cost": u["cost"], "opens_on": mine["building"]["until"],
                    "income_after": round(self._transit_income(key) * u["multiplier"], 2), "cash": self.state["cash"]}

    def _system(self, text: str) -> str:
        t = (text or "").lower()
        if t in TRANSIT:
            return t
        if "street" in t or "light" in t:
            return "streetcar"
        if "bus" in t:
            return "marta_bus"
        if "rail" in t or "train" in t or "marta" in t or "subway" in t:
            return "marta_rail"
        raise ValidationError("transit systems: " + ", ".join(v["name"] for v in TRANSIT.values()))

    def _transit_value(self) -> float:
        return sum(m["paid"] for m in self.state.get("transit", {}).values())

    # ---- car lots -------------------------------------------------------------------------------------
    def car_lot(self, property_id: str = "") -> dict:
        with self._lock:
            lots = {pid: p for pid, p in self.state["properties"].items() if p["kind"] == "car_lot"}
            if property_id:
                pid = property_id.strip().upper()
                if pid not in lots:
                    raise ValidationError(f"{pid} isn't a car lot you own")
                lots = {pid: lots[pid]}
            return {"lots": [{"id": pid, "address": p["address"], "cars": p.get("cars", {}),
                              "space_left": LOT_CAPACITY - sum(p.get("cars", {}).values()),
                              "sold_total": p.get("cars_sold", 0)} for pid, p in lots.items()],
                    "cars_you_can_stock": {k: {"buy": b, "sells_for": s} for k, (b, s) in CARS.items()},
                    "note": "" if lots else "buy a car lot first (game_market kind 'car lot')"}

    def stock_cars(self, property_id: str, model: str, count: int) -> dict:
        with self._lock:
            pid = property_id.strip().upper()
            p = self.state["properties"].get(pid)
            if p is None or p["kind"] != "car_lot":
                raise ValidationError(f"{pid} isn't a car lot you own")
            m = (model or "").lower().strip()
            match = [k for k in CARS if m and (m in k or k in m)]
            if not match:
                raise ValidationError("cars: " + ", ".join(CARS))
            count = int(count)
            cars = p.setdefault("cars", {})
            if count <= 0 or sum(cars.values()) + count > LOT_CAPACITY:
                raise ValidationError(f"the lot holds {LOT_CAPACITY} cars; space for "
                                      f"{LOT_CAPACITY - sum(cars.values())} more")
            cost = CARS[match[0]][0] * count
            self._spend(cost, f"{count} {match[0]}s")
            cars[match[0]] = cars.get(match[0], 0) + count
            self.save()
            return {"stocked": count, "model": match[0], "paid": cost, "each_sells_for": CARS[match[0]][1],
                    "cash": self.state["cash"]}

    def _sell_cars(self) -> None:
        heat = self.t("market_heat")
        for pid, p in self.state["properties"].items():
            cars = p.get("cars") or {}
            sold, income = 0, 0.0
            for model, n in list(cars.items()):
                k = sum(self.rng.random() < min(0.95, MONTHLY_SELL_CHANCE * heat) for _ in range(n))
                if k:
                    cars[model] = n - k
                    sold += k
                    income += k * CARS[model][1]
                    if not cars[model]:
                        del cars[model]
            if sold:
                self._earn(income)
                p["cars_sold"] = p.get("cars_sold", 0) + sold
                self._call("info", "Your car lot manager", f"Sold {sold} cars at {p['address']} last month for "
                           f"${income:,.0f}.", property_id=pid)

    def _cars_value(self) -> float:
        return sum(CARS[m][0] * n for p in self.state["properties"].values() for m, n in (p.get("cars") or {}).items())

    # ---- monthly and yearly money ---------------------------------------------------------------------
    def _empire_month(self, day: date) -> None:
        interest = round(sum(ln["balance"] * ln["rate"] / 12 for ln in self._loans().values()), 2)
        if interest:
            self.state["cash"] = round(self.state["cash"] - interest, 2)
            self._note(f"Paid ${interest:,.0f} loan interest.")
        fares = sum(self._transit_income(k) for k in self.state.get("transit", {}))
        if fares:
            self._earn(fares)
            self._note(f"Transit brought in ${fares:,.0f}.")
        self._sell_cars()
        if day.month == 1:
            growth = max(-0.08, min(0.15, self.rng.gauss(0.045, 0.035)))
            self.state["market_index"] = round(self.state.get("market_index", 1.0) * (1 + growth), 4)
            self._call("info", "Secretary", f"Atlanta property prices {'rose' if growth >= 0 else 'fell'} "
                       f"{abs(growth):.1%} this year.")

    def _empire_day(self, day: date) -> None:
        for key, mine in self.state.get("transit", {}).items():
            b = mine.get("building")
            if b and day >= date.fromisoformat(b["until"]):
                mine["upgrades"].append(b["upgrade"])
                mine["building"] = None
                self._call("info", "Your transit chief", f"The {b['upgrade']} upgrade on {TRANSIT[key]['name']} "
                           f"is open. It now brings in ${self._transit_income(key):,.0f} a month.")
