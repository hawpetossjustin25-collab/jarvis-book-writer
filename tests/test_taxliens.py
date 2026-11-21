"""Tax lien listings for Atlanta (DeKalb, Fulton) and Phoenix (Maricopa), parsed from the counties' real formats."""
from __future__ import annotations

import json
from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest

from jarvis_books.assistant.permissions import READ, level_of
from jarvis_books.computer.taxliens import (
    DEKALB_LIST, FULTON_MILLAGE, FULTON_PAGE, GENERIC_PROMPT, MARICOPA_RATE, TaxLiens, csv_rows, find_state,
    fulton_parcel, gemini_pdf_reader, kind_of, latest_fulton_list, official, parse_dekalb, parse_json_rows,
    rows_to_listings, xlsx_rows)
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
    with pytest.raises(ValueError, match="state"):
        tl.listings("Boise")


def test_lists_are_cached_for_a_few_hours():
    now = [1000.0]
    tl, session = liens({DEKALB_LIST: Resp(body=DEKALB.encode())}, clock=lambda: now[0])
    lists = lambda: sum(1 for u, _ in session.seen if u == DEKALB_LIST)
    tl.listings("dekalb")
    tl.listings("dekalb")
    assert lists() == 1
    now[0] += 7 * 3600
    tl.listings("dekalb")
    assert lists() == 2


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


# ---- house or land ---------------------------------------------------------------------------------------
def test_house_or_land_comes_from_the_countys_use_and_building_value():
    assert kind_of("RESIDENTIAL", 179500) == "house"
    assert kind_of("RESIDENTIAL", 0) == "land"
    assert kind_of("VACANT LAND-RESIDENTIAL") == "land"
    assert kind_of("ORPHAN PARCELS W/ RD ZONING") == "land"
    assert kind_of("SFR GRADE 010-3 URBAN SUBDIVIDED", 1450) == "house"
    assert kind_of("TRIPLEX") == "house"
    assert kind_of("COMMERCIAL-PREFERRED", 1128400) == "building"
    assert kind_of("", None) == "unknown"
    assert kind_of("CONDO GARAGE/MISCELLANEOUS", 0) == "other"
    assert kind_of("ASSOCIATED RESIDENTIAL PARCELS") == "other"
    assert kind_of("CONDOS/TH COM AREA PARCEL") == "other"


def test_dekalb_listings_say_house_or_land_and_can_be_filtered():
    facts = {"15 055 01 006": {"ParcelID": "15 055 01 006", "PROPERTY_CLASS": "R4", "APPRAISED_VALUE": 156700,
                               "BLDG_VALUE": 0},
             "15 102 02 018": {"ParcelID": "15 102 02 018", "PROPERTY_CLASS": "R3", "APPRAISED_VALUE": 208000,
                               "BLDG_VALUE": 179500}}
    tl, _ = liens({DEKALB_LIST: Resp(body=DEKALB.encode()), "Tax_Parcels_2025": arcgis_join(facts, "ParcelID")})
    houses = tl.listings("dekalb", property="house")["listings"]
    assert [(r["address"], r["kind"], r["value"]) for r in houses] == [("2816 COLUMBIA DR", "house", 208000)]
    land = tl.listings("dekalb", property="land")["listings"]
    assert [r["address"] for r in land] == ["2640 RIVER RD"]
    assert tl.listings("dekalb")["matching"] == 3
    with pytest.raises(ValueError):
        tl.listings("dekalb", property="castle")


