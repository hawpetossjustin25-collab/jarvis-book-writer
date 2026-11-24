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


# ---- the trillion-dollar climb ------------------------------------------------------------------------
def test_the_goal_is_a_trillion_and_milestones_call_in(game):
    g = game.goal()
    assert g["target"] == 1e12 and g["next_milestone"] == 100_000 and not g["milestones_reached"]
    game.dev("set_money", value=2_000_000)
    assert game.goal()["milestones_reached"] == [100_000, 1_000_000]
    assert sum("Milestone" in c["text"] for c in game.state["calls"]) == 2
    game.dev("set_money", value=1.2e12)
    assert game.goal()["won_on"] == game.state["today"]
    assert any("ONE TRILLION" in c["text"] for c in game.state["calls"])


def test_bank_loans_lend_75_percent_charge_interest_and_are_paid_from_a_sale(game):
    pid = own_a_listing(game)
    worth = game.value(game.state["properties"][pid])
    with pytest.raises(ValidationError, match="75%"):
        game.take_loan(pid, worth)
    cash = game.state["cash"]
    loan = game.take_loan(pid, round(worth * 0.5, -2))
    assert game.state["cash"] == pytest.approx(cash + loan["borrowed"])
    assert game.net_worth()["loans"] == -loan["borrowed"]
    before = game.state["cash"]
    game.advance((date(2026, 12, 1) - DAY).days)
    assert game.state["cash"] == pytest.approx(before - loan["monthly_interest"], abs=1)
    sold = game._sell(pid, worth, "a buyer")
    assert sold["loan_paid_off"] == loan["borrowed"] and not game.state["loans"]


def test_prices_grow_each_january(game):
    pid = own_a_listing(game)
    before = game.value(game.state["properties"][pid])
    game.advance((date(2027, 1, 1) - DAY).days)
    assert game.state["market_index"] != 1.0
    assert game.value(game.state["properties"][pid]) == pytest.approx(before * game.state["market_index"], rel=0.01)


def test_companies_are_worth_eight_years_of_profit(game):
    game.dev("set_money", value=10_000_000)
    game.start_company("Big Crew", "renovation crew", employees=100)
    game.advance((date(2027, 3, 2) - DAY).days)
    c = next(iter(game.state["companies"].values()))
    yearly = sum(c["recent_profits"]) * 12 / len(c["recent_profits"])
    assert game.company_value(c) == pytest.approx(max(0, yearly) * 8, rel=1e-6)


def test_big_buildings_are_always_on_the_market(game):
    types = {r["type"] for r in game.market()["for_sale"]}
    assert len(types & {"car lot (dealership)", "mixed-use development", "downtown office tower (skyscraper)",
                        "stadium", "hotel", "college campus"}) == 3


# ---- transit and car lots -----------------------------------------------------------------------------
def test_buy_the_streetcar_collect_fares_and_upgrade_it_to_a_hyperloop(game):
    game.dev("set_money", value=5e9)
    out = game.buy_transit("light rail")
    assert out["bought"] == "Atlanta Streetcar (light rail)" and game.net_worth()["transit"] == 120e6
    with pytest.raises(ValidationError, match="already own"):
        game.buy_transit("streetcar")
    cash = game.state["cash"]
    game.advance((date(2026, 12, 1) - DAY).days)
    assert game.state["cash"] == pytest.approx(cash + 0.45e6)
    up = game.upgrade_transit("streetcar", "hyperloop")
    assert up["income_after"] == pytest.approx(0.45e6 * 9)
    game.advance(540)
    assert game.transit()["systems"][0]["done"] == ["hyperloop"]
    assert any("hyperloop upgrade" in c["text"] for c in game.state["calls"])


def test_marta_rail_and_buses_can_be_bought(game):
    game.dev("set_money", value=20e9)
    assert game.buy_transit("MARTA trains")["bought"] == "MARTA rail"
    assert game.buy_transit("the bus system")["bought"] == "MARTA bus system"
    with pytest.raises(ValidationError, match="upgrades: electric"):
        game.upgrade_transit("bus", "hyperloop")


