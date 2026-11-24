"""The Atlanta real estate game: real Georgia tax sale rules, bidding, flips, companies, the secretary, and
Jarvis's tools for it."""
from datetime import date, timedelta

import pytest

from jarvis_books.errors import ValidationError
from jarvis_books.game import Game, rules
from jarvis_books.game.catalog import KINDS_BY_KEY, renovations_for

DAY = date(2026, 11, 23)


@pytest.fixture
def game(tmp_path):
    return Game(tmp_path / "game" / "atlanta.json", seed=7, today=DAY)


def at_sale(game):
    game.go_to_tax_sale()
    return [p for p in game.tax_sale()["parcels"]]


def win_a_house(game):
    """Bid until the player owns a tax deed on a house; rivals can't outbid with the max money given."""
    game.dev("set_money", value=1_000_000)
    parcels = at_sale(game)
    house = next(p for p in parcels if p["type"] == "single-family house")
    game.start_auction(house["id"])
    out = game.bid(20_000)
    assert "you_won" in out, out
    return out


# ---- the real rules ------------------------------------------------------------------------------------
@pytest.mark.parametrize("year, month, expected", [
    (2026, 12, date(2026, 12, 1)),  # Fulton's own page: the December 1, 2026 sale
    (2027, 1, date(2027, 1, 5)),
    (2030, 1, date(2030, 1, 2)),  # Jan 1 is the first Tuesday: the sale moves to the next business day
    (2025, 7, date(2025, 7, 1)),
    (2023, 7, date(2023, 7, 5)),  # July 4 is a Tuesday
])
def test_the_sheriffs_sale_is_the_first_tuesday_or_the_next_business_day(year, month, expected):
    assert rules.first_tuesday(year, month) == expected


def test_redemption_is_the_bid_plus_taxes_plus_20_then_10_percent_a_year():
    sold = date(2026, 12, 1)
    assert rules.redemption_amount(3000, 0, sold, sold + timedelta(days=30)) == 3600  # 20% for any part of year 1
    assert rules.redemption_amount(3000, 500, sold, sold + timedelta(days=365)) == 4200
    assert rules.redemption_amount(3000, 0, sold, sold + timedelta(days=366)) == 3900  # into year 2: 30%
    assert rules.redemption_amount(3000, 0, sold, sold + timedelta(days=800)) == 4200  # year 3: 40%


def test_atlanta_business_fees_match_the_state_and_city():
    assert rules.SOS_ARTICLES == 110 and rules.ANNUAL_REGISTRATION == 60
    assert rules.occupation_tax(0, 1) == 50  # flat $50, first employee exempt
    assert rules.occupation_tax(0, 4) == 125  # $25 for each employee after the first
    assert rules.occupation_tax(110_000, 1) == 50 + 100_000 * rules.OTC_RATE_ESTIMATE


# ---- a new game ---------------------------------------------------------------------------------------
def test_a_new_game_starts_with_8000_and_saves_itself(game, tmp_path):
    s = game.status()
    assert s["money"]["cash"] == 8000 and s["money"]["net_worth"] == 8000 and s["today"] == DAY.isoformat()
    assert s["next_tax_sale"] == "2026-12-01"
    again = Game(tmp_path / "game" / "atlanta.json")
    assert again.status() == s and again.rng.random() == game.rng.random()


def test_closing_mid_auction_picks_up_exactly_where_we_left_off(game, tmp_path):
    game.dev("set_money", value=50_000)
    parcel = at_sale(game)[1]
    game.start_auction(parcel["id"])
    game.state["auction"]["rivals"][0]["max"] = 5000
    game.save()
    first = game.bid(parcel["opening_bid"] + 100)
    reopened = Game(tmp_path / "game" / "atlanta.json")  # Jarvis closed and opened again
    assert reopened.status() == game.status() and reopened.state["auction"] == game.state["auction"]
    assert reopened.state["auction"]["high"] == first["high_bid"]
    assert reopened.bid(20_000) == game.bid(20_000)  # same rivals, same dice: the game carries on identically
    assert reopened.status()["where_we_left_off"][-1].endswith("for $20,000.")


