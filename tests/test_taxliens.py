"""Tax lien listings for Atlanta (DeKalb, Fulton) and Phoenix (Maricopa), parsed from the counties' real formats."""
from __future__ import annotations

import json
from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest

from jarvis_books.assistant.permissions import READ, level_of
from jarvis_books.computer.taxliens import (
    DEKALB_LIST, FULTON_MILLAGE, FULTON_PAGE, MARICOPA_RATE, TaxLiens, fulton_parcel, gemini_pdf_reader,
    latest_fulton_list, parse_dekalb, parse_json_rows)
from jarvis_books.errors import NotFound

from tests.test_computer import Resp, web

DEKALB = """<html><body><table border='1' summary='Script output'>
<tr><th scope="col">Tax Sale Date</th><th scope="col">Parcel ID</th><th scope="col">Map Ref</th>
<th scope="col">Tax Sale ID</th><th scope="col">Owner</th><th scope="col">Address</th><th scope="col">Tenant</th>
<th scope="col">Defendant</th><th scope="col">Levy Type</th><th scope="col">Lien Book</th><th scope="col">Page</th>
<th scope="col">Levy Date</th><th scope="col">Min Year</th><th scope="col">Max Year</th>
<th scope="col">Total Tax Due</th></tr>
<tr><td>
01-DEC-2026
</td><td>15 055 01 006</td><td>15 055 01 006</td><td>26-R30267229-DEC</td><td>NEW BEGINNING INVESTMENT PC</td>
<td>2640 RIVER RD</td><td>X</td><td>X</td><td>DEK</td><td>2932</td><td>0057</td><td>09-OCT-26</td>
<td align="right">      2025</td><td align="right">      2025</td><td>$4,809.01</td></tr>
<tr><td>01-DEC-2026</td><td>15 102 02 018</td><td>15 102 02 018</td><td>26-R30268314-DEC</td><td>SMITH BENJAMIN</td>
<td>2816 COLUMBIA DR</td><td>X</td><td>X</td><td>DEK</td><td>2932</td><td>0524</td><td>09-OCT-26</td>
<td>2024</td><td>2025</td><td>$2,508.96</td></tr>
<tr><td>01-DEC-2026</td><td>15 151 02 002</td><td>15 151 02 002</td><td>26-R30249519-DEC</td><td>RINGFIELD Z &amp; CO</td>
<td>2202 GREYSTONE WAY</td><td>X</td><td>X</td><td>DEK</td><td>2934</td><td>0368</td><td>09-OCT-26</td>
<td>2024</td><td>2025</td><td>$10,274.69</td></tr>
</table></body></html>"""

FULTON = """<p><a href="/-/media/Departments/Sheriff/Tax-Sales/2026/Sheriffs-December-1-2026-Tax--Levy-Sale-List--1st-Posting-1162026.pdf">Sheriff&rsquo;s December 1, 2026, Tax / Levy Sale.</a></p>
<a href="/-/media/Departments/Sheriff/Tax-Sales/2026/FCSO-November-3-2026-Sale-List--2nd-Posting.pdf">Nov</a>
<a href="/-/media/Departments/Sheriff/Tax-Sales/2025/FCSO-AUGUST-5-2025-2nd-Website-Posting-812025.pdf">old</a>
<a href="/-/media/Departments/Sheriff/Tax-Sales/Tax-Sales-Excess-Funds-Application-EDITED.pdf">form</a>"""


def features(rows, more=False):
    body = {"features": [{"attributes": r} for r in rows]}
    if more:
        body["exceededTransferLimit"] = True
    return Resp(body=json.dumps(body).encode())


def arcgis_join(rows_by_key, key):
    """A fake ArcGIS query endpoint answering `key IN ('a','b')` from a dict."""
    def answer(params):
        where = params["where"]
        wanted = [x.strip(" '") for x in where[where.index("(") + 1:-1].split(",")]
        return features([rows_by_key[w] for w in wanted if w in rows_by_key])
    return answer


class Routes:
    """Session whose GET answers by URL piece, passing query params to callables."""
    def __init__(self, routes):
        self.routes, self.headers, self.seen = routes, {}, []

    def get(self, url, params=None, **kw):
        self.seen.append((url, params))
        for piece, resp in self.routes.items():
            if piece in url:
                return resp(params or {}) if callable(resp) else resp
        return Resp(404)


def liens(routes, read_pdf=None, today=date(2026, 11, 21), clock=lambda: 1000.0):
    from jarvis_books.computer.web import Web
    w = Web(Routes(routes), check_address=lambda u: u)
    return TaxLiens(w, read_pdf, clock=clock, today=lambda: today), w.session