def test_a_car_lot_stocks_cars_and_sells_them_for_a_profit(game):
    game.dev("set_money", value=50_000_000)
    lot = next((r for r in game.market()["for_sale"] if r["type"] == "car lot (dealership)"), None)
    if lot is None:
        p = game._make_property("car_lot", "Downtown")
        p["asking"] = p["lowest"] = game.value(p)
        game.state["listings"]["L999"] = p
        lot = {"id": "L999", "asking": p["asking"]}
    pid = game.make_offer(lot["id"], lot["asking"])["property_id"]
    out = game.stock_cars(pid, "SUV", 20)
    assert out["paid"] == 20 * 28_000
    with pytest.raises(ValidationError, match="holds 80"):
        game.stock_cars(pid, "pickup", 61)
    game.dev("set", "market_heat", 3)
    game.advance((date(2026, 12, 1) - DAY).days)
    report = game.car_lot(pid)["lots"][0]
    assert report["sold_total"] > 0 and report["cars"].get("suv", 0) == 20 - report["sold_total"]
    assert any("Sold" in c["text"] and "cars" in c["text"] for c in game.state["calls"])


# ---- AI day trading -----------------------------------------------------------------------------------
def flip_a_house(game):
    game.dev("set_money", value=10_000_000)
    p = game._make_property("house", "West End")
    p["asking"] = p["lowest"] = game.value(p)
    game.state["listings"]["L777"] = p
    pid = game.make_offer("L777", p["asking"])["property_id"]
    return game._sell(pid, game.value(game.state["properties"][pid]), "a buyer")


def test_trading_unlocks_only_after_the_first_flip_and_with_120_to_180_thousand(game):
    info = game.trading()
    assert info["unlocked"] is False and "flip your first house" in info["needs"][0]
    with pytest.raises(ValidationError, match="after you flip"):
        game.open_trading(150_000)
    flip_a_house(game)
    assert any("day trading" in c["text"] and "unlocked" in c["text"] for c in game.state["calls"])
    assert game.trading()["can_open_now"]
    for bad in (100_000, 200_000):
        with pytest.raises(ValidationError, match="120,000 to \\$180,000"):
            game.open_trading(bad)
    cash = game.state["cash"]
    out = game.open_trading(150_000)
    assert out["balance"] == 150_000 and game.state["cash"] == cash - 150_000
    assert game.net_worth()["trading_account"] == 150_000


def test_the_ai_agents_buy_the_floor_and_sell_the_ceiling_each_trading_day(game):
    flip_a_house(game)
    game.open_trading(150_000)
    game.advance(30)
    assert game.trading()["trading_days_so_far"] == 0  # game days don't trade; real minutes do
    for _ in range(25):
        game.trading_session()
    days = game.trading()["last_days"]
    assert days and game.trading()["trading_days_so_far"] == 25 and days[-1]["day"].startswith("day 25")
    assert any("last 20 trading days" in c["text"] for c in game.state["calls"])
    for d in days:
        assert d["floor"] < d["ceiling"] and d["floor"] % 0.25 == 0 and d["ceiling"] % 0.25 == 0
    a = game.state["trading"]
    assert a["balance"] == pytest.approx(150_000 + sum(d["pnl"] for d in a["history"]), abs=0.05)


def test_skilled_agents_grow_the_account_and_unskilled_ones_lose(tmp_path):
    results = {}
    for skill in (0.75, 0.25):
        g = Game(tmp_path / f"{skill}.json", seed=5, today=DAY)
        flip_a_house(g)
        g.open_trading(150_000)
        g.dev("set", "agent_skill", skill)
        for _ in range(80):
            g.trading_session()
        results[skill] = g.trading()["balance"]
    assert results[0.75] > 150_000 > results[0.25]


def test_trading_funds_and_agent_controls(game):
    flip_a_house(game)
    game.open_trading(120_000)
    game.trading_funds("deposit", 30_000)
    assert game.trading()["deposited"] == 150_000
    cash = game.state["cash"]
    game.trading_funds("withdraw", 10_000)
    assert game.state["cash"] == cash + 10_000
    out = game.trading_agents(on=False, agents=9, risk_percent=2)
    assert out["on"] is False and out["agents"] == 5 and out["risk_percent"] == 2
    before = game.trading()["balance"]
    for _ in range(10):
        game.trading_session()
    assert game.trading()["balance"] == before  # agents switched off don't trade