# ---- Minneapolis -----------------------------------------------------------------------------------------
def test_hennepin_lists_delinquent_parcels_with_unpaid_tax_and_forfeited_land_for_sale():
    parcels = [
        {"PID": "0102824130131", "HOUSE_NO": 2530, "STREET_NM": "34TH ST E   ", "MUNIC_NM": "MINNEAPOLIS     ",
         "OWNER_NM": "T E PHILLIPS ", "MKT_VAL_TOT": 190000, "TAX_TOT": 2578.16, "NET_TAX_PD": 0.0,
         "EARLIEST_DELQ_YR": "25", "FORFEIT_LAND_IND": " ", "PR_TYP_NM1": "CONDOMINIUM", "BLDG_MV1": 150000},
        {"PID": "0102924430075", "HOUSE_NO": 3037, "STREET_NM": "ULYSSES ST N E", "MUNIC_NM": "MINNEAPOLIS",
         "OWNER_NM": "HENNEPIN FORFEITED LAND", "MKT_VAL_TOT": 0, "TAX_TOT": 0.0, "NET_TAX_PD": 0.0,
         "EARLIEST_DELQ_YR": "  ", "FORFEIT_LAND_IND": "T", "PR_TYP_NM1": "VACANT LAND-RESIDENTIAL", "BLDG_MV1": 0},
        {"PID": "0211724440002", "HOUSE_NO": 36, "STREET_NM": "ADDRESS UNASSIGNED", "MUNIC_NM": "MINNETRISTA",
         "OWNER_NM": "HENNEPIN FORFEITED LAND", "MKT_VAL_TOT": 0, "TAX_TOT": 0.0, "NET_TAX_PD": 0.0,
         "EARLIEST_DELQ_YR": "  ", "FORFEIT_LAND_IND": "T", "PR_TYP_NM1": "TRIPLEX", "BLDG_MV1": 0}]
    tl, session = liens({"LAND_PROPERTY": lambda params: features(parcels)})
    out = tl.listings("Minneapolis")
    rows = {r["parcel"]: r for r in out["listings"]}
    condo = rows["0102824130131"]
    assert condo["price"] == 2578.16 and condo["address"] == "2530 34TH ST E" and condo["kind"] == "house"
    assert "since 2025" in condo["status"] and "higher" in condo["price_note"]
    assert condo["link"].endswith("pid=0102824130131")
    lot = rows["0102924430075"]
    assert lot["price"] is None and lot["status"].startswith("forfeited") and lot["kind"] == "land"
    assert rows["0211724440002"]["address"] == "" and rows["0211724440002"]["kind"] == "house"
    assert [r["parcel"] for r in tl.listings("hennepin", city="minnetrista")["listings"]] == ["0211724440002"]
    assert "doesn't sell tax liens" in out["how_to_buy"]["hennepin"]
    where = session.seen[0][1]["where"]
    assert "EARLIEST_DELQ_YR" in where and "FORFEIT_LAND_IND" in where


# ---- any state ---------------------------------------------------------------------------------------------
def test_states_and_official_sites_are_recognized():
    assert find_state("Cook County, Illinois") == "IL"
    assert find_state("Wayne County, MI") == "MI"
    assert find_state("West Virginia") == "WV" and find_state("Virginia") == "VA"
    assert find_state("Boise") is None
    assert official("https://www.cookcountytreasurer.com/x") and official("https://tax.ohio.gov/")
    assert official("https://cuyahoga.sri-taxsale.com/list") and official("https://miamidade.realtaxdeed.com/")
    assert not official("https://www.taxliencrowd-blog.com/best-deals")
    assert TaxLiens.built_in("Maricopa County, Arizona") == ("maricopa",)
    assert TaxLiens.built_in("Fulton County GA") == ("fulton",)
    assert TaxLiens.built_in("Cook County, Illinois") is None


def test_list_rows_are_found_by_their_header_with_the_amount_column_named():
    table = [["Notice of tax sale"], ["Item", "Parcel Number", "Owner Name", "Property Address", "Minimum Bid", "Taxes Due"],
             ["1", "12-34-567", "DOE JOHN", "10 VACANT LOT RD", "$1,250.00", "$900.00"],
             ["2", "12-34-568", "ROE JANE", "12 MAIN ST", "$3,400.50", "$3,000.00"], ["", "", "", "", "", ""]]
    rows = rows_to_listings(table, "Cook County, Illinois", "https://x.gov/list")
    assert [(r.parcel, r.address, r.price, r.owner) for r in rows] == [
        ("12-34-567", "10 VACANT LOT RD", 1250.0, "DOE JOHN"), ("12-34-568", "12 MAIN ST", 3400.5, "ROE JANE")]
    assert rows[0].price_note == "'Minimum Bid' on the county's list" and rows[0].kind == "land"
    assert rows_to_listings([["Name", "Phone"], ["a", "b"]], "x", "y") == []
    assert csv_rows(b"Parcel\tAddress\tAmount Due\n1\t2 A ST\t$5\n")[1] == ["1", "2 A ST", "$5"]