# ---- parsing the counties' formats ----------------------------------------------------------
def test_dekalb_rows_come_with_their_exact_tax_due():
    rows = parse_dekalb(DEKALB)
    assert [r.parcel for r in rows] == ["15 055 01 006", "15 102 02 018", "15 151 02 002"]
    first = rows[0]
    assert first.price == 4809.01 and first.address == "2640 RIVER RD" and first.sale_date == "01-DEC-2026"
    assert first.owner == "NEW BEGINNING INVESTMENT PC" and first.extra["years"] == "2025-2025"
    assert rows[2].owner == "RINGFIELD Z & CO" and rows[2].price == 10274.69
    assert "exact" in first.price_note


def test_dekalb_maintenance_page_is_reported_not_returned_as_empty():
    with pytest.raises(NotFound, match="maintenance"):
        parse_dekalb("<p>The System is currently unavailable due to maintenance.</p>")


def test_fulton_parcel_numbers_are_turned_into_the_countys_format():
    assert fulton_parcel("14 -0176-0018-055-3") == "14 017600180553"
    assert fulton_parcel("14 -0008-  LL-023-6") == "14 0008  LL0236"
    assert fulton_parcel("14F-0004-0002-024-9") == "14F000400020249"
    assert fulton_parcel("09C-0100-0003-025-1") == "09C010000030251"


def test_the_next_fulton_sale_list_is_picked_or_the_latest_when_none_is_coming():
    url, when = latest_fulton_list(FULTON, date(2026, 11, 21))
    assert when == "2026-12-01" and url.endswith("Sheriffs-December-1-2026-Tax--Levy-Sale-List--1st-Posting-1162026.pdf")
    assert url.startswith("https://fultoncountyga.gov/")
    assert latest_fulton_list(FULTON, date(2026, 10, 1))[1] == "2026-11-03"
    assert latest_fulton_list(FULTON, date(2027, 3, 1))[1] == "2026-12-01"
    with pytest.raises(NotFound):
        latest_fulton_list("<p>nothing</p>")


def test_gemini_reads_the_scanned_pdf_into_rows():
    seen = {}

    def vision(system, parts):
        seen["mime"] = parts[0]["inlineData"]["mimeType"]
        return 'Here:\n```json\n[{"sale_number": "1226-57192", "parcel": "14 -0176-0018-055-3", ' \
               '"address": "605 SOUTH EUGENIA PL NW"}, {"note": "no parcel"}]\n```'
    rows = gemini_pdf_reader(vision)(b"%PDF-1.4")
    assert seen["mime"] == "application/pdf"
    assert rows == [{"sale_number": "1226-57192", "parcel": "14 -0176-0018-055-3", "address": "605 SOUTH EUGENIA PL NW"}]
    with pytest.raises(ValueError):
        parse_json_rows("sorry, I can't read that")


# ---- each county end to end ------------------------------------------------------------------
def test_fulton_listings_join_the_sale_list_with_assessed_values_for_a_price():
    pdf_rows = [{"sale_number": "1226-57192", "parcel": "14 -0176-0018-055-3", "address": "605 SOUTH EUGENIA PL NW"},
                {"sale_number": "1226-57113", "parcel": "14 -0008-  LL-023-6", "address": "1600 THOMASVILLE BLVD SE"}]
    tl, session = liens({
        FULTON_PAGE: Resp(body=FULTON.encode()),
        "Sale-List--1st-Posting": Resp(body=b"%PDF-1.4 scanned"),
        "Tyler_TaxParcels": arcgis_join({"14 017600180553": {"ParcelID": "14 017600180553", "Owner": "LEMMA LLC ",
                                                             "TotAssess": 123200, "TotAppr": 308000}}, "ParcelID"),
    }, read_pdf=lambda pdf: pdf_rows if pdf.startswith(b"%PDF") else [])
    rows = tl.fulton()
    first = next(r for r in rows if r.parcel.startswith("14 -0176"))
    assert first.price == round(123200 * FULTON_MILLAGE, 2) and first.value == 308000 and first.owner == "LEMMA LLC"
    assert first.sale_date == "2026-12-01" and first.extra["sale_number"] == "1226-57192"
    assert "estimated" in first.price_note
    second = next(r for r in rows if r.parcel.startswith("14 -0008"))
    assert second.price is None and second.address == "1600 THOMASVILLE BLVD SE"
    where = next(p for u, p in session.seen if "Tyler" in u)["where"]
    assert "'14 0008  LL0236'" in where


def test_fulton_without_gemini_says_why():
    tl, _ = liens({})
    with pytest.raises(NotFound, match="Gemini"):
        tl.fulton()


def maricopa_routes(liens_rows, facts, pages=None):
    pages = pages or [liens_rows]

    def lien_layer(params):
        i = int(params["resultOffset"]) // 1000
        return features(pages[i], more=i < len(pages) - 1)
    return {"ParcelLienDelinquent": lien_layer, "Parcel_Data_View": arcgis_join(facts, "APN")}


