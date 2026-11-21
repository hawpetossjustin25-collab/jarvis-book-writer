"""Online tax sale auctions from the platforms' public calendars, and the deal ranking."""
from __future__ import annotations

from datetime import date

import pytest

from jarvis_books.computer import online_sales as o
from jarvis_books.computer.taxliens import Listing, rank_deals
from jarvis_books.computer.web import NotFound

GOVEASE_HTML = """
<select><option value="ca|cakernreoffer|1564">CA - Kern Reoffer</option>
<option value="co|cobaca|1235">CO - Baca</option>
<option value="tn|tncobionspeccommissioner|1379">TN - Obion (Special Commissioner's)</option>
<option value="tn|tnjackson|1300">TN - City of Jackson</option></select>
<table><tbody>
<tr role="row" class="odd nobid" style="cursor: pointer;">
 <td class="dtr-control "></td><td></td>
 <td><a href="/co/cobaca/1235/openstandardparcel/5/R001" target="_blank">17</a></td>
 <td> R001</td><td><span class="no-translate">Not Available</span></td>
 <td class="alignDollar">$1,234.50</td>
 <td style="display: none;">SINGLE FAMILY RESIDENCE</td><td style="display: none;">12 MAIN ST</td>
 <td style="display: none;"><span>Baca County 2026 Tax Lien Sale</span></td>
 <td style="display: none;"><span>Tax Lien</span></td><td>3d 2h</td><td></td></tr>
<tr role="row" class="odd nobid"><td></td><td></td><td><a href="/co/cobaca/1235/openstandardparcel/7/R003">19</a></td>
 <td>R003</td><td>Not Available</td><td class="alignDollar">$500.00</td><td>CITY-SANTA CLARI</td><td>VAC/AVE Q4/VIC 9TH STE</td>
 <td>Baca County 2026 Tax Lien Sale</td><td>Tax Lien</td><td>3d</td><td></td></tr>
<tr role="row" class="odd nobid"><td></td><td></td><td><a href="/co/cobaca/1235/openstandardparcel/8/R004">20</a></td>
 <td>R004</td><td>Not Available</td><td class="alignDollar">$700.00</td><td>BRUSH CLEARANCE LIENS</td><td>9 OAK ST UNIT 5</td>
 <td>Baca County 2026 Tax Lien Sale</td><td>Tax Lien</td><td>3d</td><td></td></tr>
<tr role="row" class="even nobid"><td></td><td></td><td><a href="/co/cobaca/1235/openstandardparcel/6/R002">18</a></td>
 <td>R002</td><td>SMITH JOHN</td><td class="alignDollar">$88.00</td><td>VACANT LAND</td><td>0 COUNTY RD 4</td>
 <td>Baca County 2026 Tax Lien Sale</td><td>Tax Lien</td><td>3d 2h</td><td></td></tr>
</tbody></table>"""

BID4ASSETS_HTML = """
<div class="month" data-year="2026" ><div class="title"><h3>May</h3></div><ul class="auction-list">
<li><a href="/storefront/DouglasNVMay26">Douglas County, NV Tax Defaulted Properties Auction</a>
<span>May 26th - 28th</span></li></ul></div>
<div class="month" data-year="2026" ><div class="title"><h3>December</h3></div><ul class="auction-list">
<li><a href="/storefront/BerksPADec26">Berks County, PA Judicial Sale</a><span>December 3rd - 4th</span></li>
</ul></div>"""

ZEUS_HTML = """<div class="row"><!-- <h2>commented out</h2> --></div><hr>
<div class="col-sm-4"><h2><i class="fa"></i> 2026 Gunnison County Colorado Tax Lien Sale</h2><h3>Sale Type : Tax Sale</h3>
Items: 148<br><a href='methods/exportsale.php?idAuctionSale=2747'>Download</a></div>
<div class="col-sm-4"><b>Starting: </b>11/17/2026 9:00 AM CST<br><b>Ending: </b>11/17/2026 6:00 PM CST</div>
<div><span><b>Registration Closed As Of: 11/16/2026</b></span><a href="auctionsale.php?idSale=2747">View Results</a></div><hr>
<div><h2>Fulton County Indiana Sheriff Sale (November 2026)</h2><h3>Sale Type : Foreclosure Sale</h3>Items: 2</div>"""


class Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status


class FakeWeb:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _get(self, url, **kw):
        self.calls.append(url)
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return Resp(value)
        raise NotFound(f"not found: {url}")


