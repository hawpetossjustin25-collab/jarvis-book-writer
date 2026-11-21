"""Tax-lien and tax-sale listings, with prices, from the county offices that publish them.

Atlanta: DeKalb County Tax Commissioner's tax-sale list (exact tax due) and the Fulton County Sheriff's tax/levy
sale list (a scanned PDF read by Gemini; prices estimated from the county's assessed values, because the list
has none). Phoenix: Maricopa County Treasurer's unsold (state-held) liens and delinquent parcels from its public
map service, joined with the Assessor's values to estimate each lien's price.
"""
from __future__ import annotations

import base64
import html
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Callable, Optional
from urllib.parse import urljoin

from jarvis_books.computer.web import Web
from jarvis_books.errors import NotFound

log = logging.getLogger(__name__)

DEKALB_LIST = "https://publicaccess.dekalbtaxga.gov/forms/htmlframe.aspx?mode=content/search/tax_sale_listing.html"
FULTON_PAGE = "https://fultoncountyga.gov/Inside-Fulton-County/Fulton-County-Departments/Sheriff/Tax-Sales"
FULTON_PARCELS = ("https://services1.arcgis.com/AQDHTHDrZzfsFsB5/arcgis/rest/services/Tyler_TaxParcels/"
                  "FeatureServer/0/query")
MARICOPA_LIENS = "https://services.arcgis.com/ykpntM6e3tHvzKRJ/arcgis/rest/services/ParcelLienDelinquent/FeatureServer"
MARICOPA_PARCELS = ("https://services.arcgis.com/ykpntM6e3tHvzKRJ/arcgis/rest/services/Parcel_Data_View/"
                    "FeatureServer/0/query")
MARICOPA_LAYERS = {"unsold": 1, "delinquent": 2}

FULTON_MILLAGE = 0.040  # a typical Fulton/Atlanta total rate per $1 of assessed (40%) value
MARICOPA_RATE = 0.115   # a typical Maricopa total rate per $1 of assessed limited value (about $11.50 per $100)
CACHE_S = 6 * 3600
BATCH = 100
MAX_ROWS = 2000

AREAS = {
    "atlanta": ("fulton", "dekalb"), "georgia": ("fulton", "dekalb"), "ga": ("fulton", "dekalb"),
    "fulton": ("fulton",), "dekalb": ("dekalb",), "decatur": ("dekalb",),
    "phoenix": ("maricopa",), "arizona": ("maricopa",), "az": ("maricopa",), "maricopa": ("maricopa",),
}
HOW_TO_BUY = {
    "dekalb": "DeKalb sells at the Sheriff's tax sale on the first Tuesday of the month. Register first at "
              "https://www.gtsweb.com/contact-4. The price shown is the tax due; the opening bid adds costs.",
    "fulton": "Fulton sells at the Sheriff's tax/levy sale on the first Tuesday of the month, paid by cashier's "
              "check for the exact bid. The list changes until sale day. Prices are estimated yearly taxes from "
              "the county's assessed value; the exact amount due is printed in the South Fulton Daily Neighbor "
              "ads and at https://fultoncountytaxes.org.",
    "maricopa": "Unsold liens are held by the state and bought from the Treasurer by assignment; the yearly "
                "auction runs on https://maricopa.arizonataxsale.com. Prices are estimated from the Assessor's "
                "values; the exact payoff is on each parcel's Treasurer link.",
}
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
FULTON_PROMPT = (
    "This is a county sheriff's tax sale list. Return every property row in it as JSON only: a list of objects "
    "with keys sale_number, parcel and address, copied exactly as printed (keep the parcel's dashes and "
    "spaces). Include rows from every table and page. No other text.")


@dataclass
class Listing:
    county: str
    parcel: str
    address: str
    price: Optional[float]
    price_note: str
    city: str = ""
    owner: str = ""
    sale_date: str = ""
    status: str = ""
    value: Optional[float] = None
    property_type: str = ""
    link: str = ""
    extra: dict = field(default_factory=dict)


def money(text: str) -> Optional[float]:
    m = re.search(r"-?[\d,]+(?:\.\d+)?", (text or "").replace("$", ""))
    return float(m.group(0).replace(",", "")) if m else None


def fulton_parcel(printed: str) -> str:
    """'14 -0176-0018-055-3' -> '14 017600180553' and '14 -0008- LL-023-6' -> '14 0008  LL0236' (county format)."""
    parts = [p.strip() for p in (printed or "").split("-")]
    if len(parts) != 5 or not all(parts):
        return re.sub(r"\s+", " ", (printed or "").strip())
    return parts[0].ljust(3) + parts[1] + parts[2].rjust(4) + parts[3] + parts[4]