def test_the_trading_clock_runs_a_trading_day_every_interval(game):
    import time as _time
    from jarvis_books.game.trading import TradingClock
    assert game.t("trading_minutes") == 8 and TradingClock(game).interval() == 480
    flip_a_house(game)
    game.open_trading(150_000)
    clock = TradingClock(game, minutes=0.05 / 60)
    clock.start()
    try:
        deadline = _time.time() + 5
        while game.trading()["trading_days_so_far"] < 3 and _time.time() < deadline:
            _time.sleep(0.02)
    finally:
        clock.stop()
    assert game.trading()["trading_days_so_far"] >= 3 and not clock.running


def test_the_desk_reports_every_trading_day_and_the_agents_get_smarter(game):
    flip_a_house(game)
    game.open_trading(150_000)
    start = game.trading()["agent_win_rate"]
    traded = [game.trading_session() for _ in range(150)]  # the office keeps the last 200 calls
    reports = [c for c in game.state["calls"] if c["kind"] == "trading"]
    assert len(reports) == sum(1 for d in traded if d.get("traded")) and "floor" in reports[-1]["text"]
    assert "win rate is now" in reports[-1]["text"]
    later = game.trading()
    assert later["agent_win_rate"] > start and later["agent_win_rate"] <= start + 0.12
    assert "trades of experience" in later["learned_so_far"]
    game.trading_funds("withdraw", 50_000)  # profits go toward bigger properties


def test_countering_a_buyer_sells_when_they_can_afford_it_and_they_walk_when_they_cant(game):
    pid = own_a_listing(game)
    game.list_for_sale(pid)
    game.dev("set", "market_heat", 50)
    game.advance(10)
    call = next(c for c in game.state["calls"] if c["kind"] == "offer" and c["status"] == "new")
    too_high = game.answer_call(call["id"], False, counter=call["buyer_max"] + 1000)
    assert "walked away" in too_high["buyer_said"] and pid in game.state["properties"]
    game.advance(10)
    call = next(c for c in game.state["calls"] if c["kind"] == "offer" and c["status"] == "new")
    out = game.answer_call(call["id"], False, counter=call["buyer_max"])
    assert out["price"] == call["buyer_max"] >= call["amount"] and pid not in game.state["properties"]


def test_a_bought_listing_still_answers_to_its_listing_id(game):
    game.dev("set_money", value=10_000_000)
    lst = next(r for r in game.market()["for_sale"] if r["type"] != "vacant land lot")
    pid = game.make_offer(lst["id"], lst["asking"])["property_id"]
    assert game.renovation_quotes(lst["id"])["property"]["id"] == pid
    assert game.inspect(lst["id"])["id"] == pid
    game.list_for_sale(lst["id"])
    assert game.state["properties"][pid]["listed"]
    other = next(r for r in game.market()["for_sale"])
    with pytest.raises(ValidationError, match=f"you own {pid}"):
        game.renovate(other["id"], ["paint"])


def test_a_won_tax_deed_answers_to_its_parcel_id(game):
    won = win_a_house(game)
    parcel = game.state["deeds"][won["deed_id"]]["was"]
    game.dev("set", "redemption_rate", 0.0)
    game.advance(365)
    assert game.foreclose(parcel)["notice_sent"]



# ---- the bidding assistant -----------------------------------------------------------------------------
def test_the_bidding_assistant_is_hired_after_the_first_flip_and_bids_at_every_sale(game):
    with pytest.raises(ValidationError, match="after you flip"):
        game.hire_bidder(20_000, 5000)
    flip_a_house(game)
    out = game.hire_bidder(20_000, 9000, "house")
    assert out["hired"] and out["kinds"] == ["single-family house"]
    cash = game.state["cash"]
    game.go_to_tax_sale()
    b = game.bidder()
    report = next(c for c in game.state["calls"] if c["from"] == "Your bidding assistant")
    assert b["won"] and "won" in report["text"]
    spent = sum(w["paid"] for w in b["won"])
    assert b["budget_left"] == pytest.approx(20_000 - spent)
    assert all(w["paid"] <= 9000 for w in b["won"])
    assert game.state["cash"] == pytest.approx(cash - spent - 2500)  # Dec 1 is also payday
    assert {d["was"] for d in game.state["deeds"].values()} == {
        p["id"] for p in game.state["sale"]["parcels"] if p["status"] == "sold to you"}
    assert b["budget_left"] < 9000 or all(  # with budget left, it went after every house it could afford
        p["status"] != "for sale" for p in game.state["sale"]["parcels"]
        if p["property"]["kind"] == "house" and p["opening_bid"] <= b["budget_left"])


