"""Liens on the title and personal loans. A tax deed can come with the old owner's security deeds (Georgia's
mortgages), judgment liens and City of Atlanta liens. The tax deed foreclosure wipes out the liens of holders who
were served the notice, but the title stays clouded until a quiet title decree, and no lender loans on it before
that. Personal loans then fund more tax sale buys. Mixed into Game; uses its state, dice and helpers."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from jarvis_books.errors import NotFound, ValidationError

LENDERS = ("Truist Bank", "Wells Fargo Home Mortgage", "Rocket Mortgage", "Delta Community Credit Union",
           "Synovus Bank", "PNC Bank")
CREDITORS = ("Midland Credit Management", "Capital One", "Grady Memorial Hospital", "a roofing contractor")
QUIET_TITLE_COST = 4500.0  # attorney, filing, special master and publication (game estimate)
QUIET_TITLE_DAYS = 90
RECORDING_FEE = 25.0  # Georgia's flat fee to record each release or decree with the Clerk of Superior Court
WIPED_BY_NOTICE = ("security deed", "second security deed", "judgment lien")
PERSONAL_RATE = 0.12  # yearly (game value)
PERSONAL_MONTHS = 60
PERSONAL_MAX = 100_000.0
PERSONAL_SHARE = 0.30  # of the equity in properties with a clear title


def monthly_payment(amount: float, rate: float = PERSONAL_RATE, months: int = PERSONAL_MONTHS) -> float:
    r = rate / 12
    return round(amount * r / (1 - (1 + r) ** -months), 2)


class Title:
    state: dict[str, Any]

    # ---- liens --------------------------------------------------------------------------------------
    def _make_liens(self, worth: float) -> list[dict]:
        r, liens = self.rng, []
        if r.random() < 0.55:
            liens.append({"kind": "security deed", "holder": r.choice(LENDERS),
                          "amount": round(worth * r.uniform(0.3, 0.7), -2)})
            if r.random() < 0.25:
                liens.append({"kind": "second security deed", "holder": r.choice(LENDERS),
                              "amount": round(worth * r.uniform(0.05, 0.15), -2)})
        if r.random() < 0.2:
            liens.append({"kind": "judgment lien", "holder": r.choice(CREDITORS),
                          "amount": round(r.uniform(1500, 12000), -2)})
        if r.random() < 0.15:
            liens.append({"kind": "city lien", "holder": "City of Atlanta (code enforcement and water)",
                          "amount": round(r.uniform(400, 4500), -1)})
        return liens

    def _cloud_title(self, prop: dict) -> None:
        """The tax deed foreclosure is done: served lien holders are wiped out, but the record still shows them."""
        for ln in prop.get("liens", []):
            ln["status"] = ("wiped out by the foreclosure notice, still on the record"
                            if ln["kind"] in WIPED_BY_NOTICE else "still owed")
        prop["title"] = {"status": "clouded"}

    @staticmethod
    def title_clear(p: dict) -> bool:
        return (p.get("title") or {}).get("status", "clear") == "clear"

    def _surviving(self, p: dict) -> float:
        return round(sum(ln["amount"] for ln in p.get("liens", []) if ln.get("status") == "still owed"), 2)

    def title(self, property_id: str) -> dict:
        with self._lock:
            where, p = self._find(property_id)
            t = p.get("title") or {"status": "clear"}
            out = {"address": p["address"], "title": t["status"], "liens": p.get("liens", [])}
            if where == "deeds":
                out["note"] = ("still a tax deed: the liens stay until the foreclosure notice deadline passes; "
                               "the notice goes to every lien holder too, and any of them may redeem")
            elif t["status"] == "clouded":
                owed = self._surviving(p)
                out.update(to_clear_it={"quiet_title_action": QUIET_TITLE_COST, "liens_to_pay_off": owed,
                                        "recording_fees": RECORDING_FEE * (1 + len(p.get("liens", []))),
                                        "days": QUIET_TITLE_DAYS},
                           note="no bank lends on it and no buyer can get title insurance until it's cleared")
            elif t["status"] == "in court":
                out["decree_expected"] = t["until"]
            return out

    def clear_title(self, property_id: str) -> dict:
        with self._lock:
            where, p = self._find(property_id)
            if where != "properties":
                raise ValidationError("clear the title after the tax deed foreclosure makes the property yours"
                                      + self._own_hint())
            t = p.get("title") or {"status": "clear"}
            if t["status"] == "clear":
                return {"already_clear": True, "address": p["address"]}
            if t["status"] == "in court":
                raise ValidationError(f"the quiet title case is already in court; the decree is expected {t['until']}")
            owed = self._surviving(p)
            fees = RECORDING_FEE * (1 + len(p.get("liens", [])))
            self._spend(QUIET_TITLE_COST + owed + fees, f"clearing the title on {p['address']}")
            p["paid"] = round(p.get("paid", 0) + QUIET_TITLE_COST + owed + fees, 2)
            for ln in p.get("liens", []):
                if ln.get("status") == "still owed":
                    ln["status"] = "paid off, release recorded"
            until = (self.today + timedelta(days=QUIET_TITLE_DAYS)).isoformat()
            p["title"] = {"status": "in court", "until": until}
            self._note(f"Filed the quiet title case on {p['address']} and paid off {owed:,.0f} of liens.")
            self._did("title", f"started clearing the title on {p['address']}")
            self.save()
            return {"filed": True, "liens_paid_off": owed, "quiet_title_cost": QUIET_TITLE_COST,
                    "recording_fees": fees, "decree_expected": until, "cash": self.state["cash"]}

    def _title_day(self, day: date) -> None:
        for pid, p in self.state["properties"].items():
            t = p.get("title") or {}
            if t.get("status") == "in court" and day >= date.fromisoformat(t["until"]):
                p["title"] = {"status": "clear", "decree": day.isoformat()}
                for ln in p.get("liens", []):
                    ln["status"] = "removed by the quiet title decree"
                self._call("info", "Your attorney", f"The judge signed the quiet title decree on {p['address']}. "
                           "The title is clear and insurable: you can borrow on it or sell it.", property_id=pid)

    # ---- personal loans -----------------------------------------------------------------------------
    def _personal(self) -> dict:
        return self.state.setdefault("personal_loans", {})

    def personal_owed(self) -> float:
        return round(sum(ln["balance"] for ln in self._personal().values()), 2)

    def personal_limit(self) -> float:
        equity = sum(max(0.0, self.value(p) - self.loan_total(pid))
                     for pid, p in self.state["properties"].items() if self.title_clear(p))
        return max(0.0, round(min(PERSONAL_MAX, equity * PERSONAL_SHARE) - self.personal_owed(), -2))

    def take_personal_loan(self, amount: float, for_bidder: bool = False) -> dict:
        with self._lock:
            room = self.personal_limit()
            if room <= 0:
                raise ValidationError("the lender wants to see a property with a clear title and equity in it "
                                      "first (clear the title on a tax deed property, or own one outright)")
            if amount <= 0 or amount > room:
                raise ValidationError(f"the lender will approve up to {room:,.0f} more "
                                      f"({PERSONAL_SHARE:.0%} of your clear-title equity, at most "
                                      f"{PERSONAL_MAX:,.0f} in all)")
            if for_bidder and not self.state.get("bidder"):
                raise ValidationError("hire the bidding assistant first, then the loan can go to its budget")
            lid = self._id("Q")
            pay = monthly_payment(amount)
            self._personal()[lid] = {"balance": round(amount, 2), "rate": PERSONAL_RATE, "payment": pay,
                                     "months_left": PERSONAL_MONTHS, "since": self.state["today"]}
            self._earn(amount)
            if for_bidder:
                self.state["bidder"]["budget_left"] = round(self.state["bidder"]["budget_left"] + amount, 2)
            self._note(f"Took a ${amount:,.0f} personal loan.")
            self._did("personal_loan", f"took a ${amount:,.0f} personal loan")
            self.save()
            out = {"loan_id": lid, "borrowed": amount, "yearly_rate": PERSONAL_RATE, "monthly_payment": pay,
                   "months": PERSONAL_MONTHS, "cash": self.state["cash"],
                   "next_tax_sale": self.state["sale"]["date"]}
            if for_bidder:
                out["bidder_budget"] = self.state["bidder"]["budget_left"]
            return out

    def _personal_month(self) -> None:
        paid = 0.0
        for lid, ln in list(self._personal().items()):
            interest = ln["balance"] * ln["rate"] / 12
            pay = min(ln["payment"], round(ln["balance"] + interest, 2))
            ln["balance"] = round(ln["balance"] + interest - pay, 2)
            ln["months_left"] -= 1
            paid += pay
            if ln["balance"] <= 0.005 or ln["months_left"] <= 0:
                del self._personal()[lid]
        if paid:
            self.state["cash"] = round(self.state["cash"] - paid, 2)
            self._note(f"Paid ${paid:,.0f} on personal loans.")