def test_a_damaged_save_picks_up_from_the_backup(game, tmp_path):
    game.dev("add_money", value=1234)
    game.advance(3)
    path = tmp_path / "game" / "atlanta.json"
    path.write_text("{half written")
    back = Game(path)
    assert back.state["today"] == game.state["today"] and back.status()["money"]["cash"] == 9234
    assert list(path.parent.glob("atlanta.broken-*.json"))


def test_a_damaged_save_is_kept_and_a_new_game_starts(tmp_path):
    path = tmp_path / "atlanta.json"
    path.write_text("{not json")
    g = Game(path, seed=1, today=DAY)
    assert g.status()["money"]["cash"] == 8000 and list(tmp_path.glob("atlanta.broken-*.json"))


def test_tax_sale_houses_are_always_worth_100_to_200_thousand(tmp_path):
    for seed in range(12):
        g = Game(tmp_path / f"{seed}.json", seed=seed, today=DAY)
        for p in g.tax_sale()["parcels"]:
            if p["type"] == "single-family house":
                assert 99_000 <= p["worth"] <= 201_000, p
                assert p["opening_bid"] < 4000  # the taxes and costs, well under most bidders' limit


def test_every_property_shows_what_is_wrong_and_what_fixing_it_costs(game):
    listing = next(iter(game.market()["for_sale"]))
    report = game.inspect(listing["id"])
    assert report["problems"] == [] or all(p["fix_cost"] > 0 and p["problem"] for p in report["problems"])
    assert report["worth_after_fixing_everything"] >= report["worth_now"]
    assert all("problems" in p for p in game.tax_sale()["parcels"])


# ---- bidding at the tax sale -------------------------------------------------------------------------
def test_bidding_only_happens_on_sale_day(game):
    parcel = game.tax_sale()["parcels"][0]
    with pytest.raises(ValidationError, match="go to the tax sale"):
        game.start_auction(parcel["id"])


def test_rival_bidders_mostly_stop_around_4000(tmp_path):
    tops = []
    for seed in range(40):
        g = Game(tmp_path / f"{seed}.json", seed=seed, today=DAY)
        tops += [r["max"] for r in g._rivals(1500)]
    assert sum(t <= 4000 for t in tops) / len(tops) > 0.8 and max(tops) > 4000


def test_outbidding_the_rivals_wins_the_deed_and_the_excess_is_held_for_the_owner(game):
    game.dev("set_money", value=50_000)
    parcel = at_sale(game)[1]
    opened = game.start_auction(parcel["id"])
    assert opened["high_bid"] == parcel["opening_bid"] and opened["high_bidder"] != "you"
    out = game.bid(20_000)
    assert out["you_won"] and out["paid"] == 20_000 and game.status()["money"]["cash"] == 30_000
    assert out["excess_funds_for_owner"] == 20_000 - parcel["opening_bid"]
    assert out["if_redeemed_in_first_year_you_get"] == 24_000
    assert game.deeds()["tax_deeds"][0]["paid"] == 20_000


def test_a_rival_answers_a_low_bid_and_passing_lets_them_win(game):
    parcel = at_sale(game)[1]
    game.start_auction(parcel["id"])
    game.state["auction"]["rivals"][0]["max"] = 3000  # a rival who will go to $3,000
    out = game.bid(parcel["opening_bid"] + 100)
    assert out["high_bidder"] != "you" and out["high_bid"] > parcel["opening_bid"] + 100
    assert game.pass_auction()["sold_to"] == out["high_bidder"]


def test_you_cant_bid_more_than_you_have(game):
    parcel = at_sale(game)[1]
    game.start_auction(parcel["id"])
    with pytest.raises(ValidationError, match="cashier's check"):
        game.bid(9000)


def test_an_owner_who_redeems_pays_the_20_percent_premium(game):
    won = win_a_house(game)
    game.dev("set", "redemption_rate", 0.99)
    game.advance(200)
    assert won["deed_id"] not in game.state["deeds"]
    call = next(c for c in game.state["calls"] if "redeemed" in c["text"])
    assert "$24,000" in call["text"]