def test_the_bidding_assistant_stops_at_its_limit_and_draws_a_salary(game):
    flip_a_house(game)
    game.hire_bidder(500, 500)  # below every opening bid: never bids
    cash = game.state["cash"]
    game.advance((date(2027, 1, 1) - DAY).days)
    assert game.bidder()["won"] == []
    assert game.state["cash"] <= cash - 2500  # paid on the 1st
    assert game.fire_bidder()["let_go"] and not game.bidder()["hired"]


def test_a_buyers_top_price_stays_secret(game):
    pid = own_a_listing(game)
    game.list_for_sale(pid)
    game.dev("set", "market_heat", 50)
    game.advance(10)
    assert all("buyer_max" not in c for c in game.calls(only_new=False)["calls"])
    assert "buyer_max" not in str(game.status())


# ---- let's learn ------------------------------------------------------------------------------------
def test_lets_learn_pauses_the_game_and_teaches_the_last_move_the_atlanta_way(game):
    parcel = at_sale(game)[1]
    game.start_auction(parcel["id"])
    lesson = game.learn()
    assert lesson["paused"] and lesson["lesson"] == "Buying at an Atlanta sheriff's tax sale"
    assert parcel["address"].split(",")[0] in lesson["what_you_just_did"]
    assert any("136 Pryor Street SW" in w for w in lesson["where_to_go"])
    assert any("48-4-5" in law for law in lesson["law_and_rules"]) and lesson["steps"][0].startswith("Get the list")
    assert game.status()["paused_for_lesson"] == "tax_sale"
    assert game.resume()["was_learning"] == "tax_sale" and not game.paused


def test_every_move_has_a_real_lesson_with_steps_places_paperwork_and_law():
    from jarvis_books.game.lessons import PLAYBOOKS, playbook_for
    for key, book in PLAYBOOKS.items():
        assert book["steps"] and book["where"] and book["paperwork"] and book["law"] and book["game_note"], key
    assert playbook_for("how do I start an LLC") == "company"
    assert playbook_for("foreclosing the deed") == "foreclose"
    assert playbook_for("pulling permits for the roof") == "renovate"


def test_a_lesson_on_a_topic_and_what_it_pauses(tools):
    from jarvis_books.assistant.game_tools import GAME_MOVES
    assert {"game_advance", "game_bid", "game_offer", "game_renovate"} <= set(GAME_MOVES)
    out = tools.call("game_learn", {"topic": "starting a company"})
    assert out["lesson"] == "Starting a company in Atlanta" and any("eCorp" in s for s in out["steps"])
    assert "paused for a lesson" in tools.call("game_advance", {"days": 3})["error"]
    assert tools.call("game_status")["paused_for_lesson"] == "company"  # looking around still works
    tools.call("game_back_to_game")
    assert "today" in tools.call("game_advance", {"days": 3})


def test_trading_waits_while_a_lesson_is_on(game):
    flip_a_house(game)
    game.open_trading(150_000)
    game.learn("trading")
    assert game.trading_session() == {} and game.trading()["trading_days_so_far"] == 0
    game.resume()
    assert game.trading_session()["day"].startswith("day 1")


def test_jarvis_is_told_to_play_only_through_the_game_tools():
    from jarvis_books.writer.prompts import ASSISTANT
    assert "never say a bid was placed or what a rival did without calling the tool" in ASSISTANT


# ---- liens, clearing the title and personal loans --------------------------------------------------------
def own_a_foreclosed_house(game, liens):
    deed = win_a_house(game)["deed_id"]
    d = game.state["deeds"][deed]
    d["liens"] = liens
    d["sale_day"] = (DAY - timedelta(days=400)).isoformat()
    game.dev("set", key="redemption_rate", value=0)
    game.foreclose(deed)
    game.advance(46)
    return next(pid for pid, p in game.state["properties"].items() if p.get("was") == d["was"])