def test_govease_auctions_and_parcels_are_read():
    auctions = o.parse_govease(GOVEASE_HTML)
    assert [(a.state, a.county) for a in auctions] == [("CA", "Kern County"), ("CO", "Baca County"),
                                                       ("TN", "Obion County"), ("TN", "City of Jackson")]
    assert auctions[1].url == "https://liveauctions.govease.com/CO/cobaca/1235/browsestandard"
    assert auctions[2].sale_type == "court-ordered tax sale"
    items = o.parse_govease_items(GOVEASE_HTML, auctions[1].url)
    assert [(i["parcel"], i["starting_bid"], i["address"]) for i in items] == [
        ("R001", 1234.5, "12 MAIN ST"), ("R003", 500.0, "VAC/AVE Q4/VIC 9TH STE"), ("R004", 700.0, "9 OAK ST UNIT 5"),
        ("R002", 88.0, "0 COUNTY RD 4")]
    assert items[0]["sale_type"] == "Tax Lien"
    assert items[0]["link"] == "https://liveauctions.govease.com/co/cobaca/1235/openstandardparcel/5/R001"


def test_bid4assets_calendar_marks_past_sales():
    got = o.parse_bid4assets(BID4ASSETS_HTML, today=date(2026, 11, 21))
    assert [(a.state, a.county, a.sale_type, a.status) for a in got] == [
        ("NV", "Douglas County", "tax deed", "already held"), ("PA", "Berks County", "judicial tax sale", "scheduled")]
    assert got[1].url == "https://www.bid4assets.com/storefront/BerksPADec26"


def test_zeus_sales_are_read_and_comments_ignored():
    got = o.parse_zeus(ZEUS_HTML)
    assert [(a.state, a.county, a.sale_type, a.items) for a in got] == [
        ("CO", "Gunnison County", "tax lien", 148), ("IN", "Fulton County", "sheriff sale", 2)]
    assert got[0].starts == "11/17/2026 9:00 AM CST" and got[0].status == "registration closed"
    assert got[0].url == "https://www.zeusauction.com/auctionsale.php?idSale=2747"


def online(routes):
    return o.OnlineSales(FakeWeb(routes), clock=lambda: 0.0)


ALL = {"govease": GOVEASE_HTML, "bid4assets": BID4ASSETS_HTML, "zeusauction": ZEUS_HTML}


def test_options_for_a_state_and_for_every_state():
    sales = online(ALL)
    co = sales.options("Colorado")
    assert {a["platform"] for a in co["auctions"]} == {"GovEase", "Zeus Auction (SRI)"}
    assert co["state"]["sale_type"] == "tax lien" and "problems" not in co
    every = sales.options()
    assert set(every["states_with_online_sales"]) >= {"CA", "CO", "TN", "NV", "PA", "IN"}
    assert every["states_with_online_sales"]["CO"]["platforms"] == ["GovEase", "Zeus Auction (SRI)"]
    assert every["also_check"][0]["platform"].startswith("RealAuction")
    assert [a["county"] for a in sales.options("CO", "Baca County")["auctions"]] == ["Baca County"]
    assert len(sales.web.calls) == 3  # calendars are cached between questions


def test_a_blocked_platform_is_reported_and_others_still_answer():
    sales = online({"govease": NotFound("liveauctions.govease.com answered 503"), "bid4assets": BID4ASSETS_HTML,
                    "zeusauction": ZEUS_HTML})
    out = sales.options("Ohio")
    assert out["auctions"] == [] and "GovEase" in out["problems"] and "next_step" in out
    with pytest.raises(ValueError):
        sales.options("Atlantis")


def test_govease_parcels_become_priced_listings():
    rows, found = online(ALL).listings("Colorado", "Baca")
    assert [a["county"] for a in found] == ["Baca County"]
    assert [(r.address, r.price, r.kind, r.owner) for r in rows] == [
        ("12 MAIN ST", 1234.5, "house", ""), ("VAC/AVE Q4/VIC 9TH STE", 500.0, "land", ""),
        ("9 OAK ST UNIT 5", 700.0, "unknown", ""), ("0 COUNTY RD 4", 88.0, "land", "SMITH JOHN")]
    assert "starting bid on GovEase" in rows[0].price_note and "exact" in rows[0].price_note


def L(price, value, kind="house", addr="x"):
    return Listing(county="c", parcel=addr, address=addr, price=price, price_note="", value=value, kind=kind)


def test_deals_rank_by_value_per_dollar_and_skip_junk():
    rows = [L(1000, 50_000, addr="a"), L(500, 100_000, addr="b"), L(50, 800, "land", "strip"),
            L(30, 90_000, "other", "garage"), L(200, None, addr="novalue"), L(3000, 60_000, "land", "lot")]
    guess = L(10, 90_000, addr="guess")
    guess.price_note = "estimated yearly tax"
    waiting = L(5, 400_000, addr="waiting")
    waiting.status = "delinquent since 2024 (can't be bought until it forfeits)"
    rows += [guess, waiting]
    ranked, skipped = rank_deals(rows)
    assert [r.address for r in ranked] == ["b", "a", "lot", "guess", "novalue"]
    assert "only an estimate" in ranked[3].extra["deal"]["why"]
    assert ranked[0].extra["deal"]["value_per_dollar"] == 200.0
    assert skipped == {"parcel parts (garage stalls, common areas)": 1, "worth under $10,000": 1,
                       "not for sale yet (only delinquent)": 1}
    assert ranked[-1].extra["deal"]["value_per_dollar"] is None
