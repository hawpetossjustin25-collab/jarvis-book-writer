"""Online tax sale auctions: which states and counties sell tax liens and deeds online right now, and where.

Reads the public auction calendars of the big county auction platforms (GovEase, Bid4Assets, Zeus/SRI), so
the answer reflects the auctions actually posted today rather than a hand-kept list that goes out of date.
"""
from __future__ import annotations

import html as htmllib
import logging
import re
import time
from datetime import date
from dataclasses import asdict, dataclass
from typing import Callable, Optional
from urllib.parse import urljoin

from jarvis_books.computer.taxliens import STATES, Listing, find_state, kind_of
from jarvis_books.computer.web import NotFound, Web

log = logging.getLogger(__name__)

GOVEASE = "https://liveauctions.govease.com/"
BID4ASSETS = "https://www.bid4assets.com/county-tax-sales"
ZEUS = "https://www.zeusauction.com/methods/auctionresults.php"
ZEUS_SITE = "https://www.zeusauction.com/"
CACHE_S = 30 * 60
NAMES = {name.lower(): abbr for abbr, (name, _, _) in STATES.items()}
BLOCKED_PLATFORMS = [
    {"platform": "RealAuction (realtaxlien.com, realtaxdeed.com, arizonataxsale.com)",
     "url": "https://www.realauction.com/",
     "note": "runs many Florida, Arizona, Colorado and New Jersey county sales; its sites block automated "
             "programs, so open it in the browser to see its calendar"},
]


@dataclass
class Auction:
    platform: str
    state: str
    name: str
    url: str
    county: str = ""
    sale_type: str = ""
    starts: str = ""
    ends: str = ""
    status: str = ""
    items: Optional[int] = None


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def _county(name: str) -> str:
    m = re.search(r"([A-Z][A-Za-z.' ]+?)\s+(County|Parish|Borough)\b", name)
    if m:
        return f"{m.group(1).strip()} {m.group(2)}"
    m = re.search(r"\bCity of ([A-Z][A-Za-z.' ]+)", name)
    return f"City of {m.group(1).strip()}" if m else ""


def _state_of(text: str) -> str:
    m = re.search(r",\s*([A-Z]{2})\b", text)
    if m and m.group(1) in STATES:
        return m.group(1)
    return find_state(text) or ""


def _sale_type(text: str) -> str:
    low = text.lower()
    for words, kind in (("lien", "tax lien"), ("deed", "tax deed"), ("defaulted", "tax deed"),
                        ("tax title", "tax deed"), ("judicial", "judicial tax sale"), ("upset", "upset tax sale"),
                        ("repository", "repository (unsold) sale"), ("sheriff", "sheriff sale"),
                        ("foreclos", "foreclosure sale"), ("land sale", "land sale"),
                        ("special commissioner", "court-ordered tax sale"), ("chancery", "court-ordered tax sale")):
        if words in low:
            return kind
    return "tax sale"


GOVEASE_EXTRA = (r"\s+(?:-|\(|County|Reoffer|Re-offer|Follow-Up|Sealed|Foreclosure|Special|Mayors?|Sheriff|"
                 r"Nonprofit|Adjourned|Treasurer|Land|Chancery|Upset|Judicial|Continuation|Post|Repository|Tax|"
                 r"Delinquent|Ma\b)")
MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"), 1)}


def parse_govease(markup: str) -> list[Auction]:
    out = []
    for st, slug, ident, label in re.findall(
            r'<option[^>]*value="([a-z]{2})\|([^"|]+)\|(\d+)"[^>]*>([^<]*)</option>', markup, flags=re.I):
        name = _text(label)
        place = re.sub(r"^[A-Z]{2}\s*-\s*", "", name)
        county = _county(place) or (place if place.lower().startswith("city of") else
                                    re.split(GOVEASE_EXTRA, place)[0].strip() + " County")
        out.append(Auction("GovEase", st.upper(), name, urljoin(GOVEASE, f"{st.upper()}/{slug}/{ident}/browsestandard"),
                           county=county, sale_type=_sale_type(place), status="posted"))
    return out


def parse_govease_items(markup: str, page_url: str) -> list[dict]:
    """The parcels on a GovEase auction page (first page), with their starting bids."""
    rows = []
    for row in re.findall(r'<tr role="row" class="[^"]*"[^>]*>(.*?)</tr>', markup, flags=re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)
        if len(cells) < 10:
            continue
        link = re.search(r'href="([^"]+)"', cells[2])
        bid = re.sub(r"[^\d.]", "", _text(cells[5]))
        rows.append({"number": _text(cells[2]), "parcel": _text(cells[3]), "owner": _text(cells[4]),
                     "starting_bid": float(bid) if bid else None, "description": _text(cells[6]),
                     "address": _text(cells[7]), "auction": _text(cells[8]), "sale_type": _text(cells[9]),
                     "link": urljoin(page_url, link.group(1)) if link else page_url})
    return rows


def parse_bid4assets(markup: str, today: Optional[date] = None) -> list[Auction]:
    today = today or date.today()
    out = []
    for year, month, body in re.findall(
            r'<div class="month" data-year="(\d{4})"\s*>.*?<h3>([^<]+)</h3>(.*?)</ul>', markup, flags=re.S):
        month_no = MONTHS.get(_text(month).lower(), 12)
        past = (int(year), month_no) < (today.year, today.month)
        for href, name, when in re.findall(r'<a href="([^"]+)">([^<]+)</a>\s*<span>([^<]*)</span>', body):
            name = _text(name)
            out.append(Auction("Bid4Assets", _state_of(name), name, urljoin(BID4ASSETS, href), county=_county(name),
                               sale_type=_sale_type(name), starts=f"{_text(month)} {year}: {_text(when)}",
                               status="already held" if past else "scheduled"))
    return out


