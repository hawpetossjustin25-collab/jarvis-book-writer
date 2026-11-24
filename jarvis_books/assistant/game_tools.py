"""Jarvis's tools for the Atlanta real estate game (mixed into the ToolBox). The player talks; Jarvis plays
the guide, the auctioneer's voice and the secretary, and calls these to make every move."""
from __future__ import annotations

from typing import Optional

from jarvis_books.errors import NotFound, ValidationError
from jarvis_books.game.catalog import INDUSTRIES, RENOVATIONS
from jarvis_books.game.engine import TUNABLES

S = {"type": "STRING"}
I = {"type": "INTEGER"}
B = {"type": "BOOLEAN"}
N = {"type": "NUMBER"}
DOLLARS = {"type": "NUMBER", "description": "dollars"}
PROP = {"type": "STRING", "description": "the id shown in the game, like P3 (owned), L5 (for sale), T12 "
        "(tax sale parcel) or D4 (tax deed)"}


def _decl(name: str, description: str, props: Optional[dict] = None, required: Optional[list[str]] = None) -> dict:
    params: dict = {"type": "OBJECT", "properties": props or {}}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


GAME_DECLARATIONS = [
    _decl("game_status", "The Atlanta game: the money meter (cash, property values, tax deeds, companies, net "
          "worth), the game date, everything the player owns, companies, unheard secretary calls and the next "
          "tax sale."),
    _decl("game_new", "Start the Atlanta game over from the beginning (erases the current game). starting_cash "
          "defaults to the game setting ($8,000).", {"starting_cash": DOLLARS}),
    _decl("game_market", "Properties for sale in Atlanta right now (houses, condos, apartments, stores, storage, "
          "offices, schools, colleges and more), with asking price, worth and what's wrong with each.",
          {"kind": S, "area": S, "max_price": DOLLARS}),
    _decl("game_inspect", "Inspect a property before buying: every problem (roof, foundation, HVAC...) with the "
          "cost to fix it, and what it's worth now and after fixing everything.", {"id": PROP}, ["id"]),
    _decl("game_offer", "Offer money for a property for sale (a listing, id L...). The seller accepts, "
          "counters or refuses. Buyer closing costs are about 2%.", {"listing_id": PROP, "amount": DOLLARS},
          ["listing_id", "amount"]),
    _decl("game_renovation_quotes", "Before renovating: every renovation for a property with its cost, days, "
          "whether it's needed, the sale price right after it, and whether it makes or loses money.",
          {"id": PROP}, ["id"]),
    _decl("game_renovate", "Start renovations on a property the player owns. items: renovation keys, comma "
          "separated (" + ", ".join(r.key for r in RENOVATIONS) + ").", {"property_id": PROP, "items": S},
          ["property_id", "items"]),
    _decl("game_list_property", "Put an owned property on the market to sell (flip). price 0 = its market "
          "value. agent false = for sale by owner: no 5% commission but slower offers.",
          {"property_id": PROP, "price": DOLLARS, "agent": B}, ["property_id"]),
    _decl("game_rent_out", "Find a tenant for an owned property; rent comes in on the 1st of each month.",
          {"property_id": PROP}, ["property_id"]),
    _decl("game_tax_sale", "The next sheriff's tax sale: date, where (Fulton downtown at 136 Pryor St SW, "
          "DeKalb in Decatur), each parcel with owner, taxes due, opening bid, worth and problems."),
    _decl("game_go_to_tax_sale", "Move the game to the tax sale day so bidding can start."),
    _decl("game_start_auction", "On sale day, open the bidding on one tax sale parcel (id T...).",
          {"parcel_id": PROP}, ["parcel_id"]),
    _decl("game_bid", "Bid in the running tax sale auction. Rival bidders answer; if nobody tops it, the "
          "player wins and pays on the spot.", {"amount": DOLLARS}, ["amount"]),
    _decl("game_pass", "Stop bidding in the running auction (the high bidder wins)."),
    _decl("game_tax_deeds", "The player's tax deeds still in the redemption period: what they paid, what the "
          "owner must pay to redeem today, and when they can foreclose the right to redeem."),
    _decl("game_foreclose", "Send the notice that bars the right to redeem a tax deed (allowed 12 months after "
          "the sale; the deadline is 45 days later).", {"deed_id": PROP}, ["deed_id"]),
    _decl("game_start_company", "Start a company or startup in Atlanta the real way: Georgia Secretary of State "
          "filing, registered agent, EIN and the City of Atlanta business license. industry: "
          + ", ".join(INDUSTRIES) + ". entity: llc or corporation.",
          {"name": S, "industry": S, "entity": {"type": "STRING", "enum": ["llc", "corporation"]},
           "employees": I}, ["name", "industry"]),
    _decl("game_staff", "Change how many employees a company has.", {"company_id": S, "employees": I},
          ["company_id", "employees"]),
    _decl("game_close_company", "Dissolve one of the player's companies.", {"company_id": S}, ["company_id"]),
    _decl("game_calls", "Calls the secretary took at the Atlanta office: offers, buyers, redemptions, tenants, "
          "the tax sale. Read them to the player.", {"all": B}),
    _decl("game_answer_call", "Answer a call that needs a decision (an offer or a buyer): accept sells the "
          "property at that price; counter (dollars) asks the buyer for more instead, and they take it or walk.",
          {"call_id": S, "accept": B, "counter": DOLLARS}, ["call_id", "accept"]),
    _decl("game_advance", "Move game time forward some days (1 to 3660): renovations finish, offers and calls "
          "come in, rent and company profits are paid on the 1st, owners redeem.", {"days": I}, ["days"]),
    _decl("game_lesson", "The real Atlanta rule behind something in the game, with its source: tax sale, "
          "redemption, foreclosure, excess funds, company, business license, buying.", {"topic": S}),
    _decl("game_goal", "Progress toward the goal of a trillion dollars: net worth, percent of the way, the "
          "next milestone and the milestones reached."),
    _decl("game_take_loan", "Borrow from the bank against a property the player owns (up to 75% of its value, "
          "8% a year, interest paid monthly; paid off from the sale if it's sold).",
          {"property_id": PROP, "amount": DOLLARS}, ["property_id", "amount"]),
    _decl("game_repay_loan", "Pay back a bank loan (amount 0 = all of it).", {"loan_id": S, "amount": DOLLARS},
          ["loan_id"]),
    _decl("game_title", "The liens on a property's or tax deed's title (old security deeds, judgments, City "
          "liens), whether the title is clear, clouded or in court, and what clearing it costs.", {"property_id": PROP},
          ["property_id"]),
    _decl("game_clear_title", "Clear a foreclosed tax deed property's title: pay off the liens that survive and "
          "file the quiet title case (about 90 days). Needed before a bank loan or a sale.", {"property_id": PROP},
          ["property_id"]),
    _decl("game_take_personal_loan", "Take a personal loan (12% a year over 60 months, up to 30% of the equity in "
          "clear-title properties, $100,000 at most) to fund more tax sale buys. for_bidder puts it in the "
          "bidding assistant's budget.", {"amount": DOLLARS, "for_bidder": {"type": "BOOLEAN"}}, ["amount"]),
    _decl("game_fund_bidder", "Add money to the bidding assistant's budget.", {"amount": DOLLARS}, ["amount"]),
    _decl("game_transit", "Atlanta's transit systems the player can buy: the Atlanta Streetcar (light rail), "
          "MARTA rail and the MARTA bus system, with price, monthly income and upgrades (hyperloop, electric "
          "buses)."),
    _decl("game_buy_transit", "Buy a transit system: streetcar, marta_rail or marta_bus.", {"system": S},
          ["system"]),
    _decl("game_upgrade_transit", "Upgrade an owned transit system: hyperloop (streetcar or MARTA rail) or "
          "electric (buses). It takes months to build and multiplies the income.",
          {"system": S, "upgrade": {"type": "STRING", "enum": ["hyperloop", "electric"]}}, ["system"]),
    _decl("game_car_lot", "The player's car lots: cars in stock, space left, cars sold, and the cars they can "
          "stock with buy and sell prices.", {"property_id": PROP}),
    _decl("game_stock_cars", "Buy cars for a car lot the player owns; they sell over the following months.",
          {"property_id": PROP, "model": S, "count": I}, ["property_id", "model", "count"]),
    _decl("game_trading", "AI day trading of Micro Nasdaq futures (MNQ): whether it's unlocked (after the first "
          "flip, with $120,000 to $180,000), the account balance, profit, agents and recent trading days (one every 8 "
          "real minutes while the app is open)."),
    _decl("game_open_trading", "Open the AI day-trading account with a deposit of $120,000 to $180,000.",
          {"deposit": DOLLARS}, ["deposit"]),
    _decl("game_trading_funds", "Move money into (deposit) or out of (withdraw) the trading account.",
          {"action": {"type": "STRING", "enum": ["deposit", "withdraw"]}, "amount": DOLLARS},
          ["action", "amount"]),
    _decl("game_trading_agents", "Control the AI trading agents: on/off, how many (1-5) and the percent of the "
          "account each risks per trade (0.25-5).", {"on": B, "agents": I, "risk_percent": N}),
    _decl("game_bidder", "The hired bidding assistant: whether hired (possible after the first flip), budget "
          "left, the most it bids per property, what it goes after and what it won."),
    _decl("game_hire_bidder", "Hire a bidding assistant ($2,500 a month) who goes to every tax sale and bids for "
          "the player against the other bidders. budget: total to spend; max_per_property: the most for one; "
          "kinds: property types, comma separated (default house).",
          {"budget": DOLLARS, "max_per_property": DOLLARS, "kinds": S}, ["budget", "max_per_property"]),
    _decl("game_fire_bidder", "Let the bidding assistant go."),
    _decl("game_learn", "'Let's learn': pause the game and get the real Atlanta lesson for what the player just "
          "did (or a topic): the steps in order, where to go, the paperwork and fees, and the law behind it.",
          {"topic": S}),
    _decl("game_back_to_game", "End the lesson and un-pause the game ('back to the game')."),
    _decl("game_dev", "Development mode: change the game. action settings (show them), set (key to value; "
          "keys: " + ", ".join(TUNABLES) + "), add_money, set_money, new_tax_sale or new_listings.",
          {"action": {"type": "STRING", "enum": ["settings", "set", "add_money", "set_money", "new_tax_sale",
                                                 "new_listings"]}, "key": S, "value": N}, ["action"]),
]
GAME_TOOLS = tuple(d["name"] for d in GAME_DECLARATIONS)
# what a lesson pauses: anything that moves time or money (looking around and learning still work)
GAME_LOOKS = ("game_status", "game_market", "game_inspect", "game_renovation_quotes", "game_tax_sale",
              "game_tax_deeds", "game_title", "game_lesson", "game_goal", "game_transit", "game_car_lot", "game_trading",
              "game_bidder", "game_calls", "game_learn", "game_back_to_game", "game_dev", "game_new")
