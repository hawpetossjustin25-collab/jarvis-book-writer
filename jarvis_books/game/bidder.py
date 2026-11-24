"""A hired bidding assistant who goes to every sheriff's tax sale and bids for the player. Mixed into Game."""
from __future__ import annotations

from typing import Any

from jarvis_books.errors import ValidationError
from jarvis_books.game.catalog import KINDS_BY_KEY, kind_for

BIDDER_SALARY = 2500.0  # a month (game value)


class Bidder:
    state: dict[str, Any]

    def bidder(self) -> dict:
        b = self.state.get("bidder")
        if not b:
            return {"hired": False, "can_hire": self.state.get("flips", 0) >= 1, "monthly_salary": BIDDER_SALARY,
                    "needs": [] if self.state.get("flips", 0) >= 1 else ["flip your first house"]}
        return {"hired": True, **b, "kinds": [KINDS_BY_KEY[k].label for k in b["kinds"]],
                "monthly_salary": BIDDER_SALARY}

    def hire_bidder(self, budget: float, max_per_property: float, kinds: str = "house") -> dict:
        with self._lock:
            if self.state.get("flips", 0) < 1:
                raise ValidationError("you can hire a bidding assistant after you flip your first house")
            if budget <= 0 or max_per_property <= 0:
                raise ValidationError("give a total budget and the most to bid on one property")
            keys = sorted({kind_for(k.strip()).key for k in (kinds or "house").split(",") if k.strip()})
            self.state["bidder"] = {"budget_left": round(budget, 2), "max_per_property": round(max_per_property, 2),
                                    "kinds": keys, "hired": self.state["today"], "won": [], "lost": 0}
            self._note(f"Hired a bidding assistant with a ${budget:,.0f} budget.")
            self.save()
            return {**self.bidder(), "note": f"the assistant bids at every tax sale, starting "
                                             f"{self.state['sale']['date']}, for ${BIDDER_SALARY:,.0f} a month"}

    def fire_bidder(self) -> dict:
        with self._lock:
            if not self.state.get("bidder"):
                raise ValidationError("you don't have a bidding assistant")
            b = self.state.pop("bidder")
            self.save()
            return {"let_go": True, "won_for_you": len(b["won"])}

    def _bidder_at_sale(self) -> None:
        b = self.state.get("bidder")
        if not b:
            return
        won, lost = [], 0
        parcels = [p for p in self.state["sale"]["parcels"]
                   if p["status"] == "for sale" and p["property"]["kind"] in b["kinds"]]
        parcels.sort(key=lambda p: -self.value(p["property"]) / max(1.0, p["opening_bid"]))
        for parcel in parcels:
            limit = min(b["max_per_property"], b["budget_left"], self.state["cash"])
            if limit < parcel["opening_bid"]:
                continue
            rivals = self._rivals(parcel["opening_bid"])
            top = max(rivals, key=lambda r: r["max"])
            step = self.t("bid_increment")
            if top["max"] + step <= limit:
                price = round(max(parcel["opening_bid"], top["max"] + step), 2)
                out = self._award(parcel, price, by="your bidding assistant")
                b["budget_left"] = round(b["budget_left"] - price, 2)
                b["won"].append({"day": self.state["today"], "address": parcel["property"]["address"],
                                 "paid": price, "deed_id": out["deed_id"]})
                won.append(f"{parcel['property']['address']} for ${price:,.0f}")
            else:
                parcel["status"] = f"sold to {top['name']}"
                lost += 1
        b["lost"] += lost
        if won or lost:
            self._call("info", "Your bidding assistant",
                       (f"At today's tax sale I won {len(won)}: " + "; ".join(won) + "." if won else
                        "I didn't win anything at today's tax sale; the other bidders went higher.")
                       + (f" Lost {lost} to higher bidders." if won and lost else "")
                       + f" ${b['budget_left']:,.0f} of the budget is left.")
