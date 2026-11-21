"""Tax-lien and tax-sale listings, with prices, from the county offices that publish them.

Atlanta: DeKalb County Tax Commissioner's tax-sale list (exact tax due) and the Fulton County Sheriff's tax/levy
sale list (a scanned PDF read by Gemini; prices estimated from the county's assessed values, because the list
has none). Phoenix: Maricopa County Treasurer's unsold (state-held) liens and delinquent parcels from its public
map service, joined with the Assessor's values to estimate each lien's price. Minneapolis: Hennepin County's
delinquent and tax-forfeited parcels from its property map service, with this year's unpaid tax.

Anywhere else in the US: what kind of tax sale the state holds, then the county's official sale list found on the
web (HTML tables, CSV, Excel or PDF files on county sites and the auction sites counties use).
"""
from __future__ import annotations

import base64
import csv
import html
import io
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree
import zipfile

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
DEKALB_PARCELS = ("https://services2.arcgis.com/IxVN2oUE9EYLSnPE/arcgis/rest/services/Tax_Parcels_2025/"
                  "FeatureServer/0/query")
HENNEPIN_PARCELS = "https://gis.hennepin.us/arcgis/rest/services/HennepinData/LAND_PROPERTY/MapServer/1/query"
HENNEPIN_PINS = "https://www16.co.hennepin.mn.us/pins/pidresult.jsp?pid="
CLASSES = {"R": "RESIDENTIAL", "C": "COMMERCIAL", "I": "INDUSTRIAL", "E": "EXEMPT", "A": "AGRICULTURAL",
           "V": "CONSERVATION USE", "U": "UTILITY", "H": "HISTORIC", "B": "BROWNFIELD"}

FULTON_MILLAGE = 0.040  # a typical Fulton/Atlanta total rate per $1 of assessed (40%) value
MARICOPA_RATE = 0.115   # a typical Maricopa total rate per $1 of assessed limited value (about $11.50 per $100)
CACHE_S = 6 * 3600
BATCH = 100
MAX_ROWS = 2000

AREAS = {
    "atlanta": ("fulton", "dekalb"), "georgia": ("fulton", "dekalb"), "ga": ("fulton", "dekalb"),
    "fulton": ("fulton",), "dekalb": ("dekalb",), "decatur": ("dekalb",),
    "phoenix": ("maricopa",), "maricopa": ("maricopa",),
    "minneapolis": ("hennepin",), "hennepin": ("hennepin",),
}
PROPERTY_KINDS = ("house", "land", "building", "other", "any")
HOW_TO_BUY = {
    "dekalb": "DeKalb sells at the Sheriff's tax sale on the first Tuesday of the month. Register first at "
              "https://www.gtsweb.com/contact-4. The price shown is the tax due; the opening bid adds costs.",
    "fulton": "Fulton sells at the Sheriff's tax/levy sale on the first Tuesday of the month, paid by cashier's "
              "check for the exact bid. The list changes until sale day. Prices are estimated yearly taxes from "
              "the county's assessed value; the exact amount due is printed in the South Fulton Daily Neighbor "
              "ads and at https://fultoncountytaxes.org.",
    "hennepin": "Minnesota doesn't sell tax liens. A parcel that stays delinquent (about 3 years) forfeits to the "
                "state, and Hennepin County then sells it at auction or over the counter, so only the forfeited "
                "parcels can be bought. Delinquent prices are this year's unpaid tax; earlier years add more "
                "(each parcel's link shows the full amount due).",
    "maricopa": "Unsold liens are held by the state and bought from the Treasurer by assignment; the yearly "
                "auction runs on https://maricopa.arizonataxsale.com. Prices are estimated from the Assessor's "
                "values; the exact payoff is on each parcel's Treasurer link.",
}
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
GENERIC_PROMPT = (
    "This is a county's tax sale, tax lien or delinquent tax list. Return every property row as JSON only: a list "
    "of objects with keys parcel, address, owner, amount (the dollar amount printed for that row, as printed, or "
    "empty) and amount_label (that amount's column heading). Copy exactly as printed. No other text.")
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
    kind: str = "unknown"
    link: str = ""
    extra: dict = field(default_factory=dict)