def test_maricopa_unsold_liens_get_city_owner_value_and_an_estimated_price():
    rows = [{"APN": "10227151", "PropertyFullStreetAddress": "3335 N 107TH AVE",
             "TreasurerWebLink": "https://treasurer.maricopa.gov/PropertyTaxInformation/?parcel=10227151"},
            {"APN": "99999999", "PropertyFullStreetAddress": None, "TreasurerWebLink": ""}]
    facts = {"10227151": {"APN": "10227151", "PropertyCity": "AVONDALE", "OwnerName": "ACME LLC",
                          "FullCashValue": 12100, "AssessedLimitedPropertyValue": 116,
                          "PropertyUseDescription": "ORPHAN PARCELS W/ RD ZONING", "LotSize_Acre": 0.2}}
    tl, session = liens(maricopa_routes(rows, facts))
    out = tl.maricopa("unsold")
    assert out[0].price == round(116 * MARICOPA_RATE, 2) and out[0].city == "AVONDALE" and out[0].value == 12100
    assert out[0].status.startswith("unsold") and out[0].link.endswith("parcel=10227151")
    assert out[1].price is None and out[1].address == ""
    assert any("/1/query" in u for u, _ in session.seen)
    with pytest.raises(ValueError):
        tl.maricopa("everything")


def test_maricopa_pages_through_the_whole_list():
    page1 = [{"APN": f"A{i}"} for i in range(1000)]
    page2 = [{"APN": "B1"}]
    tl, session = liens(maricopa_routes(None, {}, pages=[page1, page2]))
    assert len(tl.maricopa("delinquent")) == 1001
    assert any("/2/query" in u for u, _ in session.seen)
    joins = [p for u, p in session.seen if "Parcel_Data_View" in u]
    assert len(joins) == 11  # 1,001 parcels looked up 100 at a time


# ---- what Jarvis gets ---------------------------------------------------------------------------
def test_atlanta_combines_both_counties_filters_by_price_and_sorts_cheapest_first():
    tl, _ = liens({DEKALB_LIST: Resp(body=DEKALB.encode())})  # no Gemini: Fulton is reported, DeKalb still works
    out = tl.listings("Atlanta", max_price=5000)
    assert [r["price"] for r in out["listings"]] == [2508.96, 4809.01]
    assert out["matching"] == 2 and out["listed_by_county"] == {"dekalb": 3}
    assert "Gemini" in out["problems"]["fulton"] and set(out["how_to_buy"]) == {"fulton", "dekalb"}
    assert tl.listings("dekalb", min_price=5000)["listings"][0]["price"] == 10274.69
    small = tl.listings("dekalb", limit=1)
    assert len(small["listings"]) == 1 and small["more"].startswith("2 more")


def test_phoenix_filters_by_city_and_unknown_areas_are_refused():
    rows = [{"APN": "1", "PropertyFullStreetAddress": "1 A ST"}, {"APN": "2", "PropertyFullStreetAddress": "2 B ST"}]
    facts = {"1": {"APN": "1", "PropertyCity": "PHOENIX", "AssessedLimitedPropertyValue": 20000},
             "2": {"APN": "2", "PropertyCity": "MESA", "AssessedLimitedPropertyValue": 100}}
    tl, _ = liens(maricopa_routes(rows, facts))
    out = tl.listings("phoenix", city="mesa")
    assert [r["parcel"] for r in out["listings"]] == ["2"]
    assert tl.listings("phoenix", sort="value")["matching"] == 2
    with pytest.raises(ValueError, match="Atlanta"):
        tl.listings("Boise")


def test_lists_are_cached_for_a_few_hours():
    now = [1000.0]
    tl, session = liens({DEKALB_LIST: Resp(body=DEKALB.encode())}, clock=lambda: now[0])
    tl.listings("dekalb")
    tl.listings("dekalb")
    assert len(session.seen) == 1
    now[0] += 7 * 3600
    tl.listings("dekalb")
    assert len(session.seen) == 2


def test_jarvis_tool_is_free_to_read_and_returns_listings(library, tmp_path):
    from jarvis_books.assistant.permissions import PermissionGate
    from jarvis_books.assistant.tools import ToolBox
    from jarvis_books.jobs import JobManager
    from jarvis_books.computer.web import Web
    assert level_of("tax_lien_listings") == READ
    tools = ToolBox(library, writer=None, index=None, jobs=JobManager(library, workers=1), gate=PermissionGate(library))
    tools.web = Web(Routes({DEKALB_LIST: Resp(body=DEKALB.encode())}), check_address=lambda u: u)
    out = tools.call("tax_lien_listings", {"area": "dekalb", "max_price": 3000})
    assert out["listings"][0]["address"] == "2816 COLUMBIA DR"
    assert "tax_lien_listings" in {d["name"] for d in tools.select("find me tax lien properties in atlanta")}
    assert "tax_lien_listings" in {d["name"] for d in tools.declarations()}