def test_foreclosing_waits_12_months_then_the_house_is_yours_after_45_days(game):
    won = win_a_house(game)
    game.dev("set", "redemption_rate", 0.0)
    with pytest.raises(ValidationError, match="12 months"):
        game.foreclose(won["deed_id"])
    game.advance(365)
    out = game.foreclose(won["deed_id"])
    assert out["redemption_deadline"] == (game.today + timedelta(days=45)).isoformat()
    game.advance(45)
    assert not game.state["deeds"] and len(game.state["properties"]) == 1
    assert any("yours" in c["text"] for c in game.state["calls"])


# ---- buying, renovating and flipping ------------------------------------------------------------------
def own_a_listing(game):
    game.dev("set_money", value=10_000_000)
    lst = next(r for r in game.market()["for_sale"] if r["type"] != "vacant land lot")
    out = game.make_offer(lst["id"], lst["asking"])
    assert out["accepted"] and out["closing_costs"] == round(lst["asking"] * 0.02, 2)
    return out["property_id"]


def test_a_lowball_offer_is_refused_and_a_close_one_is_countered(game):
    lst = game.market()["for_sale"][0]
    assert game.make_offer(lst["id"], lst["asking"] * 0.5)["accepted"] is False
    p = game.state["listings"][lst["id"]]
    p["lowest"] = p["asking"]
    out = game.make_offer(lst["id"], lst["asking"] * 0.9)
    assert out["counter_offer"] >= lst["asking"] * 0.9


def test_each_renovation_has_its_sale_price_before_you_do_it(game):
    pid = own_a_listing(game)
    q = game.renovation_quotes(pid)
    assert q["renovations"] and all(r["sale_price_after"] - q["sale_price_now"] == r["adds"] for r in q["renovations"])
    assert {r["verdict"] for r in q["renovations"]} <= {"worth it", "about even", "loses money"}
    best = q["renovations"][0]
    started = game.renovate(pid, [best["renovation"]])
    assert started["worth_when_done"] == best["sale_price_after"]
    game.advance(best["days"])
    assert game.status()["properties"][0]["worth"] == best["sale_price_after"]


def test_a_needed_repair_adds_more_than_the_same_job_on_a_sound_house(game):
    pid = own_a_listing(game)
    p = game.state["properties"][pid]
    p["issues"], p["done"] = ["roof"], []
    needed = next(r for r in game.renovation_quotes(pid)["renovations"] if r["renovation"] == "roof")
    p["issues"] = []
    sound = next(r for r in game.renovation_quotes(pid)["renovations"] if r["renovation"] == "roof")
    assert needed["needed"] and not sound["needed"] and needed["adds"] > sound["adds"]


def test_flipping_offers_come_through_the_secretary_and_accepting_sells(game):
    pid = own_a_listing(game)
    game.list_for_sale(pid, agent=True)
    game.dev("set", "market_heat", 50)
    game.advance(10)
    waiting = game.calls()["waiting_for_answer"]
    assert waiting
    cash = game.status()["money"]["cash"]
    sold = game.answer_call(waiting[0], True)
    assert sold["agent_commission"] == round(sold["price"] * 0.05, 2)
    assert sold["transfer_tax"] == round(sold["price"] / 1000, 2)
    assert game.status()["money"]["cash"] == pytest.approx(cash + sold["you_got"])
    assert pid not in game.state["properties"]


def test_rent_is_paid_on_the_first_of_the_month(game):
    pid = own_a_listing(game)
    rent = game.rent_out(pid)["monthly_rent"]
    game.state["properties"][pid]["tenant"]["status"] = "signed"
    cash = game.status()["money"]["cash"]
    game.advance((date(2026, 12, 1) - DAY).days)
    assert game.status()["money"]["cash"] == pytest.approx(cash + rent)


def test_every_property_type_can_be_bought_and_lots_have_no_renovations():
    assert {"school", "college", "store", "apartments", "storage", "office", "warehouse"} <= set(KINDS_BY_KEY)
    assert renovations_for(KINDS_BY_KEY["lot"]) == []


# ---- companies ---------------------------------------------------------------------------------------
def test_starting_a_company_files_with_the_state_and_city(game):
    out = game.start_company("Peach Flip", "renovation crew", "llc", employees=3)
    assert out["name"] == "Peach Flip, LLC" and out["paid"] == 110 + 50 + 50
    assert any("Secretary of State" in s for s in out["steps_done"]) and any("ATLBIZ" in s for s in out["steps_done"])
    with pytest.raises(ValidationError, match="already has a company"):
        game.start_company("peach flip", "trucking")