HOME_WORDS = ("RESIDEN", "SFR", "SINGLE FAMILY", "SINGLE-FAMILY", "DUPLEX", "TRIPLEX", "FOURPLEX", "CONDO",
              "TOWNHOUSE", "TOWNHOME", "MOBILE HOME", "MH ", "HOUSE", "HOME", "DWELLING", "TWO UNIT")


PART_WORDS = ("GARAGE", "ASSOCIATED", "COMMON AREA", "COM AREA", "MISCELLANEOUS", "STORAGE", "RAILROAD")  # not a whole home or lot


def kind_of(desc: str, building: Optional[float] = None) -> str:
    """house, land, building (commercial and the like) or unknown, from the county's use and building value."""
    d = (desc or "").upper()
    if any(w in d for w in PART_WORDS):
        return "other"
    if "VACANT" in d or d.startswith("LAND") or "ORPHAN" in d or re.search(r"\bLOTS?\b", d):
        return "land"
    if building is not None and building <= 0:
        return "land"
    if any(w in d for w in HOME_WORDS):
        return "house"
    return "building" if building and building > 0 or d else "unknown"


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
    """Gemini's JSON list, also when a long list was cut off part way (the rows up to the cut are kept)."""
    text = text or ""
    start = text.find("[")
    if start < 0:
        raise ValueError("Gemini didn't return the list")
    body = text[start:]
    try:
        rows = json.loads(body[:body.rfind("]") + 1])
    except ValueError:
        cut = body.rfind("}")
        try:
            rows = json.loads(body[:cut + 1] + "]") if cut > 0 else []
        except ValueError:
            raise ValueError("Gemini's list couldn't be read") from None
    return [r for r in rows if isinstance(r, dict) and (r.get("parcel") or r.get("address"))]


# ---- any county: tables in web pages, CSV, Excel and PDF lists ---------------------------------------
PARCEL_HEADS = (re.compile(r"parcel|\bpin\b|\bapn\b|tax ?id|property id|folio|map ?ref", re.I),
                re.compile(r"account|certificate", re.I))
ADDRESS_HEAD = re.compile(r"address|location|situs|street|property desc", re.I)
OWNER_HEAD = re.compile(r"owner|name", re.I)
AMOUNT_HEADS = (re.compile(r"minimum bid|opening bid|min\.? bid|starting bid|bid amount", re.I),
                re.compile(r"total due|amount due|total tax|taxes due|tax due|face|lien amount|judgment|amount|"
                           r"balance|price|total|\bdue\b", re.I))


def _column(head: list[str], pattern) -> Optional[int]:
    return next((i for i, h in enumerate(head) if pattern.search(h or "")), None)


def rows_to_listings(table: list[list[str]], county: str, source: str) -> list[Listing]:
    """A list's rows, found by its header row (parcel or address, plus an amount if it has one)."""
    for at, head in enumerate(table[:15]):
        parcel = next((c for c in (_column(head, p) for p in PARCEL_HEADS) if c is not None), None)
        address = _column(head, ADDRESS_HEAD)
        amount = next((c for c in (_column(head, p) for p in AMOUNT_HEADS) if c is not None), None)
        if (parcel is not None or address is not None) and (amount is not None or (parcel is not None and address is not None)):
            break
    else:
        return []
    owner = _column(head, OWNER_HEAD)
    owner = owner if owner not in (parcel, address) else None
    out = []
    for row in table[at + 1:]:
        cell = lambda i: (row[i].strip() if i is not None and i < len(row) and row[i] else "")
        if not (cell(parcel) or cell(address)) or cell(parcel) == head[parcel or 0]:
            continue
        text = " ".join(row)
        out.append(Listing(
            county=county, parcel=cell(parcel), address=cell(address),
            price=money(cell(amount)) if amount is not None else None,
            price_note=f"'{head[amount]}' on the county's list" if amount is not None else "the list has no prices",
            owner=cell(owner), status="on the tax sale list", link=source, kind=kind_of(text) if
            re.search(r"vacant|\blots?\b|land|residen|house|home|sfr", text, re.I) else "unknown"))
    return out


