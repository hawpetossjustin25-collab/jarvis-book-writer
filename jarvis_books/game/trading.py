"""AI day trading of Micro Nasdaq futures, unlocked after the first flip. Mixed into Game.

The contract is real (CME Micro E-mini Nasdaq-100, MNQ: $2 per index point, ticks of 0.25 = $0.50); the
market is simulated. The AI agents trade a range: buy near the day's floor, sell near its ceiling, with a
stop-loss under the floor. Some days lose."""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

from jarvis_books.errors import ValidationError

log = logging.getLogger(__name__)

POINT_VALUE = 2.0  # dollars per index point for MNQ
TICK = 0.25
COMMISSION_ROUND_TRIP = 1.24  # game estimate per contract (about $0.62 a side)
DAY_MARGIN = 100.0  # game estimate of a broker's day-trading margin per contract
OPEN_MIN, OPEN_MAX = 120_000.0, 180_000.0
MAX_AGENTS = 5
START_LEVEL = 21_000.0
SESSIONS_PER_MONTH = 20  # trading days in a month, for the desk's monthly report
MAX_LEARNED = 0.12  # the most the agents' hit rate improves with experience
LEARNING_TRADES = 150  # trades for the agents to learn about two thirds of what they can


def _tick(x: float) -> float:
    return round(round(x / TICK) * TICK, 2)