def test_a_renovation_crew_company_makes_renovations_cheaper(game):
    pid = own_a_listing(game)
    before = game.renovation_quotes(pid)["renovations"][0]
    game.start_company("Crew", "renovation crew")
    after = next(r for r in game.renovation_quotes(pid)["renovations"] if r["renovation"] == before["renovation"])
    assert after["cost"] == pytest.approx(round(before["cost"] * 0.85, -1), abs=10)


def test_companies_pay_the_annual_registration_by_april_first(game):
    game.dev("set_money", value=100_000)
    game.start_company("Atl Coffee", "coffee shop")
    game.advance((date(2027, 4, 1) - DAY).days)
    assert any("annual registration: $60" in c["text"] for c in game.state["calls"])


# ---- development mode ---------------------------------------------------------------------------------
def test_dev_mode_changes_the_game(game):
    assert game.dev("set", "starting_cash", 12_000)["cash"] == 12_000
    game.dev("add_money", value=500)
    assert game.status()["money"]["cash"] == 12_500
    with pytest.raises(ValidationError):
        game.dev("set", "nonsense", 1)
    assert game.new_game(seed=3, today=DAY)["money"]["cash"] == 12_000


# ---- Jarvis plays it with the player ------------------------------------------------------------------
@pytest.fixture
def tools(library, make_engine, tmp_path):
    from fakes import FakeGemini
    from jarvis_books.assistant import ToolBox
    from jarvis_books.jobs import JobManager
    from jarvis_books.memory import StoryIndex
    from jarvis_books.writer import BookWriter
    engine = make_engine(FakeGemini())
    index = StoryIndex(library, engine)
    jobs = JobManager(library, workers=1)
    box = ToolBox(library, BookWriter(library, engine, index), index, jobs)
    box.game = Game(tmp_path / "jarvis_game.json", seed=11, today=DAY)
    yield box
    jobs.shutdown(wait=True)


def test_every_game_tool_has_a_permission_and_comes_up_when_the_player_talks(tools):
    from jarvis_books.assistant.game_tools import GAME_TOOLS
    from jarvis_books.assistant.permissions import TOOL_LEVELS
    assert set(GAME_TOOLS) <= set(TOOL_LEVELS)
    offered = {d["name"] for d in tools.select("let's go to the tax sale and bid on a house")}
    assert {"game_tax_sale", "game_bid", "game_go_to_tax_sale"} <= offered
    assert "game_status" in {d["name"] for d in tools.select("how much money do we have in the game")}


def test_jarvis_plays_a_whole_tax_sale_by_tool_calls(tools):
    assert tools.call("game_status")["money"]["cash"] == 8000
    sale = tools.call("game_go_to_tax_sale")
    assert sale["is_today"]
    house = next(p for p in sale["parcels"] if p["type"] == "single-family house")
    opened = tools.call("game_start_auction", {"parcel_id": house["id"]})
    assert opened["high_bid"] == house["opening_bid"]
    too_much = tools.call("game_bid", {"amount": 9000})
    assert "cashier's check" in too_much["error"]
    out = tools.call("game_bid", {"amount": 7900})
    assert "you_won" in out or out["high_bidder"] != "you"
    assert tools.call("game_lesson", {"topic": "redemption"})["source"].startswith("O.C.G.A. 48-4-40")


def test_starting_over_needs_the_players_yes_but_moves_do_not(tools):
    assert tools.gate.decide("game_new") == "ask" and tools.gate.decide("game_bid") == "free"
    assert "erased" in tools.describe("game_new", {})
    assert tools.call("game_dev", {"action": "add_money", "value": 1000})["cash"] == 9000
    assert "error" in tools.call("game_inspect", {"id": "Z99"})


def test_jarvis_is_told_not_to_say_tool_names_out_loud():
    from jarvis_books.writer.prompts import ASSISTANT
    assert "Never read out or write your tool names" in ASSISTANT and "The Atlanta game" in ASSISTANT