def make_xlsx(rows):
    import io
    import zipfile
    strings, sheet = [], []
    for i, row in enumerate(rows, 1):
        cells = []
        for j, v in enumerate(row):
            ref = chr(65 + j) + str(i)
            if isinstance(v, (int, float)):
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            else:
                strings.append(v)
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
        sheet.append(f'<row r="{i}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(sheet)}</sheetData></worksheet>')
    return buf.getvalue()


def test_excel_lists_are_read_without_a_spreadsheet_library():
    data = make_xlsx([["PIN", "Situs Address", "Total Due"], ["0101", "5 ELM ST", 812.5]])
    assert xlsx_rows(data) == [["PIN", "Situs Address", "Total Due"], ["0101", "5 ELM ST", "812.5"]]


class FakeSearch:
    def __init__(self, results):
        self.results, self.queries = results, []

    def __call__(self, query, limit=8):
        self.queries.append(query)
        return {"query": query, "engine": "test", "results": [{"title": u, "url": u, "snippet": ""} for u in self.results]}


def test_any_county_finds_the_official_list_page_and_its_files():
    page = """<h1>Annual Tax Sale</h1><table><tr><th>Parcel</th><th>Address</th><th>Amount Due</th></tr>
    <tr><td>A-1</td><td>1 OAK ST</td><td>$2,100.00</td></tr></table>
    <a href="/files/2026-tax-sale-list.csv">2026 tax sale list (CSV)</a>
    <a href="/files/sale-list.xlsx">Excel list</a><a href="/files/sale-list.pdf">PDF list</a>
    <a href="/files/minutes.pdf">Board minutes</a>
    <a href="/pdfs/HowToAvoidTheTaxSale.pdf">How to avoid the tax sale</a>"""
    xlsx = make_xlsx([["PIN", "Situs Address", "Total Due"], ["X-9", "9 PINE RD", 5000]])
    pdf_rows = [{"parcel": "P-7", "address": "7 HOUSE LN", "amount": "$1,999.99", "amount_label": "Minimum Bid"}]
    prompts = []
    routes = {
        "treasurer.cookcountyil.gov/taxsale": Resp(body=page.encode(), headers={"Content-Type": "text/html"},
                                                     url="https://treasurer.cookcountyil.gov/taxsale"),
        "2026-tax-sale-list.csv": Resp(body=b"Parcel,Address,Amount Due\nC-3,3 BIRCH AVE,$950\n",
                                       headers={"Content-Type": "text/csv"},
                                       url="https://treasurer.cookcountyil.gov/files/2026-tax-sale-list.csv"),
        "sale-list.xlsx": Resp(body=xlsx, headers={"Content-Type": "application/octet-stream"},
                               url="https://treasurer.cookcountyil.gov/files/sale-list.xlsx"),
    }
    tl, session = liens(routes, read_pdf=lambda pdf, prompt="": prompts.append(prompt) or pdf_rows)
    tl.web.search = FakeSearch(["https://blog.example.com/tax-liens", "https://treasurer.cookcountyil.gov/taxsale"])
    out = tl.listings("Cook County, Illinois", max_price=5000)
    assert [(r["address"], r["price"]) for r in out["listings"]] == [
        ("3 BIRCH AVE", 950.0), ("1 OAK ST", 2100.0), ("9 PINE RD", 5000.0)]
    assert out["sale_type"] == "tax lien" and "Illinois holds tax lien sales" in out["how_to_buy"]["Cook County, Illinois"]
    assert "https://treasurer.cookcountyil.gov/taxsale" in out["sources_checked"]
    assert not any("blog.example.com" in u for u, _ in session.seen)
    assert not any("minutes" in u or "Avoid" in u for u, _ in session.seen)
    assert "Cook County, Illinois tax lien sale list" in tl.web.search.queries
    assert out["listings"][0]["link"].endswith("2026-tax-sale-list.csv")
    # the PDF wasn't fetched (3 files at most: CSV, Excel, then the PDF answered 404 here)
    assert "problems" in out