def test_tax_sale_parcels_show_the_liens_found_in_the_title_search(game):
    for parcel in game.state["sale"]["parcels"]:
        assert isinstance(parcel["property"].get("liens"), list)
    assert any(p["property"]["liens"] for p in game.state["sale"]["parcels"])
    shown = next(r for r in at_sale(game) if r.get("liens_on_title"))
    assert any(k in shown["liens_on_title"][0] for k in ("security deed", "judgment lien", "city lien"))


def test_foreclosed_house_title_must_be_cleared_before_loans_or_a_sale_then_a_personal_loan_funds_more_bids(game):
    liens = [{"kind": "security deed", "holder": "Truist Bank", "amount": 90_000.0},
             {"kind": "city lien", "holder": "City of Atlanta (code enforcement and water)", "amount": 1_200.0}]
    pid = own_a_foreclosed_house(game, liens)
    p = game.state["properties"][pid]
    assert p["title"]["status"] == "clouded"
    assert p["liens"][0]["status"].startswith("wiped out") and p["liens"][1]["status"] == "still owed"
    view = game.title(pid)
    assert view["to_clear_it"]["liens_to_pay_off"] == 1_200.0
    with pytest.raises(ValidationError, match="clear"):
        game.take_loan(pid, 1_000)
    with pytest.raises(ValidationError, match="title insurance"):
        game.list_for_sale(pid)
    game.state["properties"] = {pid: p}
    with pytest.raises(ValidationError, match="clear title"):
        game.take_personal_loan(10_000)

    cash = game.state["cash"]
    filed = game.clear_title(pid)
    assert game.state["cash"] == pytest.approx(cash - 4_500 - 1_200 - 75)
    assert game.title(pid)["title"] == "in court"
    game.advance(91)
    assert game.title(pid)["title"] == "clear"
    assert any("quiet title decree" in c["text"] for c in game.state["calls"])
    assert filed["decree_expected"] <= game.state["today"]

    game.state["flips"] = 1
    game.hire_bidder(budget=5_000, max_per_property=4_000)
    room = game.personal_limit()
    assert room == pytest.approx(min(100_000, 0.30 * game.value(p)), abs=100)
    with pytest.raises(ValidationError, match="approve up to"):
        game.take_personal_loan(room + 1_000)
    cash = game.state["cash"]
    loan = game.take_personal_loan(room, for_bidder=True)
    assert game.state["cash"] == pytest.approx(cash + room)
    assert game.state["bidder"]["budget_left"] == pytest.approx(5_000 + room)
    assert loan["monthly_payment"] == pytest.approx(room * 0.01 / (1 - 1.01 ** -60), abs=0.01)
    assert game.net_worth()["loans"] == -room
    before = game.state["cash"]
    first = date.fromisoformat(game.state["today"]).replace(day=1)
    first = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    game.advance((first - game.today).days)
    owed = game.state["personal_loans"][loan["loan_id"]]["balance"]
    assert owed < room
    assert game.state["cash"] < before
    game.dev("add_money", value=room * 2)
    game.repay_loan(loan["loan_id"])
    assert not game.state["personal_loans"]
    assert game.take_loan(pid, 10_000)["borrowed"] == 10_000


def test_lets_learn_teaches_clearing_liens_and_personal_loans():
    from jarvis_books.game.lessons import PLAYBOOKS, playbook_for
    assert playbook_for("clearing the mortgages off the house") == "title"
    assert playbook_for("taking out a personal loan") == "personal_loan"
    assert playbook_for("buying a tax lien") == "tax_sale"
    assert any("23-3-60" in law for law in PLAYBOOKS["title"]["law"])
    assert any("GSCCCA" in w for w in PLAYBOOKS["title"]["where"])
    assert any("certified funds" in s for s in PLAYBOOKS["personal_loan"]["steps"])


