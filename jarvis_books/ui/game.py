"""The Game tab: the Atlanta real estate game's money meter, what the player owns, the tax sale, the market,
the secretary's calls and buttons to move time. The game is played by talking to Jarvis; this tab shows it."""
from __future__ import annotations

import html
import logging

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QDoubleSpinBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTabWidget,
                             QTextBrowser, QVBoxLayout, QWidget)

from jarvis_books.game import money

log = logging.getLogger(__name__)
REFRESH_MS = 2000
ANNOUNCE = ("trading", "offer", "buyer", "milestone", "info", "reminder")


def _rows(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "<p><i>Nothing yet.</i></p>"
    head = "".join(f"<th align=left>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table cellspacing=0 cellpadding=4 border=1><tr>{head}</tr>{body}</table>"


class GameTab(QWidget):
    def __init__(self, studio, parent=None, announce=None):
        super().__init__(parent)
        self.studio = studio
        self.game = studio.game
        self.announce = announce  # tells the author (status bar, and Jarvis out loud when he's on a call)
        self._closed = False
        self._shown = None
        self._heard = {c["id"] for c in self.game.state["calls"]}  # calls from before this session aren't news
        lay = QVBoxLayout(self)
        self.meter = QLabel()
        self.meter.setStyleSheet("font-size: 22px; font-weight: bold; color: #1b7f3b;")
        lay.addWidget(self.meter)
        self.when = QLabel()
        lay.addWidget(self.when)
        buttons = QHBoxLayout()
        for label, slot in (("Next day", lambda: self._advance(1)), ("Next week", lambda: self._advance(7)),
                            ("Next month", lambda: self._advance(30)), ("Go to the tax sale", self._to_sale),
                            ("Add $8,000", self._add_cash), ("New game", self._new_game)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        lay.addLayout(buttons)
        bidding = QHBoxLayout()
        bidding.addWidget(QLabel("Tax sale parcel:"))
        self.parcel = QLineEdit()
        self.parcel.setPlaceholderText("e.g. T12")
        self.parcel.setMaximumWidth(90)
        bidding.addWidget(self.parcel)
        open_btn = QPushButton("Open bidding")
        open_btn.clicked.connect(self._open_bidding)
        bidding.addWidget(open_btn)
        bidding.addWidget(QLabel("My bid $"))
        self.bid_box = QDoubleSpinBox()
        self.bid_box.setRange(0, 1e12)
        self.bid_box.setDecimals(0)
        self.bid_box.setSingleStep(100)
        bidding.addWidget(self.bid_box)
        for label, slot in (("Bid", self._bid), ("Pass", self._pass)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            bidding.addWidget(b)
        bidding.addStretch(1)
        lay.addLayout(bidding)
        self.views = QTabWidget()
        self.owned, self.sale, self.market, self.calls, self.history = (QTextBrowser() for _ in range(5))
        for w, name in ((self.owned, "What I own"), (self.sale, "Tax sale"), (self.market, "For sale"),
                        (self.calls, "Secretary's calls"), (self.history, "History")):
            self.views.addTab(w, name)
        lay.addWidget(self.views, 1)
        self.status = QLabel("Talk to Jarvis to play: \"Jarvis, what's at the tax sale?\"")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self, force: bool = False) -> None:
        if self._closed:
            return
        try:
            st = self.game.state
            mark = (st["today"], st["cash"], st["next_id"], len(st["calls"]), len(st["log"]),
                    sum(c["status"] == "new" for c in st["calls"]), str(st.get("auction")))
            self._announce_new_calls(st["calls"])
            if mark == self._shown and not force:
                return
            self._shown = mark
            s = self.game.status()
            m = s["money"]
            g = s["goal"]
            self.meter.setText(f"Cash {money(m['cash'])}    Net worth {money(m['net_worth'])}    "
                               f"Goal $1 trillion: {g['progress_percent']:.6f}%"
                               + (f" (next milestone {money(g['next_milestone'])})" if g["next_milestone"] else
                                  "  YOU WON!"))
            self.when.setText(f"Game date {s['today']}  ·  next sheriff's tax sale {s['next_tax_sale']}  ·  "
                              f"{s['new_calls']} new call{'s' if s['new_calls'] != 1 else ''}  ·  "
                              f"$8,000 adds left in {s['cash_adds']['year']}: {s['cash_adds']['left_this_year']} of "
                              f"{s['cash_adds']['allowed_per_year']}  ·  saved "
                              f"automatically after every move (last {s['last_saved'] or 'now'})")
            owned = _rows(["ID", "Property", "Worth", "Paid", "Status"],
                          [[p["id"], f"{p['type']}, {p['address']}", money(p["worth"]), money(p["paid"]),
                            (f"renovating until {p['renovating_until']}" if p["renovating_until"] else
                             f"listed at {money(p['listed_at'])}" if p["listed_at"] else
                             f"rented {money(p['rent'])}/mo" if p["rent"] else "owned")] for p in s["properties"]])
            deeds = _rows(["ID", "Property", "Paid", "Redemption today", "Can foreclose from"],
                          [[d["id"], d["address"], money(d["paid"]), money(d["redemption_price_today"]),
                            d["notice"] and f"deadline {d['notice']}" or d["can_foreclose_from"]]
                           for d in s["tax_deeds"]])
            companies = _rows(["ID", "Company", "Industry", "Employees", "Profit so far"],
                              [[c["id"], c["name"], c["industry"], c["employees"], money(c["profit_total"])]
                               for c in s["companies"]])
            loans = _rows(["ID", "Against", "Owed"], [[ln["id"], ln["address"], money(ln["balance"])]
                                                       for ln in s["loans"]]
                          + [[ln["id"], f"personal loan, {money(ln['payment'])}/mo", money(ln["balance"])]
                             for ln in s["personal_loans"]])
            tr = self.game.transit()["systems"]
            transit = _rows(["System", "Owned", "Monthly income", "Upgrades"],
                            [[t["name"], "yes" if t["owned"] else f"for sale {money(t['price'])}",
                              money(t["monthly_income"]),
                              ", ".join(t.get("done") or []) + (f" (building {t['building']['upgrade']} until "
                                                                f"{t['building']['until']})" if t.get("building")
                                                                else "")] for t in tr])
            tr_acct = self.game.trading()
            trading = (f"<p>Balance {money(tr_acct['balance'])} (profit {money(tr_acct['profit'])}), "
                       f"{tr_acct['agents']} AI agents {'on' if tr_acct['on'] else 'off'}, Nasdaq "
                       f"{tr_acct['nasdaq_level']:,.2f}</p>" if "balance" in tr_acct else
                       f"<p>Locked: {html.escape('; '.join(tr_acct['needs']) or 'ready to open')}</p>")
            self.owned.setHtml(f"<h3>AI day trading (MNQ)</h3>{trading}<h3>Properties</h3>{owned}<h3>Tax deeds (redemption period)</h3>{deeds}"
                               f"<h3>Companies</h3>{companies}<h3>Bank loans</h3>{loans}<h3>Transit</h3>{transit}")
            ts = self.game.tax_sale()
            sale = _rows(["ID", "Property", "Owner", "Worth", "Opening bid", "Problems"],
                         [[p["id"], f"{p['type']}, {p['address']}", p["owner"], money(p["worth"]),
                           money(p["opening_bid"]), "; ".join(p["problems"]) or "none found"] for p in ts["parcels"]])
            auction = ""
            a = st.get("auction")
            if a:
                auction = (f"<h3>Auction running on {a['parcel']}: high bid {money(a['high'])} by "
                           f"{html.escape(a['leader'])}</h3><p>{html.escape(' → '.join(a['history'][-8:]))}</p>")
            self.sale.setHtml(f"<h3>Sheriff's sale {ts['sale_day']}{' (TODAY)' if ts['is_today'] else ''}</h3>"
                              + "".join(f"<p><b>{k}:</b> {html.escape(v)}</p>" for k, v in ts["where"].items())
                              + auction + sale)
            mk = self.game.market()
            self.market.setHtml(_rows(["ID", "Property", "Asking", "Worth", "Condition", "Problems"],
                                      [[p["id"], f"{p['type']}, {p['address']}", money(p["asking"]),
                                        money(p["worth"]), p["condition"], "; ".join(p["problems"]) or "none found"]
                                       for p in mk["for_sale"]]))
            self.calls.setHtml("".join(
                f"<p><b>{html.escape(c['day'])} {html.escape(c['from'])}</b> ({c['id']}, {c['status']}): "
                f"{html.escape(c['text'])}</p>" for c in reversed(st["calls"][-60:])) or "<p><i>No calls yet.</i></p>")
            self.history.setHtml("".join(f"<p>{html.escape(e['day'])}: {html.escape(e['text'])}</p>"
                                         for e in reversed(st["log"][-100:])))
        except Exception:
            log.exception("game tab refresh failed")

    def _announce_new_calls(self, calls: list) -> None:
        for c in calls:
            if c["id"] in self._heard:
                continue
            self._heard.add(c["id"])
            if c["kind"] not in ANNOUNCE:
                continue
            self.status.setText(f"{c['from']}: {c['text']}")
            if self.announce is not None:
                try:
                    self.announce(f"{c['from']}: {c['text']}", c)
                except Exception:
                    log.exception("game announcement failed")

    def _open_bidding(self) -> None:
        out = self._run("game_start_auction", {"parcel_id": self.parcel.text().strip()})
        if "error" not in out:
            self.status.setText(out["auctioneer"])
            self.bid_box.setValue(out["high_bid"] + 100)
            self.views.setCurrentWidget(self.sale)

    def _bid(self) -> None:
        out = self._run("game_bid", {"amount": float(self.bid_box.value())})
        if "error" in out:
            return
        if "you_won" in out:
            self.status.setText(f"SOLD to you: {out['you_won']} for {money(out['paid'])}.")
        else:
            self.status.setText(out["auctioneer"])
            self.bid_box.setValue(out["high_bid"] + 100)

    def _pass(self) -> None:
        out = self._run("game_pass", {})
        if "error" not in out:
            self.status.setText(f"Sold to {out['sold_to']} for {money(out['price'])}." if "sold_to" in out else
                                f"SOLD to you: {out.get('you_won')}")

    def _run(self, name: str, args: dict) -> dict:
        out = self.studio.tools.run(name, args)
        if "error" in out:
            self.status.setText(out["error"])
        self.refresh(force=True)
        return out

    def _advance(self, days: int) -> None:
        out = self._run("game_advance", {"days": days})
        if "error" not in out:
            self.status.setText(f"Moved to {out['today']}; cash changed {money(out['cash_change'])}."
                                + (f" {len(out['new_calls'])} new call(s)." if out["new_calls"] else ""))

    def _add_cash(self) -> None:
        out = self._run("game_add_cash", {})
        if "error" not in out:
            self.status.setText(f"Added {money(out['added'])}. Cash {money(out['cash'])}; "
                                f"{out['left_this_year']} add(s) left in {out['year']}.")

    def _to_sale(self) -> None:
        self._run("game_go_to_tax_sale", {})
        self.views.setCurrentWidget(self.sale)

    def _new_game(self) -> None:
        if QMessageBox.question(self, "New game", "Start the Atlanta game over? The current game is erased.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.game.new_game(tunables=dict(self.game.state["tunables"]))
        self.refresh(force=True)

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