GAME_MOVES = tuple(n for n in GAME_TOOLS if n not in GAME_LOOKS)
GAME_WORDS = ("game", "atlanta", "tax sale", "tax lien", "auction", "bid", "flip", "renovat", "fix up", "property",
              "properties", "house", "money meter", "how much money", "secretary", "calls", "company", "startup",
              "start up", "tenant", "rent", "deed", "redeem", "foreclos", "inspect", "listing", "offer", "buy",
              "sell", "market", "net worth", "next month", "skip ahead", "wait", "trillion", "goal", "milestone",
              "loan", "borrow", "bank", "lien", "mortgage", "title", "marta", "streetcar", "light rail", "rail", "bus", "hyperloop", "transit",
              "car lot", "cars", "dealership", "trading", "trade", "day trad", "nasdaq",
              "futures", "mnq", "stock", "agents", "assistant", "hire", "bid for", "learn", "teach", "back to the game")


class GameTools:
    """Mixed into ToolBox; `self.game` is set by the Studio."""

    game = None

    def _game(self):
        if self.game is None:
            raise NotFound("the Atlanta game isn't available here")
        return self.game

    def _play(fn):  # noqa: N805 - a decorator for the tool methods below
        def wrapped(self, *a, **kw):
            try:
                name = fn.__name__[3:]
                if name in GAME_MOVES and self.game is not None and self.game.paused:
                    return {"error": "the game is paused for a lesson; say 'back to the game' to play on"}
                return fn(self, *a, **kw)
            except (ValidationError, ValueError) as exc:
                return {"error": str(exc)}
        wrapped.__name__ = fn.__name__
        return wrapped

    def describe_game(self, name: str, args: dict) -> Optional[str]:
        if name == "game_new":
            return "Start the Atlanta game over (the current game is erased)"
        return None

    @_play
    def _t_game_status(self) -> dict:
        return self._game().status()

    @_play
    def _t_game_new(self, starting_cash: Optional[float] = None) -> dict:
        g = self._game()
        t = dict(g.state.get("tunables") or {})
        if starting_cash is not None:
            t["starting_cash"] = float(starting_cash)
        return g.new_game(tunables=t)

    @_play
    def _t_game_market(self, kind: str = "", area: str = "", max_price: float = 0) -> dict:
        return self._game().market(kind, area, max_price)

    @_play
    def _t_game_inspect(self, id: str) -> dict:  # noqa: A002 - the declared argument name
        return self._game().inspect(id)

    @_play
    def _t_game_offer(self, listing_id: str, amount: float) -> dict:
        return self._game().make_offer(listing_id, amount)

    @_play
    def _t_game_renovation_quotes(self, id: str) -> dict:  # noqa: A002
        return self._game().renovation_quotes(id)

    @_play
    def _t_game_renovate(self, property_id: str, items: str) -> dict:
        return self._game().renovate(property_id, [i for i in items.replace(";", ",").split(",")])

    @_play
    def _t_game_list_property(self, property_id: str, price: float = 0, agent: bool = True) -> dict:
        return self._game().list_for_sale(property_id, price, agent)

    @_play
    def _t_game_rent_out(self, property_id: str) -> dict:
        return self._game().rent_out(property_id)

    @_play
    def _t_game_tax_sale(self) -> dict:
        return self._game().tax_sale()

    @_play
    def _t_game_go_to_tax_sale(self) -> dict:
        return self._game().go_to_tax_sale()

    @_play
    def _t_game_start_auction(self, parcel_id: str) -> dict:
        return self._game().start_auction(parcel_id)

    @_play
    def _t_game_bid(self, amount: float) -> dict:
        return self._game().bid(amount)

    @_play
    def _t_game_pass(self) -> dict:
        return self._game().pass_auction()

    @_play
    def _t_game_tax_deeds(self) -> dict:
        return self._game().deeds()

    @_play
    def _t_game_foreclose(self, deed_id: str) -> dict:
        return self._game().foreclose(deed_id)

    @_play
    def _t_game_start_company(self, name: str, industry: str, entity: str = "llc", employees: int = 1) -> dict:
        return self._game().start_company(name, industry, entity, employees)

    @_play
    def _t_game_staff(self, company_id: str, employees: int) -> dict:
        return self._game().staff(company_id, employees)

    @_play
    def _t_game_close_company(self, company_id: str) -> dict:
        return self._game().close_company(company_id)

    @_play
    def _t_game_calls(self, all: bool = False) -> dict:  # noqa: A002
        return self._game().calls(only_new=not all)

    @_play
    def _t_game_answer_call(self, call_id: str, accept: bool, counter: float = 0) -> dict:
        return self._game().answer_call(call_id, accept, counter)

    @_play
    def _t_game_advance(self, days: int) -> dict:
        return self._game().advance(days)

    @_play
    def _t_game_lesson(self, topic: str = "") -> dict:
        return self._game().lesson(topic)

    @_play
    def _t_game_goal(self) -> dict:
        return self._game().goal()

    @_play
    def _t_game_take_loan(self, property_id: str, amount: float) -> dict:
        return self._game().take_loan(property_id, amount)

    @_play
    def _t_game_title(self, property_id: str) -> dict:
        return self._game().title(property_id)

    @_play
    def _t_game_clear_title(self, property_id: str) -> dict:
        return self._game().clear_title(property_id)

    @_play
    def _t_game_take_personal_loan(self, amount: float, for_bidder: bool = False) -> dict:
        return self._game().take_personal_loan(amount, for_bidder)

    @_play
    def _t_game_fund_bidder(self, amount: float) -> dict:
        return self._game().fund_bidder(amount)

    @_play
    def _t_game_repay_loan(self, loan_id: str, amount: float = 0) -> dict:
        return self._game().repay_loan(loan_id, amount)

    @_play
    def _t_game_transit(self) -> dict:
        return self._game().transit()

    @_play
    def _t_game_buy_transit(self, system: str) -> dict:
        return self._game().buy_transit(system)

    @_play
    def _t_game_upgrade_transit(self, system: str, upgrade: str = "hyperloop") -> dict:
        return self._game().upgrade_transit(system, upgrade)

    @_play
    def _t_game_car_lot(self, property_id: str = "") -> dict:
        return self._game().car_lot(property_id)

    @_play
    def _t_game_stock_cars(self, property_id: str, model: str, count: int) -> dict:
        return self._game().stock_cars(property_id, model, count)

    @_play
    def _t_game_trading(self) -> dict:
        return self._game().trading()

    @_play
    def _t_game_open_trading(self, deposit: float) -> dict:
        return self._game().open_trading(deposit)

    @_play
    def _t_game_trading_funds(self, action: str, amount: float) -> dict:
        return self._game().trading_funds(action, amount)

    @_play
    def _t_game_trading_agents(self, on: Optional[bool] = None, agents: Optional[int] = None,
                               risk_percent: Optional[float] = None) -> dict:
        return self._game().trading_agents(on, agents, risk_percent)

    @_play
    def _t_game_bidder(self) -> dict:
        return self._game().bidder()

    @_play
    def _t_game_hire_bidder(self, budget: float, max_per_property: float, kinds: str = "house") -> dict:
        return self._game().hire_bidder(budget, max_per_property, kinds)

    @_play
    def _t_game_fire_bidder(self) -> dict:
        return self._game().fire_bidder()

    @_play
    def _t_game_learn(self, topic: str = "") -> dict:
        return self._game().learn(topic)

    @_play
    def _t_game_back_to_game(self) -> dict:
        return self._game().resume()

    @_play
    def _t_game_dev(self, action: str, key: str = "", value: float = 0) -> dict:
        return self._game().dev(action, key, value)