def test_arriving_at_the_tax_sale_reports_what_the_bidding_assistant_already_won(game):
    game.dev("set_money", value=500_000)
    game.state["flips"] = 1
    game.hire_bidder(budget=200_000, max_per_property=50_000, kinds="house,duplex,store,lot")
    out = game.go_to_tax_sale()
    assert "I won" in out["your_bidding_assistant_this_morning"]
    won = {w["address"] for w in game.state["bidder"]["won"]}
    assert won and not won & {p["address"].split(",")[0] for p in out["parcels"]}


def test_jarvis_waits_for_the_player_to_say_back_to_the_game():
    from jarvis_books.writer.prompts import ASSISTANT
    assert "Never call game_back_to_game on your own" in ASSISTANT
    assert "exactly as the lesson returns them" in ASSISTANT


# ---- time travel ---------------------------------------------------------------------------------------
def test_go_back_one_move_undoes_a_purchase_and_can_be_redone(game):
    pid = own_a_listing(game)
    was = game.state["properties"][pid]["was"]
    paid_cash = game.state["cash"]
    back = game.go_back(moves=1)
    assert pid not in game.state["properties"] and was in game.state["listings"]
    assert back["cash_then"] == game.state["cash"] != paid_cash
    assert any("bought" in u for u in back["undone"])
    later = max(s["snapshot"] for s in game.timeline()["recent"] if "bought" in s["after"])
    game.go_back(snapshot=later)
    assert pid in game.state["properties"] and game.state["cash"] == paid_cash


def test_go_back_twice_keeps_walking_back_instead_of_bouncing_forward(game):
    game.dev("set_money", value=1_000_000)
    game.advance(3)
    game.advance(3)
    game.advance(3)
    d9 = game.state["today"]
    game.go_back(moves=1)
    assert game.state["today"] < d9
    d6 = game.state["today"]
    game.go_back(moves=1)
    assert game.state["today"] < d6


def test_go_back_to_a_day_replays_to_exactly_that_day_and_survives_a_restart(game, tmp_path):
    game.advance(40)
    target = (DAY + timedelta(days=10)).isoformat()
    out = game.go_back(to_day=target)
    assert game.state["today"] == target and out["from"] == (DAY + timedelta(days=40)).isoformat()
    with pytest.raises(ValidationError, match="future"):
        game.go_back(to_day="2030-01-01")
    again = Game(game.path)
    assert again.state["today"] == target
    assert again.go_back(to_day=DAY.isoformat())["went_back_to"] == DAY.isoformat()


def test_go_forward_to_a_day_or_to_the_next_event(game):
    sale = game.state["sale"]["date"]
    assert game.go_forward("the tax sale")["jumped_to"] == sale
    assert game.state["today"] == sale
    pid = own_a_listing(game)
    quotes = game.renovation_quotes(pid)
    item = quotes["renovations"][0]["renovation"] if "renovations" in quotes else None
    if item:
        game.dev("add_money", value=500_000)
        until = game.renovate(pid, [item])["ready_on"]
        game.go_forward("renovation")
        assert game.state["today"] == until and not game.state["properties"][pid].get("renovating")
    game.go_forward("2028-03-01")
    assert game.state["today"] == "2028-03-01"
    assert game.go_forward("next month")["jumped_to"] == "2028-04-01"
    with pytest.raises(ValidationError, match="go back"):
        game.go_forward("2027-01-01")
    with pytest.raises(ValidationError, match="no quiet title"):
        game.go_forward("title decree")


def test_timeline_is_trimmed_and_a_new_game_starts_a_fresh_one(game, monkeypatch):
    from jarvis_books.game import timeline
    monkeypatch.setattr(timeline, "TIMELINE_KEEP", 5)
    for _ in range(14):
        game.advance(1)
    assert len(game.timeline_path.read_text(encoding="utf-8").splitlines()) <= 5
    game.new_game(seed=3, today=DAY)
    assert game.timeline()["snapshots"] == 1


def test_undo_keeps_what_happened_earlier_the_same_day(game):
    game.dev("set_money", value=400_000)
    listing = min(game.market()["for_sale"], key=lambda r: r["asking"])
    game.make_offer(listing["id"], listing["asking"])
    game.go_back(moves=1)
    assert game.state["cash"] == 400_000 and listing["id"] in game.state["listings"]
    again = Game(game.path)
    again.advance(2)
    again.go_back(moves=1)
    assert again.state["cash"] == 400_000
