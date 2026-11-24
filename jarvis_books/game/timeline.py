"""Time travel in the game: jump forward to a date or to the next thing that happens, and rewind to any earlier
day or move. Every save keeps a snapshot of the whole game (money, properties, bids, trading) in a timeline file
next to the save, so rewinding is exact and can be undone by going back to a later snapshot. Mixed into Game."""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from jarvis_books.errors import ValidationError

log = logging.getLogger(__name__)
TIMELINE_KEEP = 400  # snapshots kept; older ones are dropped
EVENTS = ("tax sale", "renovation", "redemption", "notice", "title", "month", "year", "offer")
OFFER_WAIT_DAYS = 365


class Timeline:
    state: dict[str, Any]
    path: Path

    # ---- the snapshots ------------------------------------------------------------------------------
    @property
    def timeline_path(self) -> Path:
        return self.path.with_name(self.path.stem + ".timeline.jsonl")

    def _timeline_marks(self) -> list[dict]:
        if getattr(self, "_marks", None) is None:
            self._marks = []
            try:
                with self.timeline_path.open(encoding="utf-8") as fh:
                    for line in fh:
                        head = line.split("\t", 1)[0]
                        try:
                            self._marks.append(json.loads(head))
                        except ValueError:
                            continue
            except FileNotFoundError:
                pass
        return self._marks

    def _remember(self, text: str) -> None:
        """Keep a snapshot when the day or the last move changed since the previous one."""
        marks = self._timeline_marks()
        head = {"day": self.state["today"], "move": (self.state.get("last_action") or {}).get("what", "")}
        if marks and (marks[-1]["day"], marks[-1]["move"]) == (head["day"], head["move"]):
            self._replace_last(text)
            return
        head["n"] = (marks[-1]["n"] + 1) if marks else 1
        head["parent"] = getattr(self, "_restored_from", None) or (marks[-1]["n"] if marks else 0)
        self._restored_from = None
        head["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self.timeline_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(head) + "\t" + text + "\n")
            marks.append(head)
            self._dirty_last = None
            if len(marks) > TIMELINE_KEEP * 2:
                self._trim()
        except OSError as exc:
            log.warning("couldn't write the game timeline: %s", exc)

    def _lines(self) -> list[str]:
        try:
            return self.timeline_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def _write_lines(self, lines: list[str]) -> None:
        tmp = self.timeline_path.with_suffix(".tmp")
        tmp.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")
        tmp.replace(self.timeline_path)
        self._marks = None

    def _replace_last(self, text: str) -> None:
        """Same day and move as the last snapshot: keep only the newest state for it (one write per day)."""
        self._dirty_last = text

    def _flush_last(self) -> None:
        text = getattr(self, "_dirty_last", None)
        if text is None:
            return
        self._dirty_last = None
        lines = self._lines()
        if lines:
            lines[-1] = lines[-1].split("\t", 1)[0] + "\t" + text
            self._write_lines(lines)

    def _trim(self) -> None:
        self._write_lines(self._lines()[-TIMELINE_KEEP:])

    def _clear_timeline(self) -> None:
        self.timeline_path.unlink(missing_ok=True)
        self._marks, self._dirty_last = [], None

    def timeline(self, limit: int = 20) -> dict:
        with self._lock:
            marks = self._timeline_marks()
            return {"today": self.state["today"], "snapshots": len(marks),
                    "recent": [{"snapshot": m["n"], "day": m["day"], "after": m["move"] or "(time passing)"}
                               for m in marks[-max(1, int(limit)):]],
                    "earliest_day": marks[0]["day"] if marks else self.state["today"]}

    # ---- going back --------------------------------------------------------------------------------
    def go_back(self, to_day: str = "", moves: int = 0, snapshot: int = 0) -> dict:
        with self._lock:
            self._flush_last()
            lines = self._lines()
            heads = [json.loads(ln.split("\t", 1)[0]) for ln in lines]
            if not heads:
                raise ValidationError("there's nothing saved to go back to yet")
            pick: Optional[int] = None
            if snapshot:
                pick = next((i for i, h in enumerate(heads) if h["n"] == int(snapshot)), None)
                if pick is None:
                    raise ValidationError(f"no snapshot {snapshot}; the timeline has {heads[0]['n']} to "
                                          f"{heads[-1]['n']}")
            elif moves:
                by_n = {h["n"]: i for i, h in enumerate(heads)}
                pick = len(heads) - 1
                for _ in range(int(moves)):
                    parent = by_n.get(heads[pick].get("parent"))
                    if parent is None:
                        break
                    pick = parent
            elif to_day:
                try:
                    day = date.fromisoformat(to_day.strip()[:10])
                except ValueError:
                    raise ValidationError("give the day as YYYY-MM-DD") from None
                if day > self.today:
                    raise ValidationError("that day is in the future; go forward to get there")
                cands = [i for i, h in enumerate(heads) if date.fromisoformat(h["day"]) <= day]
                if not cands:
                    raise ValidationError(f"the timeline only goes back to {heads[0]['day']}")
                pick = cands[-1]
            else:
                raise ValidationError("say how far back: a day, a number of moves, or a snapshot number")
            was_day, was_cash = self.state["today"], self.state["cash"]
            undone = [h["move"] for h in heads[pick + 1:] if h["move"] and h["n"] > heads[pick]["n"]]
            self.state = json.loads(lines[pick].split("\t", 1)[1])
            r = self.state["rng"]
            self.rng.setstate((r[0], tuple(r[1]), r[2]))
            self.state.pop("learning", None)
            self._note(f"Went back in time from {was_day} to {self.state['today']}.")
            self._restored_from = heads[pick].get("parent")  # the new snapshot is a copy of this one
            self.save()
            if to_day and not snapshot and not moves and day > self.today:
                self.advance((day - self.today).days)
            return {"went_back_to": self.state["today"], "from": was_day, "snapshot": heads[pick]["n"],
                    "cash_then": self.state["cash"], "cash_before_going_back": was_cash,
                    "undone": list(dict.fromkeys(undone))[-12:], "net_worth": self.net_worth()["net_worth"],
                    "note": "the later snapshots are kept, so you can go forward to them again"}

    # ---- going forward -----------------------------------------------------------------------------
    def _event_day(self, what: str) -> date:
        t, today = what.lower(), self.today
        if "sale" in t or "auction" in t:
            day = date.fromisoformat(self.state["sale"]["date"])
            if day <= today:
                raise ValidationError("the tax sale is today; go to it, or go forward a day for the next one")
            return day
        if "renovat" in t or "repair" in t or "work" in t:
            days = [date.fromisoformat(p["renovating"]["until"]) for p in self.state["properties"].values()
                    if p.get("renovating")]
            if not days:
                raise ValidationError("no renovation is under way")
            return min(days)
        if "redeem" in t or "redemption" in t or "foreclos" in t:
            days = [date.fromisoformat(d["sale_day"]) + timedelta(days=365) for d in self.state["deeds"].values()
                    if not d.get("notice")]
            days = [d for d in days if d > today]
            if not days:
                raise ValidationError("no tax deed is waiting out its 12-month redemption period")
            return min(days)
        if "notice" in t or "deadline" in t:
            days = [date.fromisoformat(d["notice"]) for d in self.state["deeds"].values() if d.get("notice")]
            if not days:
                raise ValidationError("no foreclosure notice is out")
            return min(days)
        if "title" in t or "decree" in t or "quiet" in t:
            days = [date.fromisoformat(p["title"]["until"]) for p in self.state["properties"].values()
                    if (p.get("title") or {}).get("status") == "in court"]
            if not days:
                raise ValidationError("no quiet title case is in court")
            return min(days)
        if "month" in t:
            return date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        if "year" in t or "january" in t:
            return date(today.year + 1, 1, 1)
        try:
            day = date.fromisoformat(t.strip()[:10])
        except ValueError:
            raise ValidationError("go forward to a day (YYYY-MM-DD) or to: " + ", ".join(EVENTS)) from None
        if day <= today:
            raise ValidationError("that day isn't in the future; go back to get there")
        return day

    def go_forward(self, to: str) -> dict:
        with self._lock:
            if "offer" in (to or "").lower() or "buyer" in (to or "").lower():
                calls0 = len(self.state["calls"])
                for _ in range(OFFER_WAIT_DAYS):
                    self.advance(1)
                    if any(c["kind"] in ("offer", "buyer") for c in self.state["calls"][calls0:]):
                        break
                got = [f"{c['from']}: {c['text']}" for c in self.state["calls"][calls0:]
                       if c["kind"] in ("offer", "buyer")]
                return {"today": self.state["today"], "offers": got or ["no offer came in within a year"],
                        "cash": self.state["cash"]}
            day = self._event_day(to or "")
            out = self.advance((day - self.today).days)
            return {"jumped_to": day.isoformat(), **out}