class Trading:
    state: dict[str, Any]

    def _acct(self) -> dict | None:
        return self.state.get("trading")

    def trading(self) -> dict:
        with self._lock:
            a = self._acct()
            flips = self.state.get("flips", 0)
            if not a:
                ready = flips >= 1 and self.state["cash"] >= OPEN_MIN
                return {"unlocked": flips >= 1, "flips": flips, "cash": self.state["cash"], "can_open_now": ready,
                        "needs": [] if ready else
                        (["flip your first house (buy, fix, sell)"] if flips < 1 else [])
                        + ([f"${OPEN_MIN:,.0f} to ${OPEN_MAX:,.0f} to fund the account"]
                           if self.state["cash"] < OPEN_MIN else []),
                        "contract": "Micro E-mini Nasdaq-100 futures (MNQ): $2 per point, ticks of 0.25 = $0.50",
                        "approach": "AI agents buy the day's floor and sell its ceiling, with a stop-loss"}
            return {"unlocked": True, "balance": a["balance"], "deposited": a["deposited"],
                    "profit": round(a["balance"] - a["deposited"], 2), "agents": a["agents"], "on": a["on"],
                    "risk_percent": a["risk"] * 100, "nasdaq_level": a["level"],
                    "last_days": a["history"][-10:], "wins": a["wins"], "losses": a["losses"],
                    "contract": "MNQ, $2 per point", "trading_days_so_far": a.get("sessions", 0),
                    "minutes_per_trading_day": self.t("trading_minutes"),
                    "agent_win_rate": round(min(0.9, self.t("agent_skill") + self.learned(a)), 3),
                    "learned_so_far": f"+{self.learned(a) * 100:.1f} points from {int(a.get('trades', 0))} "
                                      "trades of experience"}

    def open_trading(self, deposit: float) -> dict:
        with self._lock:
            if self._acct():
                raise ValidationError("the trading account is already open")
            if self.state.get("flips", 0) < 1:
                raise ValidationError("day trading unlocks after you flip your first house")
            if not OPEN_MIN <= deposit <= OPEN_MAX:
                raise ValidationError(f"open the account with ${OPEN_MIN:,.0f} to ${OPEN_MAX:,.0f}")
            self._spend(deposit, "funding the trading account")
            self.state["trading"] = {"balance": round(deposit, 2), "deposited": round(deposit, 2), "agents": 3,
                                     "on": True, "risk": 0.01, "level": START_LEVEL, "history": [], "wins": 0,
                                     "losses": 0, "month_pnl": 0.0}
            self._note(f"Opened an AI day-trading account with ${deposit:,.0f}.")
            self._did("trading", f"opened the trading account with ${deposit:,.0f}")
            self.save()
            return {**self.trading(), "note": "3 AI agents trade one trading day every 8 minutes while Jarvis is open; each risks 1% of the account"}

    def trading_funds(self, action: str, amount: float) -> dict:
        with self._lock:
            a = self._acct()
            if not a:
                raise ValidationError("open the trading account first")
            if amount <= 0:
                raise ValidationError("give an amount in dollars")
            if action == "deposit":
                self._spend(amount, "the deposit")
                a["balance"] = round(a["balance"] + amount, 2)
                a["deposited"] = round(a["deposited"] + amount, 2)
            elif action == "withdraw":
                if amount > a["balance"]:
                    raise ValidationError(f"the account has ${a['balance']:,.0f}")
                a["balance"] = round(a["balance"] - amount, 2)
                a["deposited"] = round(a["deposited"] - amount, 2)
                self._earn(amount)
            else:
                raise ValidationError("action is deposit or withdraw")
            self.save()
            return self.trading()

    def trading_agents(self, on: bool | None = None, agents: int | None = None,
                       risk_percent: float | None = None) -> dict:
        with self._lock:
            a = self._acct()
            if not a:
                raise ValidationError("open the trading account first")
            if on is not None:
                a["on"] = bool(on)
            if agents is not None:
                a["agents"] = max(1, min(MAX_AGENTS, int(agents)))
            if risk_percent is not None:
                a["risk"] = max(0.0025, min(0.05, float(risk_percent) / 100))
            self.save()
            return self.trading()

    def trading_session(self) -> dict:
        """One trading day for the AI agents (the clock runs one every few real minutes while Jarvis is open)."""
        with self._lock:
            out = self._session()
            if out is not None:
                self.save()
            return out or {}

    def _session(self):
        a = self._acct()
        if not a or self.state.get("learning"):  # the game is paused for a lesson
            return None
        rng = self.rng
        a["sessions"] = a.get("sessions", 0) + 1
        label = f"day {a['sessions']} ({time.strftime('%b %d %I:%M %p')})"
        level = a["level"]
        open_ = _tick(level * (1 + rng.gauss(0.0003, 0.006)))
        width = max(40.0, abs(rng.gauss(0.012, 0.004)) * open_)
        floor = _tick(open_ - width * rng.uniform(0.3, 0.7))
        ceiling = _tick(floor + width)
        close = _tick(rng.uniform(floor, ceiling))
        a["level"] = close
        if a["sessions"] % SESSIONS_PER_MONTH == 0:
            self._trading_month()
        if not a["on"] or a["balance"] < DAY_MARGIN:
            return {"day": label, "traded": False}
        learned = self.learned(a)
        skill = min(0.9, self.t("agent_skill") + learned)
        timing = 0.1 - 0.5 * learned  # experienced agents buy closer to the floor
        # a sell just under the ceiling against a stop well under the floor: about even odds needed to profit
        entry = _tick(floor + timing * width)
        target, stop = _tick(ceiling - 0.25 * width), _tick(floor - 0.5 * width)
        pnl, trades = 0.0, 0
        for _ in range(a["agents"]):
            if rng.random() > 0.8:  # price never came near the floor for this agent's setup
                continue
            risk_pts = entry - stop
            contracts = max(1, min(int(a["balance"] * a["risk"] / (risk_pts * POINT_VALUE)),
                                   int(a["balance"] / DAY_MARGIN / max(1, a["agents"]))))
            won = rng.random() < skill
            points = (target - entry) if won else -(entry - stop)
            a["trades"] = a.get("trades", 0) + 1
            a["trade_wins"] = a.get("trade_wins", 0) + (1 if won else 0)
            if not won:  # a loss teaches a little more than a win
                a["trades"] += 0.5
            pnl += points * POINT_VALUE * contracts - COMMISSION_ROUND_TRIP * contracts
            trades += 1
        if not trades:
            return {"day": label, "traded": False, "floor": floor, "ceiling": ceiling}
        pnl = round(pnl, 2)
        a["balance"] = round(max(0.0, a["balance"] + pnl), 2)
        a["month_pnl"] = round(a.get("month_pnl", 0.0) + pnl, 2)
        a["wins" if pnl > 0 else "losses"] += 1
        day = {"day": label, "floor": floor, "ceiling": ceiling, "trades": trades, "pnl": pnl}
        a["history"] = (a["history"] + [day])[-60:]
        self._call("trading", "Your AI trading desk",
                   f"Trading {label}: Nasdaq floor {floor:,.2f}, ceiling {ceiling:,.2f}. {trades} trade"
                   f"{'s' if trades != 1 else ''}, {'made' if pnl >= 0 else 'lost'} ${abs(pnl):,.0f}"
                   f"{' (rough day)' if pnl < -0.04 * (a['balance'] - pnl) else ''}. Account ${a['balance']:,.0f}; "
                   f"the agents' win rate is now {skill + 0.0:.0%} and climbing as they learn.")
        return {**day, "traded": True, "balance": a["balance"]}

    def _trading_month(self) -> None:
        a = self._acct()
        if a and a.get("month_pnl"):
            m = a["month_pnl"]
            self._call("info", "Your AI trading desk", f"Over the last {SESSIONS_PER_MONTH} trading days the agents {'made' if m >= 0 else 'lost'} "
                       f"${abs(m):,.0f}. The account is at ${a['balance']:,.0f}.")
            a["month_pnl"] = 0.0

    def learned(self, a: dict) -> float:
        """How much the agents have improved from experience (0 to MAX_LEARNED)."""
        return round(MAX_LEARNED * (1 - math.exp(-a.get("trades", 0) / LEARNING_TRADES)), 4)

    def _trading_value(self) -> float:
        a = self._acct()
        return a["balance"] if a else 0.0


class TradingClock:
    """Runs one AI trading day every `trading_minutes` real minutes while the app is open."""

    def __init__(self, game, minutes: float | None = None):
        self.game = game
        self.minutes = minutes
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def interval(self) -> float:
        return 60.0 * float(self.minutes if self.minutes is not None else self.game.t("trading_minutes"))

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="game-trading", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval()):
            try:
                if self.game.state.get("trading"):
                    self.game.trading_session()
            except Exception:
                log.exception("trading session failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