def html_tables(markup: str) -> list[list[list[str]]]:
    return [[_cells(r) for r in re.findall(r"<tr[^>]*>(.*?)</tr>", t, flags=re.S | re.I)]
            for t in re.findall(r"<table[^>]*>(.*?)</table>", markup, flags=re.S | re.I)]


def csv_rows(data: bytes) -> list[list[str]]:
    text = data.decode("utf-8-sig", errors="replace")
    return list(csv.reader(io.StringIO(text), delimiter="\t" if text.count("\t") > text.count(",") else ","))


def xlsx_rows(data: bytes) -> list[list[str]]:
    """The first sheet of an Excel (.xlsx) file, without needing Excel or a spreadsheet library."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            shared = ["".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t"))
                      for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns)]
        sheet = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
        rows = []
        for r in ElementTree.fromstring(z.read(sheet)).iter(f"{{{ns['m']}}}row"):
            row: list[str] = []
            for c in r.findall("m:c", ns):
                col = 0
                for ch in re.match(r"[A-Z]+", c.get("r", "A")).group(0):
                    col = col * 26 + ord(ch) - 64
                v = c.find("m:v", ns)
                value = "".join(t.text or "" for t in c.iter(f"{{{ns['m']}}}t")) if c.get("t") == "inlineStr" else \
                    (v.text or "" if v is not None else "")
                if c.get("t") == "s" and value.isdigit():
                    value = shared[int(value)]
                row += [""] * (col - 1 - len(row)) + [value]
            rows.append(row)
        return rows


STATES = {  # abbreviation: (name, kind of sale, how it usually works)
    "AL": ("Alabama", "tax lien", "liens sold at county auctions, usually in spring"),
    "AK": ("Alaska", "tax deed", "boroughs foreclose and sell the property"),
    "AZ": ("Arizona", "tax lien", "county liens auctioned online each February; unsold liens bought from the Treasurer"),
    "AR": ("Arkansas", "tax deed", "the state Land Commissioner auctions tax-delinquent land"),
    "CA": ("California", "tax deed", "county tax collectors auction tax-defaulted property, mostly online"),
    "CO": ("Colorado", "tax lien", "county treasurers sell lien certificates each fall"),
    "CT": ("Connecticut", "redeemable deed", "towns hold tax sales; the owner can buy back within a set time"),
    "DE": ("Delaware", "redeemable deed", "sheriff's sales with a short buy-back period"),
    "DC": ("District of Columbia", "tax lien", "the Office of Tax and Revenue holds a yearly lien sale"),
    "FL": ("Florida", "tax lien", "county tax certificates auctioned online each spring; tax deed sales later"),
    "GA": ("Georgia", "redeemable deed", "county sheriffs hold tax sales on the first Tuesday of the month"),
    "HI": ("Hawaii", "redeemable deed", "counties auction tax-delinquent property"),
    "ID": ("Idaho", "tax deed", "counties auction tax-deeded property"),
    "IL": ("Illinois", "tax lien", "counties hold yearly tax sales of delinquent taxes"),
    "IN": ("Indiana", "tax lien", "county tax sales each year, online in most counties"),
    "IA": ("Iowa", "tax lien", "county treasurers hold the annual tax sale in June"),
    "KS": ("Kansas", "tax deed", "counties foreclose and hold sheriff's tax sales"),
    "KY": ("Kentucky", "tax lien", "county clerks sell certificates of delinquency"),
    "LA": ("Louisiana", "redeemable deed", "parish tax sales, often online"),
    "ME": ("Maine", "tax deed", "towns take title by lien foreclosure and may sell it"),
    "MD": ("Maryland", "tax lien", "county tax sales, usually in May or June"),
    "MA": ("Massachusetts", "redeemable deed", "towns take tax title, then foreclose and sell"),
    "MI": ("Michigan", "tax deed", "county treasurers auction foreclosed property each summer and fall"),
    "MN": ("Minnesota", "forfeited land", "no lien sales; forfeited land is sold by the counties"),
    "MS": ("Mississippi", "tax lien", "county tax sales each August"),
    "MO": ("Missouri", "tax lien", "county collector's sales each August"),
    "MT": ("Montana", "tax lien", "county tax lien sales each July"),
    "NE": ("Nebraska", "tax lien", "county treasurer sales each March"),
    "NV": ("Nevada", "tax deed", "county treasurers auction tax-defaulted property"),
    "NH": ("New Hampshire", "tax deed", "towns take tax deeds and may sell them"),
    "NJ": ("New Jersey", "tax lien", "each town holds its own tax sale, many online"),
    "NM": ("New Mexico", "tax deed", "the state Taxation and Revenue Department auctions property"),
    "NY": ("New York", "tax deed", "counties foreclose and auction (New York City sells liens in bulk)"),
    "NC": ("North Carolina", "tax deed", "counties foreclose and hold sales"),
    "ND": ("North Dakota", "tax deed", "counties sell forfeited property each November"),
    "OH": ("Ohio", "tax deed", "sheriff's foreclosure sales; some counties also sell lien certificates"),
    "OK": ("Oklahoma", "tax lien", "county treasurers hold tax sales each October and resales in June"),
    "OR": ("Oregon", "tax deed", "counties foreclose and auction tax-foreclosed property"),
    "PA": ("Pennsylvania", "tax deed", "county tax claim bureaus hold upset and judicial sales"),
    "RI": ("Rhode Island", "redeemable deed", "towns hold tax sales with a buy-back period"),
    "SC": ("South Carolina", "redeemable deed", "county tax sales, usually in the fall"),
    "SD": ("South Dakota", "tax deed", "counties take tax deeds and sell the property"),
    "TN": ("Tennessee", "redeemable deed", "county chancery court tax sales"),
    "TX": ("Texas", "redeemable deed", "sheriff's sales on the first Tuesday of the month, at the courthouse or online"),
    "UT": ("Utah", "tax deed", "counties hold one tax sale each May or June"),
    "VT": ("Vermont", "redeemable deed", "towns hold tax sales with a one-year buy-back"),
    "VA": ("Virginia", "tax deed", "localities hold judicial tax sales"),
    "WA": ("Washington", "tax deed", "counties foreclose and auction property"),
    "WV": ("West Virginia", "tax lien", "the State Auditor sells tax liens and later deeds"),
    "WI": ("Wisconsin", "tax deed", "counties take tax deeds and sell the property"),
    "WY": ("Wyoming", "tax lien", "county treasurers sell tax certificates each year"),
}
AUCTION_HOSTS = ("realtaxlien.com", "realtaxdeed.com", "realauction.com", "arizonataxsale.com", "govease.com",
                 "bid4assets.com", "zeusauction.com", "civicsource.com", "lienhub.com", "grantstreet.com",
                 "taxsale.com", "publicsurplus.com", "mdtaxsale.com", "sri-taxsale.com")
LIST_FILE = re.compile(r"\.(pdf|csv|xlsx|txt)(\?|$)", re.I)
LIST_WORDS = re.compile(r"list|lien|delinquen|parcels|auction|certificates|forfeit|properties|advertis", re.I)
NOT_A_LIST = re.compile(r"how|avoid|primer|guide|faq|brochure|application|form|instruction|minutes|agenda|"
                        r"rules|terms|notice-of-rights|handbook|explain|spanish|espanol", re.I)


def find_state(text: str) -> Optional[str]:
    low = (text or "").lower()
    for abbr, (name, _, _) in sorted(STATES.items(), key=lambda kv: -len(kv[1][0])):
        if re.search(rf"\b{name.lower()}\b", low):
            return abbr
    m = re.search(r"(?:,|\s)\s*([A-Za-z]{2})\s*$", (text or "").strip())
    return m.group(1).upper() if m and m.group(1).upper() in STATES else None


def official(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith((".gov", ".us")) or "county" in host or "parish" in host or \
        any(host == h or host.endswith("." + h) for h in AUCTION_HOSTS)


class TaxLiens:
    def __init__(self, web: Web, read_pdf: Optional[Callable[..., list[dict]]] = None,
                 clock: Callable[[], float] = time.time, today: Callable[[], date] = date.today,
                 ocr: Optional[Callable[[bytes], list[list[str]]]] = None):
        self.web = web
        self.read_pdf = read_pdf
        self.ocr = ocr
        self.clock = clock
        self.today = today
        self._cache: dict[str, tuple[float, list[Listing]]] = {}

    # ---- counties -------------------------------------------------------------------------
    def dekalb(self) -> list[Listing]:
        rows = parse_dekalb(self.web._get(DEKALB_LIST).text)
        try:
            facts = self._arcgis(DEKALB_PARCELS, "ParcelID", [r.parcel for r in rows],
                                 "ParcelID,PROPERTY_CLASS,APPRAISED_VALUE,BLDG_VALUE")
        except NotFound as exc:  # the list still has the prices without the parcel details
            log.warning("DeKalb parcel details unavailable: %s", exc)
            facts = {}
        for r in rows:
            f = facts.get(r.parcel)
            if f:
                r.property_type = CLASSES.get((f.get("PROPERTY_CLASS") or " ")[0], f.get("PROPERTY_CLASS") or "")
                r.value, r.kind = f.get("APPRAISED_VALUE"), kind_of(r.property_type, f.get("BLDG_VALUE"))
        return rows

    def _read_fulton(self, pdf: bytes) -> tuple[list[dict], str]:
        """Gemini reads the scan best; Windows' own OCR reads it when Gemini can't (no key, out of quota)."""
        problem = "reading Fulton's scanned sale list needs Gemini or Windows' text reader"
        if self.read_pdf is not None:
            try:
                rows = self.read_pdf(pdf)
                if rows:
                    return rows, "Gemini"
            except Exception as exc:  # Gemini down or out of quota: fall back to OCR
                problem = f"Gemini couldn't read it ({exc})"
                log.warning("Fulton list via Gemini failed: %s", exc)
        if self.ocr is not None:
            from jarvis_books.computer.ocr import fulton_rows
            try:
                rows = fulton_rows(self.ocr(pdf))
            except Exception as exc:
                raise NotFound(f"{problem}; Windows' text reader failed too ({exc})") from exc
            if rows:
                return rows, "Windows text reader"
            problem += "; Windows' text reader found no rows"
        raise NotFound(problem)

    def fulton(self) -> list[Listing]:
        if self.read_pdf is None and self.ocr is None:
            raise NotFound("reading Fulton's scanned sale list needs Gemini or Windows' text reader")
        url, when = latest_fulton_list(self.web._get(FULTON_PAGE).text, self.today())
        rows, reader = self._read_fulton(self.web._get(url).content)
        parcels = {fulton_parcel(r["parcel"]): r for r in rows}
        facts = self._arcgis(FULTON_PARCELS, "ParcelID", list(parcels),
                             "ParcelID,Address,Owner,TotAssess,TotAppr,ImprAppr,ClassCode,LandAcres")
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
                property_type=CLASSES.get((f.get("ClassCode") or " ")[0], f.get("ClassCode") or ""),
                kind=kind_of(CLASSES.get((f.get("ClassCode") or " ")[0], ""), f.get("ImprAppr")) if f else "unknown",
                extra={"sale_number": r.get("sale_number", ""), "acres": f.get("LandAcres"), "read_by": reader}))
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
                             "PropertyUseDescription,LotSize_Acre,LivableArea_SqFt")
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
                kind=kind_of(f.get("PropertyUseDescription"), f.get("LivableArea_SqFt") or None) if f else "unknown",
                link=a.get("TreasurerWebLink") or "", extra={"acres": f.get("LotSize_Acre")}))
        return out

    def hennepin(self) -> list[Listing]:
        attrs, offset = [], 0
        while len(attrs) < 3 * MAX_ROWS:
            body = self._json(HENNEPIN_PARCELS, {
                "where": "EARLIEST_DELQ_YR <> '  ' OR FORFEIT_LAND_IND = 'T'", "returnGeometry": "false",
                "outFields": "PID,HOUSE_NO,STREET_NM,ZIP_CD,MUNIC_NM,OWNER_NM,MKT_VAL_TOT,TAX_TOT,NET_TAX_PD,"
                             "EARLIEST_DELQ_YR,FORFEIT_LAND_IND,PR_TYP_NM1,BLDG_MV1",
                "orderByFields": "OBJECTID", "resultOffset": offset, "resultRecordCount": MAX_ROWS, "f": "json"})
            batch = [f["attributes"] for f in body.get("features", [])]
            attrs += batch
            if not batch or not body.get("exceededTransferLimit"):
                break
            offset += len(batch)
        out = []
        for a in attrs:
            street = (a.get("STREET_NM") or "").strip()
            address = "" if "UNASSIGNED" in street else f"{a.get('HOUSE_NO') or ''} {street}".strip()
            use = (a.get("PR_TYP_NM1") or "").strip()
            forfeited = (a.get("FORFEIT_LAND_IND") or "").strip() == "T"
            unpaid = round((a.get("TAX_TOT") or 0) - (a.get("NET_TAX_PD") or 0), 2)
            since = (a.get("EARLIEST_DELQ_YR") or "").strip()
            since = f"20{since}" if len(since) == 2 else since
            out.append(Listing(
                county="Hennepin County, MN", parcel=a.get("PID") or "", address=address,
                price=None if forfeited or unpaid <= 0 else unpaid,
                price_note="forfeited land; Hennepin sets its price when it lists it for sale" if forfeited else
                f"this year's unpaid tax; delinquent since {since}, so the full amount due is higher",
                city=(a.get("MUNIC_NM") or "").strip(), owner=(a.get("OWNER_NM") or "").strip(),
                status="forfeited (the county sells it)" if forfeited else
                f"delinquent since {since} (can't be bought until it forfeits)",
                value=a.get("MKT_VAL_TOT") or None, property_type=use,
                kind=kind_of(use, None if forfeited else a.get("BLDG_MV1")), link=HENNEPIN_PINS + (a.get("PID") or "")))
        return out

    def anywhere(self, state: str, county: str = "", pages: int = 4, files: int = 3) -> dict:
        """Find a county's (or a state's) official tax sale list on the web and read its rows."""
        name, sale, how = STATES[state]
        where = f"{county} County, {name}" if county else name
        words = "tax lien sale list" if sale == "tax lien" else "tax sale list" if sale != "forfeited land" \
            else "tax forfeited land for sale list"
        seen, candidates = set(), []
        for q in (f"{where} {words}", f"{where} delinquent property tax sale list"):
            try:
                found = self.web.search(q, limit=8)["results"]
            except NotFound:
                continue
            for r in found:
                if official(r["url"]) and r["url"] not in seen:
                    seen.add(r["url"])
                    candidates.append(r["url"])
        rows, checked, problems = [], [], {}
        file_links: list[str] = []
        for url in candidates[:pages]:
            try:
                got = self._read_list(url, where)
            except (NotFound, ValueError) as exc:
                problems[url] = str(exc)
                continue
            checked.append(url)
            rows += got[0]
            file_links += [f for f in got[1] if f not in file_links]
        for url in file_links[:files]:
            try:
                rows += self._read_list(url, where)[0]
                checked.append(url)
            except (NotFound, ValueError) as exc:
                problems[url] = str(exc)
        return {"rows": rows, "sale_type": sale, "how_it_works": how, "checked": checked, "problems": problems,
                "where": where}

    def _read_list(self, url: str, where: str) -> tuple[list[Listing], list[str]]:
        resp = self.web._get(url)
        kind = (resp.headers.get("Content-Type") or "").lower()
        path = urlparse(resp.url or url).path.lower()
        if "pdf" in kind or path.endswith(".pdf"):
            if self.read_pdf is None:
                raise NotFound("reading PDF lists needs Gemini, which isn't set up")
            got = self.read_pdf(resp.content, GENERIC_PROMPT)
            return [Listing(county=where, parcel=str(r.get("parcel") or ""), address=str(r.get("address") or ""),
                            price=money(str(r.get("amount") or "")),
                            price_note=f"'{r.get('amount_label') or 'amount'}' on the county's list"
                            if r.get("amount") else "the list has no prices", owner=str(r.get("owner") or ""),
                            status="on the tax sale list", link=url) for r in got], []
        if "spreadsheetml" in kind or path.endswith(".xlsx"):
            return rows_to_listings(xlsx_rows(resp.content), where, url), []
        if "csv" in kind or path.endswith((".csv", ".txt")):
            return rows_to_listings(csv_rows(resp.content), where, url), []
        markup = resp.text
        rows = [r for t in html_tables(markup) for r in rows_to_listings(t, where, url)]
        links = []
        for href, label in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', markup, flags=re.S | re.I):
            full = urljoin(resp.url or url, html.unescape(href))
            words = urlparse(full).path.rsplit("/", 1)[-1] + " " + re.sub(r"<[^>]+>", " ", label)
            if LIST_FILE.search(full) and LIST_WORDS.search(words) and not NOT_A_LIST.search(words) \
                    and full not in links:
                links.append(full)
        return rows, links

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
    @staticmethod
    def built_in(area: str) -> Optional[tuple[str, ...]]:
        low = re.sub(r"\b(county|parish|city of)\b", " ", (area or "").lower())
        parts = [p.strip() for p in re.split(r"[,]", low) if p.strip()]
        for key in [low.strip()] + parts[:1] + [" ".join(low.split()[:1])]:
            key = " ".join(key.split())
            if key in AREAS:
                return AREAS[key]
        return None

    def listings(self, area: str, max_price: Optional[float] = None, min_price: Optional[float] = None,
                 city: str = "", kind: str = "unsold", limit: int = 25, sort: str = "price",
                 property: str = "any") -> dict:
        property = (property or "any").lower()
        if property not in PROPERTY_KINDS:
            raise ValueError("property must be house, land, building or any")
        counties = self.built_in(area)
        rows, problems, counts, extra = [], {}, {}, {}
        if counties:
            for name in counties:
                try:
                    got = self.county(name, kind)
                except (NotFound, ValueError) as exc:
                    problems[name] = str(exc)
                    continue
                counts[name] = len(got)
                rows += got
            how = {n: HOW_TO_BUY[n] for n in counties}
        else:
            state = find_state(area)
            if not state:
                raise ValueError(f"tell me the state for '{area}' (for example 'Cook County, Illinois')")
            county = re.sub(r"\b(county|parish)\b", " ", re.split(r",", area)[0], flags=re.I).strip() \
                if "," in area or not re.fullmatch(rf"\s*({STATES[state][0]}|{state})\s*", area, flags=re.I) else ""
            if county.lower() in (STATES[state][0].lower(), state.lower()):
                county = ""
            key = f"any:{state}:{county.lower()}"
            hit = self._cache.get(key)
            if hit and self.clock() - hit[0] < CACHE_S:
                found = hit[1]
            else:
                found = self.anywhere(state, county)
                self._cache[key] = (self.clock(), found)
            rows, problems = list(found["rows"]), dict(found["problems"])
            counts = {found["where"]: len(rows)}
            how = {found["where"]: f"{STATES[state][0]} holds {found['sale_type']} sales: {found['how_it_works']}. "
                                   "Rules change, so check the county's own terms before bidding."}
            extra = {"sale_type": found["sale_type"], "sources_checked": found["checked"]}
            if not rows:
                extra["next_step"] = ("No list with rows was found on the county's sites. Use research_web and "
                                      "explore_website on the county treasurer, tax collector or sheriff site, or "
                                      "the auction site it uses.")
        if property != "any":
            rows = [r for r in rows if r.kind == property]
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
               "listings": [asdict(r) for r in priced[:limit]], "how_to_buy": how, **extra}
        if problems:
            out["problems"] = problems
        if len(priced) > limit:
            out["more"] = f"{len(priced) - limit} more; ask with a price range, a city or a bigger limit"
        return out


MAX_PDF = 18 * 1024 * 1024  # Gemini takes files up to about 20 MB inline


def gemini_pdf_reader(vision: Callable[[str, list], str]) -> Callable[..., list[dict]]:
    """Read a (possibly scanned) PDF list with Gemini, which reads PDFs directly."""
    def read(pdf: bytes, prompt: str = FULTON_PROMPT) -> list[dict]:
        if len(pdf) > MAX_PDF:
            raise ValueError("that PDF list is too big for Gemini to read in one go")
        part = {"inlineData": {"mimeType": "application/pdf", "data": base64.b64encode(pdf).decode()}}
        return parse_json_rows(vision(prompt, [part, {"text": "Return the rows as JSON."}]))
    return read