def parse_zeus(markup: str) -> list[Auction]:
    markup = re.sub(r"<!--.*?-->", " ", markup, flags=re.S)
    out = []
    for block in re.split(r'<hr[^>]*>', markup):
        title = re.search(r"<h2>(.*?)</h2>", block, flags=re.S)
        if not title:
            continue
        name = _text(title.group(1))
        kind = re.search(r"Sale Type\s*:\s*([^<]+)", block)
        items = re.search(r"Items:\s*(\d+)", block)
        start = re.search(r"Starting:\s*</b>\s*([^<]+)", block)
        end = re.search(r"Ending:\s*</b>\s*([^<]+)", block)
        sale = re.search(r'href="(auctionsale\.php\?idSale=\d+)"', block)
        status = "registration closed" if "Registration Closed" in block else \
            "registration open" if "Registration Open" in block or "Register" in _text(block) else ""
        out.append(Auction("Zeus Auction (SRI)", _state_of(name), name,
                           urljoin(ZEUS_SITE, sale.group(1)) if sale else ZEUS_SITE + "publicauctions.php",
                           county=_county(name), sale_type=_sale_type(name + " " + (kind.group(1) if kind else "")),
                           starts=_text(start.group(1)) if start else "", ends=_text(end.group(1)) if end else "",
                           status=status, items=int(items.group(1)) if items else None))
    return out


PLATFORMS: dict[str, tuple[str, Callable[[str], list[Auction]]]] = {
    "GovEase": (GOVEASE, parse_govease),
    "Bid4Assets": (BID4ASSETS, parse_bid4assets),
    "Zeus Auction (SRI)": (ZEUS, parse_zeus),
}


def state_code(text: str) -> str:
    bare = (text or "").strip().upper()
    return bare if bare in STATES else (find_state(text) or "")


class OnlineSales:
    def __init__(self, web: Web, clock: Callable[[], float] = time.time):
        self.web = web
        self.clock = clock
        self._cache: dict[str, tuple[float, object]] = {}

    def _cached(self, key: str, make: Callable[[], object]):
        hit = self._cache.get(key)
        if hit and self.clock() - hit[0] < CACHE_S:
            return hit[1]
        value = make()
        self._cache[key] = (self.clock(), value)
        return value

    def auctions(self) -> tuple[list[Auction], dict[str, str]]:
        found, problems = [], {}
        for platform, (url, parse) in PLATFORMS.items():
            try:
                found += self._cached(platform, lambda u=url, p=parse: p(self.web._get(u).text))
            except (NotFound, ValueError) as exc:
                problems[platform] = str(exc)
                log.warning("online tax sales: %s unavailable: %s", platform, exc)
        return found, problems

    def options(self, state: str = "", county: str = "") -> dict:
        """Online tax sale auctions posted now, for one state (and county) or every state."""
        abbr = state_code(state) if state else ""
        if state and not abbr:
            raise ValueError(f"I don't know the state '{state}'")
        found, problems = self.auctions()
        if abbr:
            found = [a for a in found if a.state == abbr]
        if county:
            want = re.sub(r"\b(county|parish|borough)\b", "", county, flags=re.I).strip().lower()
            found = [a for a in found if want and want in (a.county + " " + a.name).lower()]
        by_state: dict[str, dict] = {}
        for a in found:
            s = by_state.setdefault(a.state or "unknown", {"state": STATES.get(a.state, (a.state or "unknown",))[0],
                                                            "auctions": 0, "platforms": set()})
            s["auctions"] += 1
            s["platforms"].add(a.platform)
        summary = {k: {**v, "platforms": sorted(v["platforms"])} for k, v in sorted(by_state.items())}
        out = {"auctions": [asdict(a) for a in found], "states_with_online_sales": summary,
               "also_check": BLOCKED_PLATFORMS,
               "note": "Only auctions the platforms have posted right now are listed. Many counties post a few "
                       "weeks before the sale, and some sell only in person; check the county treasurer's site."}
        if abbr:
            name, sale, how = STATES[abbr]
            out["state"] = {"name": name, "sale_type": sale, "how_it_works": how}
            if not found:
                out["next_step"] = (f"No online {name} auctions are posted on these platforms right now. Search the "
                                    f"county treasurer or tax collector sites, or RealAuction in the browser.")
        if problems:
            out["problems"] = problems
        return out

    def govease_listings(self, auction: Auction | dict) -> list[Listing]:
        a = auction if isinstance(auction, Auction) else Auction(**auction)
        items = self._cached("items:" + a.url, lambda: parse_govease_items(self.web._get(a.url).text, a.url))
        out = []
        for r in items:
            desc = r["description"]
            out.append(Listing(
                county=a.county or a.name, parcel=r["parcel"], address=r["address"], price=r["starting_bid"],
                price_note=f"starting bid on GovEase ({r['sale_type'] or a.sale_type}), exact; bidding can go higher",
                owner="" if r["owner"].lower() == "not available" else r["owner"], status="online auction",
                property_type=desc[:80], kind=kind_of(desc), link=r["link"],
                extra={"auction": r["auction"] or a.name, "platform": "GovEase"}))
        return out

    def listings(self, state: str, county: str = "") -> tuple[list[Listing], list[dict]]:
        """Parcels with prices from the online auctions posted for a county (GovEase shows them publicly)."""
        abbr = state_code(state)
        found = self.options(abbr, county)["auctions"] if abbr else []
        rows: list[Listing] = []
        for a in found:
            if a["platform"] == "GovEase":
                try:
                    rows += self.govease_listings(a)
                except (NotFound, ValueError) as exc:
                    log.warning("GovEase listings for %s failed: %s", a["name"], exc)
        return rows, found