def test_pdf_lists_anywhere_are_read_by_gemini_with_the_general_prompt():
    prompts = []
    routes = {"county.gov/list.pdf": Resp(body=b"%PDF", headers={"Content-Type": "application/pdf"},
                                            url="https://county.gov/list.pdf")}
    rows = [{"parcel": "P-7", "address": "7 HOUSE LN", "amount": "$1,999.99", "amount_label": "Minimum Bid"}]
    tl, _ = liens(routes, read_pdf=lambda pdf, prompt="": prompts.append(prompt) or rows)
    tl.web.search = FakeSearch(["https://county.gov/list.pdf"])
    out = tl.listings("Wayne County, MI")
    assert out["listings"][0]["price"] == 1999.99 and "'Minimum Bid'" in out["listings"][0]["price_note"]
    assert prompts == [GENERIC_PROMPT] and out["sale_type"] == "tax deed"


def test_nothing_found_anywhere_says_what_to_try_next():
    tl, _ = liens({})
    tl.web.search = FakeSearch(["https://www.someblog.com/x"])
    out = tl.listings("Ohio")
    assert out["matching"] == 0 and "research_web" in out["next_step"] and out["sale_type"] == "tax deed"


def test_a_cut_off_gemini_list_keeps_the_rows_it_finished():
    text = '[{"parcel": "1", "address": "A"}, {"parcel": "2", "address": "B"}, {"parcel": "3", "addr'
    assert [r["parcel"] for r in parse_json_rows(text)] == ["1", "2"]


# ---- Fulton without Gemini ---------------------------------------------------------------------------------
def fulton_routes():
    return {FULTON_PAGE: Resp(body=FULTON.encode()), "Sale-List--1st-Posting": Resp(body=b"%PDF-1.4 scanned"),
            "Tyler_TaxParcels": arcgis_join({"14 017600180553": {"ParcelID": "14 017600180553", "TotAssess": 100000,
                                                                 "ImprAppr": 200000, "ClassCode": "R3"}}, "ParcelID")}


OCR_PAGES = [["1226-57192 14 -0176-0018-055-3 605 SOUTH EUGENIA PL NW"]]


def test_fulton_falls_back_to_windows_ocr_when_gemini_fails():
    def gemini(pdf):
        raise RuntimeError("out of quota")
    tl, _ = liens(fulton_routes(), read_pdf=gemini)
    tl.ocr = lambda pdf: OCR_PAGES
    rows = tl.fulton()
    assert [(r.address, r.price, r.kind) for r in rows] == [("605 SOUTH EUGENIA PL NW", 4000.0, "house")]
    assert rows[0].extra["read_by"] == "Windows text reader"


def test_fulton_uses_gemini_first_and_reports_when_both_readers_fail():
    gem = [{"sale_number": "1", "parcel": "14 -0176-0018-055-3", "address": "605 SOUTH EUGENIA PL NW"}]
    tl, _ = liens(fulton_routes(), read_pdf=lambda pdf: gem)
    tl.ocr = lambda pdf: pytest.fail("OCR shouldn't run when Gemini read the list")
    assert tl.fulton()[0].extra["read_by"] == "Gemini"

    def gemini(pdf):
        raise RuntimeError("out of quota")

    def bad_ocr(pdf):
        raise RuntimeError("no OCR language")
    tl, _ = liens(fulton_routes(), read_pdf=gemini)
    tl.ocr = bad_ocr
    with pytest.raises(NotFound, match="out of quota.*no OCR language"):
        tl.fulton()
