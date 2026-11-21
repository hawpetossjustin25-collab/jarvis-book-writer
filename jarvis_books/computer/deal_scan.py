"""Nationwide tax sale deal scan: every state's online auctions plus the built-in counties, scored and shortlisted.

Each property gets a 0-100 score with the reasons behind it, so Jarvis can go over the shortlist with the user
property by property instead of handing over a raw dump.
"""
from __future__ import annotations

import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Optional

from jarvis_books.computer.online_sales import state_code
from jarvis_books.computer.taxliens import DEAL_MIN_VALUE, NotFound

log = logging.getLogger(__name__)

BUILT_IN = ("Atlanta", "Phoenix", "Minneapolis")
BUILT_IN_STATES = {"Atlanta": "GA", "Phoenix": "AZ", "Minneapolis": "MN"}
KIND_POINTS = {"house": 30, "building": 20, "land": 15, "unknown": 10}
RISK_WORDS = {
    r"brush clearance|weed abatement": "the lien is for brush or weed clearance, not the land",
    r"investigate before": "the county warns to investigate before you buy",
    r"\bstrip\b|sliver|remnant": "looks like a leftover strip",
    r"easement|right of way|\brow\b": "looks like an easement or right of way",
    r"landlock": "may be landlocked (no road access)",
    r"drainage|ditch|retention|canal": "looks like a drainage or ditch parcel",
    r"mineral|mining claim": "mineral rights only",
    r"unbuildable|non-?buildable": "listed as unbuildable",
    r"common area|\bhoa\b": "looks like an HOA common area",
    r"^vac/|\bvac/": "vacant land described by nearby streets only, no real address",
    r"mobile home|manufactured": "a mobile home, which may not include the land",
}
WORKERS = 8


def score(row: dict) -> tuple[Optional[int], list[str]]:
    """0-100 (higher is better) and why; None when it isn't a buyable property at all."""
    status, kind = row.get("status") or "", row.get("kind") or "unknown"
    price, value = row.get("price"), row.get("value")
    if "can't be bought" in status or kind == "other":
        return None, []
    if value is not None and value < DEAL_MIN_VALUE:
        return None, []
    pts, why = KIND_POINTS.get(kind, 10), [f"{kind}"]
    exact = price is not None and "estimat" not in (row.get("price_note") or "").lower()
    risks = []
    if price and price > 0 and value:
        ratio = value / price
        points = min(40, max(0, round(13 * math.log10(ratio)))) if ratio > 1 else 0
        pts += points if exact else points // 2  # an estimated price can be far from what you'd really pay
        why.append(f"county value ${value:,.0f} for ${price:,.2f} ({ratio:,.0f}x)")
        if not exact and ratio > 1000:
            risks.append("the estimated price looks far too low; get the exact payoff before counting on it")
    elif price:
        why.append("no county value to compare the price with")
    if exact:
        pts += 10
        why.append("exact price")
    elif price is not None:
        why.append("price is only an estimate")
    if price is not None and price <= 5000:
        pts += 10
    elif price is not None and price <= 20000:
        pts += 5
    text = f"{row.get('property_type') or ''} {row.get('address') or ''}".lower()
    risks += [msg for pat, msg in RISK_WORDS.items() if re.search(pat, text)]
    address = (row.get("address") or "").strip().lower()
    if not address or re.match(r"^(0|vacant|vac)\b", address):
        risks.append("no street address")
    pts -= 20 * len(risks)
    if "online" in status:
        pts += 5
        why.append("you can bid online")
    return max(0, min(100, pts)), why + [f"risk: {r}" for r in risks]


class DealScan:
    def __init__(self, liens, workers: int = WORKERS):
        self.liens = liens
        self.workers = workers

    def _built_in(self, area: str, max_price, min_price, prop) -> list[dict]:
        out = self.liens.listings(area, max_price, min_price, property=prop, sort="deal", limit=200)
        for r in out["listings"]:
            r.setdefault("state", BUILT_IN_STATES[area])
        return out["listings"]

    def _online(self, auction: dict) -> list[dict]:
        rows = []
        for r in self.liens.online_sales().govease_listings(auction):
            d = asdict(r)
            d["state"] = auction["state"]
            rows.append(d)
        return rows

    def scan(self, max_price: Optional[float] = None, min_price: Optional[float] = None, property: str = "any",
             states: str = "", limit: int = 20) -> dict:
        wanted = {state_code(s) for s in re.split(r"[,/;]| and ", states or "") if s.strip()} - {""}
        online = self.liens.online_sales()
        found = online.options()
        problems = dict(found.get("problems") or {})
        auctions = [a for a in found["auctions"] if a["platform"] == "GovEase" and (not wanted or a["state"] in wanted)]
        jobs = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for area in BUILT_IN:
                if not wanted or BUILT_IN_STATES[area] in wanted:
                    jobs[pool.submit(self._built_in, area, max_price, min_price, property)] = area
            for a in auctions:
                jobs[pool.submit(self._online, a)] = a["name"]
            rows, scanned = [], {}
            for job in as_completed(jobs):
                name = jobs[job]
                try:
                    got = job.result()
                except (NotFound, ValueError, KeyError) as exc:
                    problems[name] = str(exc)
                    continue
                except Exception as exc:  # one bad county must not sink the whole scan
                    log.warning("deal scan: %s failed", name, exc_info=True)
                    problems[name] = f"{type(exc).__name__}: {exc}"
                    continue
                scanned[name] = len(got)
                rows += got
        best = []
        for r in rows:
            price = r.get("price")
            if max_price is not None and (price is None or price > max_price):
                continue
            if min_price is not None and (price is None or price < min_price):
                continue
            if property not in ("", "any") and r.get("kind") != property:
                continue
            pts, why = score(r)
            if pts is None:
                continue
            best.append({"score": pts, "why": why, "state": r.get("state", ""), "county": r.get("county", ""),
                         "address": r.get("address", ""), "parcel": r.get("parcel", ""), "kind": r.get("kind"),
                         "price": price, "price_note": r.get("price_note", ""), "value": r.get("value"),
                         "status": r.get("status", ""), "sale_date": r.get("sale_date", ""),
                         "link": r.get("link", ""), "auction": (r.get("extra") or {}).get("auction", "")})
        best.sort(key=lambda b: (-b["score"], b["price"] if b["price"] is not None else math.inf))
        by_state: dict[str, int] = {}
        for b in best:
            by_state[b["state"]] = by_state.get(b["state"], 0) + 1
        return {
            "shortlist": best[:max(1, min(int(limit or 20), 50))],
            "candidates": len(best), "properties_scanned": len(rows), "sources_scanned": len(scanned),
            "candidates_by_state": dict(sorted(by_state.items())),
            "states_with_online_auctions": sorted(found["states_with_online_sales"]),
            "problems": problems or None,
            "covers": "every state's auctions posted on GovEase (with prices), plus Atlanta (Fulton, DeKalb), "
                      "Phoenix (Maricopa) and Minneapolis (Hennepin). Bid4Assets, Zeus and RealAuction sales are "
                      "listed by online_tax_sales but don't show prices publicly.",
            "go_over_it": "Go over the shortlist with the user one property at a time, best first: the address, "
                          "price (exact or estimate), what it's worth, why it scored well, its risks, how and when "
                          "to buy, and the link. Ask which ones they want to look into, skip or save.",
        }