def _cells(row: str) -> list[str]:
    return [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S | re.I)]


def parse_dekalb(markup: str) -> list[Listing]:
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", markup, flags=re.S | re.I)
    head, out = None, []
    for row in rows:
        cells = _cells(row)
        if "Parcel ID" in cells and "Total Tax Due" in cells:
            head = cells
            continue
        if not head or len(cells) != len(head):
            continue
        r = dict(zip(head, cells))
        out.append(Listing(
            county="DeKalb County, GA", parcel=r.get("Parcel ID", ""), address=r.get("Address", ""),
            price=money(r.get("Total Tax Due", "")), price_note="exact tax due (opening bid adds costs)",
            owner=r.get("Owner", ""), sale_date=r.get("Tax Sale Date", ""), status="for sale",
            link=DEKALB_LIST, extra={"tax_sale_id": r.get("Tax Sale ID", ""),
                                     "years": f"{r.get('Min Year', '')}-{r.get('Max Year', '')}"}))
    if not out and "unavailable" in markup.lower():
        raise NotFound("DeKalb's tax sale list is down for maintenance; try again later")
    return out


def latest_fulton_list(markup: str, today: Optional[date] = None) -> tuple[str, str]:
    """The sale list PDF for the next sale (or the most recent one if none is coming up): (url, sale date)."""
    today = today or date.today()
    found = []
    for href in set(re.findall(r'href="([^"]*/Tax-Sales/\d{4}/[^"]+\.pdf)"', markup, flags=re.I)):
        m = re.search(r"(" + "|".join(MONTHS) + r")-(\d{1,2})-(\d{4})", href, flags=re.I)
        if m:
            when = date(int(m.group(3)), MONTHS.index(m.group(1).lower()) + 1, int(m.group(2)))
            found.append((when, href))
    if not found:
        raise NotFound("the Fulton Sheriff's page has no sale list right now")
    coming = sorted(f for f in found if f[0] >= today)
    when, href = coming[0] if coming else max(found)
    return urljoin(FULTON_PAGE, html.unescape(href)), when.isoformat()


def parse_json_rows(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text or "", flags=re.S)
    if not m:
        raise ValueError("Gemini didn't return the list")
    rows = json.loads(m.group(0))
    return [r for r in rows if isinstance(r, dict) and r.get("parcel")]


