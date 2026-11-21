"""The nationwide deal scan: scoring, filtering, and combining built-in counties with online auctions."""
from __future__ import annotations

from jarvis_books.computer.deal_scan import DealScan, score
from jarvis_books.computer.taxliens import Listing, NotFound


def row(**kw):
    base = {"kind": "house", "price": 1000.0, "price_note": "exact tax due", "value": 200_000.0,
            "status": "for sale", "address": "12 MAIN ST", "property_type": "RESIDENTIAL"}
    return {**base, **kw}


def test_score_rewards_value_exact_price_and_houses():
    good, why = score(row())
    assert good is not None and "exact price" in why and any("200x" in w for w in why)
    estimate, _ = score(row(price_note="estimated yearly tax"))
    land, _ = score(row(kind="land"))
    assert good > estimate and good > land


def test_score_flags_risks_and_drops_unbuyable():
    risky, why = score(row(property_type="BRUSH CLEARANCE LIENS", address="VAC/AVE Q4/VIC 9TH STE"))
    assert risky < score(row())[0]
    assert any("brush" in w for w in why) and any("no street address" in w for w in why)
    cheap_guess, why = score(row(price=9.0, value=51_000.0, price_note="estimated yearly tax"))
    assert cheap_guess < score(row())[0] and any("far too low" in w for w in why)
    assert score(row(kind="other")) == (None, [])
    assert score(row(value=800.0)) == (None, [])
    assert score(row(status="delinquent since 2024 (can't be bought until it forfeits)")) == (None, [])


class FakeOnline:
    def __init__(self, fail=False):
        self.fail = fail

    def options(self, state="", county=""):
        auctions = [{"platform": "GovEase", "state": "CO", "name": "CO - Baca", "county": "Baca County",
                     "url": "u1", "sale_type": "tax lien", "starts": "", "ends": "", "status": "posted", "items": None},
                    {"platform": "GovEase", "state": "TN", "name": "TN - Obion", "county": "Obion County",
                     "url": "u2", "sale_type": "tax sale", "starts": "", "ends": "", "status": "posted", "items": None},
                    {"platform": "Zeus Auction (SRI)", "state": "IN", "name": "Fulton IN", "county": "Fulton County",
                     "url": "u3", "sale_type": "sheriff sale", "starts": "", "ends": "", "status": "", "items": 2}]
        return {"auctions": auctions, "states_with_online_sales": {"CO": {}, "TN": {}, "IN": {}},
                "problems": {"Bid4Assets": "answered 403"}}

    def govease_listings(self, a):
        if a["state"] == "TN" and self.fail:
            raise NotFound("liveauctions.govease.com answered 500")
        return [Listing(county=a["county"], parcel="P1", address="5 ELM ST", price=300.0,
                        price_note="starting bid on GovEase, exact", status="online auction", kind="house",
                        extra={"auction": a["name"]}),
                Listing(county=a["county"], parcel="P2", address="GARAGE 4", price=50.0, price_note="exact",
                        status="online auction", kind="other")]


class FakeLiens:
    def __init__(self, fail=False):
        self.online = FakeOnline(fail)
        self.areas = []

    def online_sales(self):
        return self.online

    def listings(self, area, max_price=None, min_price=None, property="any", sort="price", limit=25):
        self.areas.append(area)
        if area == "Phoenix":
            raise NotFound("Maricopa answered 503")
        return {"listings": [{**row(address=f"{area} house", price=4000.0), "county": area, "extra": {}}]}


def test_scan_covers_every_state_and_builtins_and_shortlists_the_best():
    liens = FakeLiens(fail=True)
    out = DealScan(liens, workers=2).scan(max_price=5000)
    assert sorted(liens.areas) == ["Atlanta", "Minneapolis", "Phoenix"]
    assert {b["state"] for b in out["shortlist"]} == {"GA", "MN", "CO"}
    assert all(b["kind"] != "other" for b in out["shortlist"])
    assert out["shortlist"] == sorted(out["shortlist"], key=lambda b: -b["score"])
    assert set(out["problems"]) == {"Bid4Assets", "Phoenix", "TN - Obion"}
    assert out["states_with_online_auctions"] == ["CO", "IN", "TN"] and "go_over_it" in out
    assert out["shortlist"][0]["link"] is not None and "why" in out["shortlist"][0]


def test_scan_one_state_and_filters():
    liens = FakeLiens()
    out = DealScan(liens, workers=2).scan(states="Tennessee", property="house", max_price=1000)
    assert liens.areas == [] and [(b["state"], b["price"]) for b in out["shortlist"]] == [("TN", 300.0)]
    out = DealScan(FakeLiens(), workers=2).scan(states="GA, Colorado", limit=1)
    assert len(out["shortlist"]) == 1 and out["candidates"] == 2