class TaxLiens:
    def __init__(self, web: Web, read_pdf: Optional[Callable[[bytes], list[dict]]] = None,
                 clock: Callable[[], float] = time.time, today: Callable[[], date] = date.today):
        self.web = web
        self.read_pdf = read_pdf
        self.clock = clock
        self.today = today
        self._cache: dict[str, tuple[float, list[Listing]]] = {}

    # ---- counties -------------------------------------------------------------------------
    def dekalb(self) -> list[Listing]:
        return parse_dekalb(self.web._get(DEKALB_LIST).text)

    def fulton(self) -> list[Listing]:
        if self.read_pdf is None:
            raise NotFound("reading Fulton's scanned sale list needs Gemini, which isn't set up")
        url, when = latest_fulton_list(self.web._get(FULTON_PAGE).text, self.today())
        rows = self.read_pdf(self.web._get(url).content)
        parcels = {fulton_parcel(r["parcel"]): r for r in rows}
        facts = self._arcgis(FULTON_PARCELS, "ParcelID", list(parcels),
                             "ParcelID,Address,Owner,TotAssess,TotAppr,LUCode,LandAcres,TaxYear")
        out = []
        for pid, r in parcels.items():
            f = facts.get(pid, {})
            assessed = f.get("TotAssess")
            out.append(Listing(
                county="Fulton County, GA", parcel=r["parcel"], address=r.get("address") or f.get("Address") or "",
                price=round(assessed * FULTON_MILLAGE, 2) if assessed else None,
                price_note="estimated yearly tax (assessed value x typical rate); the list has no prices",
                owner=(f.get("Owner") or "").strip(), sale_date=when, status="for sale",
                value=f.get("TotAppr"), link=url,
                extra={"sale_number": r.get("sale_number", ""), "acres": f.get("LandAcres")}))
        return out

    def maricopa(self, kind: str = "unsold") -> list[Listing]:
        layer = MARICOPA_LAYERS.get(kind)
        if layer is None:
            raise ValueError("kind must be unsold or delinquent")
        liens, offset = [], 0
        while len(liens) < MAX_ROWS:
            body = self._json(f"{MARICOPA_LIENS}/{layer}/query", {
                "where": "1=1", "outFields": "APN,PropertyFullStreetAddress,TreasurerWebLink",
                "returnGeometry": "false", "resultOffset": offset, "resultRecordCount": 1000,
                "orderByFields": "OBJECTID", "f": "json"})
            batch = [f["attributes"] for f in body.get("features", [])]
            liens += batch
            if not batch or not body.get("exceededTransferLimit"):
                break
            offset += len(batch)
        facts = self._arcgis(MARICOPA_PARCELS, "APN", [a["APN"] for a in liens if a.get("APN")],
                             "APN,PropertyCity,OwnerName,FullCashValue,AssessedLimitedPropertyValue,"
                             "PropertyUseDescription,LotSize_Acre")
        out = []
        for a in liens:
            f = facts.get(a.get("APN"), {})
            assessed = f.get("AssessedLimitedPropertyValue")
            out.append(Listing(
                county="Maricopa County, AZ", parcel=a.get("APN") or "", address=a.get("PropertyFullStreetAddress") or "",
                price=round(assessed * MARICOPA_RATE, 2) if assessed else None,
                price_note="estimated yearly tax (assessed value x typical rate); exact payoff on the link",
                city=f.get("PropertyCity") or "", owner=f.get("OwnerName") or "",
                status="unsold lien (state-held)" if kind == "unsold" else "delinquent taxes",
                value=f.get("FullCashValue"), property_type=f.get("PropertyUseDescription") or "",
                link=a.get("TreasurerWebLink") or "", extra={"acres": f.get("LotSize_Acre")}))
        return out

    # ---- helpers ----------------------------------------------------------------------------
    def _json(self, url: str, params: dict) -> dict:
        body = self.web._get(url, params=params).json()
        if body.get("error"):
            raise NotFound(f"the county map service answered: {body['error'].get('message', 'error')}")
        return body

    def _arcgis(self, url: str, key: str, ids: list[str], fields: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            where = f"{key} IN (" + ",".join("'" + x.replace("'", "''") + "'" for x in chunk) + ")"
            body = self._json(url, {"where": where, "outFields": fields, "returnGeometry": "false", "f": "json"})
            for f in body.get("features", []):
                out[f["attributes"].get(key)] = f["attributes"]
        return out

    def county(self, name: str, kind: str = "unsold") -> list[Listing]:
        key = f"{name}:{kind}" if name == "maricopa" else name
        hit = self._cache.get(key)
        if hit and self.clock() - hit[0] < CACHE_S:
            return hit[1]
        rows = self.maricopa(kind) if name == "maricopa" else getattr(self, name)()
        self._cache[key] = (self.clock(), rows)
        return rows

    # ---- what Jarvis calls ------------------------------------------------------------------
    def listings(self, area: str, max_price: Optional[float] = None, min_price: Optional[float] = None,
                 city: str = "", kind: str = "unsold", limit: int = 25, sort: str = "price") -> dict:
        counties = AREAS.get((area or "").strip().lower())
        if not counties:
            raise ValueError(f"I have tax lien lists for Atlanta (Fulton, DeKalb) and Phoenix (Maricopa), "
                             f"not '{area}'")
        rows, problems, counts = [], {}, {}
        for name in counties:
            try:
                got = self.county(name, kind)
            except (NotFound, ValueError) as exc:
                problems[name] = str(exc)
                continue
            counts[name] = len(got)
            rows += got
        if city:
            rows = [r for r in rows if city.lower() in (r.city or r.address).lower()]
        priced = [r for r in rows if r.price is not None]
        if min_price is not None:
            priced = [r for r in priced if r.price >= min_price]
        if max_price is not None:
            priced = [r for r in priced if r.price <= max_price]
        if min_price is None and max_price is None:
            priced += [r for r in rows if r.price is None]
        if sort == "value":
            priced.sort(key=lambda r: -(r.value or 0))
        else:
            priced.sort(key=lambda r: (r.price is None, r.price or 0))
        limit = max(1, min(int(limit or 25), 200))
        out = {"area": area, "matching": len(priced), "listed_by_county": counts,
               "listings": [asdict(r) for r in priced[:limit]],
               "how_to_buy": {n: HOW_TO_BUY[n] for n in counties}}
        if problems:
            out["problems"] = problems
        if len(priced) > limit:
            out["more"] = f"{len(priced) - limit} more; ask with a price range, a city or a bigger limit"
        return out


def gemini_pdf_reader(vision: Callable[[str, list], str]) -> Callable[[bytes], list[dict]]:
    """Read a (possibly scanned) PDF list with Gemini, which reads PDFs directly."""
    def read(pdf: bytes) -> list[dict]:
        part = {"inlineData": {"mimeType": "application/pdf", "data": base64.b64encode(pdf).decode()}}
        return parse_json_rows(vision(FULTON_PROMPT, [part, {"text": "Return the rows as JSON."}]))
    return read
