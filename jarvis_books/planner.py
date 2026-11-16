"""The author's planner: dated tasks and writing sessions, and reminders that ring an alarm when due.

Times are stored as Unix timestamps and shown in the computer's local time. `parse_when` turns what the author
(or Jarvis) says, like "tomorrow at 9", "in 20 minutes" or "2026-11-20 15:30", into a local datetime.
A ReminderClock thread checks for due reminders every few seconds and tells listeners (the window rings the
alarm). Reminders that came due while the app was closed ring as soon as it starts.
"""
from __future__ import annotations

import calendar
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from jarvis_books.errors import NotFound, ValidationError
from jarvis_books.storage.db import Database

log = logging.getLogger(__name__)

REPEATS = ("none", "daily", "weekdays", "weekly", "monthly")
SNOOZE_MINUTES = (5, 10, 15, 30, 60)
MAX_SCHEDULE_ITEMS = 366
LATE_GRACE_S = 60  # a reminder set for "now" or a moment ago still counts

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")
_PARTS = {"morning": 9, "noon": 12, "midday": 12, "afternoon": 15, "evening": 18, "tonight": 20, "night": 20,
          "midnight": 0}
_UNITS = {"min": 60, "minute": 60, "minutes": 60, "mins": 60, "m": 60, "hour": 3600, "hours": 3600, "hr": 3600,
          "hrs": 3600, "h": 3600, "day": 86400, "days": 86400, "week": 604800, "weeks": 604800,
          "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1}
_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
            "forty-five": 45, "sixty": 60, "ninety": 90, "half": 0.5, "a couple of": 2, "a few": 3}
DEFAULT_HOUR = 9


# ---- understanding times ------------------------------------------------------------------------
def parse_when(text: str, now: Optional[datetime] = None) -> tuple[datetime, bool]:
    """(local naive datetime, whether a time of day was given). Raises ValueError when it can't tell."""
    now = (now or datetime.now()).replace(microsecond=0)
    raw = (text or "").strip()
    t = re.sub(r"\s+", " ", raw.lower()).strip(" .")
    if not t:
        raise ValueError("no time given")
    iso = _iso(raw)
    if iso is not None:
        return iso
    if t == "now":
        return now, True
    rel = _relative(t)
    if rel is not None:
        return now + timedelta(seconds=rel), True

    day: Optional[date] = None
    rest = t
    m = re.search(r"\b(day after tomorrow|today|tonight|tomorrow|tmrw|tmr)\b", rest)
    if m:
        word = m.group(1)
        day = now.date() + timedelta(days={"day after tomorrow": 2, "tomorrow": 1, "tmrw": 1, "tmr": 1}.get(word, 0))
        rest = rest.replace(word, " " if word != "tonight" else " tonight ", 1)
    if day is None:
        m = re.search(r"\b(next |this |on )?(" + "|".join(_WEEKDAYS) + r"|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b",
                      rest)
        if m:
            target = next(i for i, d in enumerate(_WEEKDAYS) if d.startswith(m.group(2)[:3]))
            ahead = (target - now.weekday()) % 7
            if (m.group(1) or "").strip() == "next" and ahead == 0:
                ahead = 7
            day = now.date() + timedelta(days=ahead)
            rest = rest[:m.start()] + " " + rest[m.end():]
            weekday_day = True
        else:
            weekday_day = False
    else:
        weekday_day = False
    if day is None:
        day, rest = _month_day(rest, now)

    hm = _clock(rest)
    if day is None and hm is None:
        raise ValueError(f"I couldn't tell when {raw!r} is; say a date and time like 'tomorrow at 9 am', "
                         "'in 20 minutes' or '2026-11-20 15:30'")
    if hm is None:
        return datetime.combine(day, datetime.min.time()).replace(hour=DEFAULT_HOUR), False
    hour, minute, meridiem = hm
    base = day or now.date()
    if meridiem or not 1 <= hour <= 12:
        hours = [hour]
    elif hour == 12:
        hours = [12]
    elif hour <= 6:  # "at 5" means the afternoon: nobody sets a 5 am writing reminder without saying so
        hours = [hour + 12]
    elif day is not None and day != now.date():
        hours = [hour]  # "tomorrow at 9" is the morning
    else:
        hours = [hour, hour + 12]  # "at 8" today is whichever 8 o'clock comes next
    for h in hours:
        when = datetime.combine(base, datetime.min.time()).replace(hour=h % 24, minute=minute)
        if when > now or base != now.date():
            return when, True
    when = datetime.combine(base, datetime.min.time()).replace(hour=hours[0] % 24, minute=minute)
    return when + timedelta(days=7 if weekday_day else 1), True


_PM_HINT = re.compile(r"\b(afternoon|evening|tonight|night)\b")


def _iso(raw: str) -> Optional[tuple[datetime, bool]]:
    s = raw.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return None
    has_time = bool(re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}", s))
    s = s.replace("Z", "+00:00").replace(" ", "T", 1) if has_time else s[:10]
    try:
        value = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"{raw!r} is not a real date and time") from None
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    if not has_time:
        value = value.replace(hour=DEFAULT_HOUR)
    return value.replace(microsecond=0), has_time


def _relative(t: str) -> Optional[float]:
    if not t.startswith("in ") and not re.match(r"^\d+\s*(m|min|mins|minutes?|h|hrs?|hours?)$", t):
        return None
    body = t[3:] if t.startswith("in ") else t
    body = body.replace("half an hour", "30 minutes").replace("half hour", "30 minutes").replace("an hour and a half",
                                                                                                  "90 minutes")
    total, found = 0.0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?|a couple of|a few|forty-five|[a-z]+)\s*(seconds?|secs?|minutes?|"
                                r"mins?|hours?|hrs?|days?|weeks?|[mhs])\b", body):
        value = float(num) if num[0].isdigit() else _NUMBERS.get(num)
        if value is None:
            continue
        total += value * _UNITS[unit]
        found = True
    return total if found and total > 0 else None


def _month_day(rest: str, now: datetime) -> tuple[Optional[date], str]:
    months = "|".join(m[:3] + r"[a-z]*" for m in _MONTHS)
    pattern = (rf"\b(?:(?P<m1>{months})\.? (?P<d1>\d{{1,2}})(?:st|nd|rd|th)?|(?P<d2>\d{{1,2}})(?:st|nd|rd|th)? "
               rf"(?:of )?(?P<m2>{months}))(?:,? (?P<y>\d{{4}}))?\b")
    m = re.search(pattern, rest)
    if not m:
        m2 = re.search(r"\b(?P<mo>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\b", rest)
        if not m2:
            return None, rest
        month, dayn, year = int(m2.group("mo")), int(m2.group("d")), m2.group("y")
        span = m2.span()
    else:
        name = m.group("m1") or m.group("m2")
        month = next(i + 1 for i, full in enumerate(_MONTHS) if full.startswith(name[:3]))
        dayn, year = int(m.group("d1") or m.group("d2")), m.group("y")
        span = m.span()
    y = int(year) + (2000 if year and len(year) == 2 else 0) if year else now.year
    try:
        found = date(y, month, dayn)
    except ValueError:
        raise ValueError(f"{month}/{dayn} is not a real date") from None
    if not year and found < now.date():
        found = date(y + 1, month, dayn)
    return found, rest[:span[0]] + " " + rest[span[1]:]


def _clock(rest: str) -> Optional[tuple[int, int, str]]:
    m = re.search(r"\b(?:at |@ ?|by |around )?(\d{1,2})(?::(\d{2}))? ?(a\.?m\.?|p\.?m\.?)(?=\W|$)", rest)
    if m:
        hour, minute, mer = int(m.group(1)), int(m.group(2) or 0), m.group(3)[0]
        if not 1 <= hour <= 12 or minute > 59:
            raise ValueError("that isn't a real time of day")
        return (hour % 12) + (12 if mer == "p" else 0), minute, mer
    m = re.search(r"\b(?:at |@ ?|by |around )?(\d{1,2}):(\d{2})\b", rest)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            raise ValueError("that isn't a real time of day")
        return hour, minute, ""
    m = re.search(r"\b(?:at|@|by|around) (\d{1,2})\b(?! ?(?:st|nd|rd|th|minutes?|hours?|days?))", rest)
    if m and int(m.group(1)) <= 23:
        hour = int(m.group(1))
        if _PM_HINT.search(rest) and 1 <= hour < 12:
            return hour + 12, 0, "p"
        if re.search(r"\bmorning\b", rest) and hour <= 12:
            return hour % 12, 0, "a"
        return hour, 0, ""
    for word, hour in _PARTS.items():
        if re.search(rf"\b{word}\b", rest):
            return hour, 0, "x"
    return None


def next_time(scheduled: datetime, repeat: str, after: datetime) -> Optional[datetime]:
    """The next time a repeating reminder rings after `after`; None when it doesn't repeat."""
    if repeat not in REPEATS:
        raise ValidationError(f"repeat must be one of {', '.join(REPEATS)}")
    if repeat == "none":
        return None
    when = scheduled
    for _ in range(100000):
        if repeat == "daily":
            when += timedelta(days=1)
        elif repeat == "weekdays":
            when += timedelta(days=1)
            while when.weekday() >= 5:
                when += timedelta(days=1)
        elif repeat == "weekly":
            when += timedelta(days=7)
        else:
            month = when.month % 12 + 1
            year = when.year + (1 if month == 1 else 0)
            dayn = min(scheduled.day, calendar.monthrange(year, month)[1])
            when = when.replace(year=year, month=month, day=dayn)
        if when > after:
            return when
    raise ValidationError("could not work out the next time")


def describe_time(ts: Optional[float], now: Optional[float] = None, all_day: bool = False) -> str:
    """'today 5:00 PM', 'tomorrow 9:00 AM', 'Fri Nov 20 3:30 PM', 'Nov 20' (all day)."""
    if ts is None:
        return "no date"
    when = datetime.fromtimestamp(ts)
    today = datetime.fromtimestamp(now if now is not None else time.time()).date()
    delta = (when.date() - today).days
    day = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(delta)
    if day is None:
        day = when.strftime("%a %b %d" if abs(delta) < 300 else "%a %b %d %Y").replace(" 0", " ")
    if all_day:
        return day
    return f"{day} {when.strftime('%I:%M %p').lstrip('0')}"


# ---- records ------------------------------------------------------------------------------------
@dataclass
class PlanItem:
    id: int
    title: str
    notes: str
    book_id: Optional[str]
    due_at: Optional[float]
    all_day: bool
    done_at: Optional[float]
    created_at: float

    @property
    def done(self) -> bool:
        return self.done_at is not None

    @classmethod
    def from_row(cls, r) -> "PlanItem":
        return cls(r["id"], r["title"], r["notes"], r["book_id"], r["due_at"], bool(r["all_day"]), r["done_at"],
                   r["created_at"])

    def to_dict(self, now: Optional[float] = None) -> dict:
        return {"item_id": self.id, "title": self.title, "when": describe_time(self.due_at, now, self.all_day),
                "done": self.done, **({"notes": self.notes} if self.notes else {}),
                **({"book_id": self.book_id} if self.book_id else {})}


@dataclass
class Reminder:
    id: int
    text: str
    scheduled_at: float
    due_at: float
    repeat: str
    alarm: bool
    status: str  # pending | ringing | done | cancelled
    plan_item_id: Optional[int]
    book_id: Optional[str]
    fired_at: Optional[float]
    created_at: float

    @classmethod
    def from_row(cls, r) -> "Reminder":
        return cls(r["id"], r["text"], r["scheduled_at"], r["due_at"], r["repeat"], bool(r["alarm"]), r["status"],
                   r["plan_item_id"], r["book_id"], r["fired_at"], r["created_at"])

    def late_minutes(self, now: float) -> int:
        """How long past its time it rang (it came due while the app was closed)."""
        return max(0, int(((self.fired_at or now) - self.due_at) // 60))

    def to_dict(self, now: Optional[float] = None) -> dict:
        now = now if now is not None else time.time()
        out = {"reminder_id": self.id, "text": self.text, "when": describe_time(self.due_at, now),
               "repeat": self.repeat, "alarm": self.alarm, "status": self.status}
        if self.status == "ringing" and self.late_minutes(now) >= 2:
            out["missed_by_minutes"] = self.late_minutes(now)
        return out


Listener = Callable[[str, Any], None]


class Planner:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db = db
        self.clock = clock
        self._listeners: list[Listener] = []
        self._lock = threading.RLock()

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def _emit(self, event: str, payload: Any = None) -> None:
        for listener in list(self._listeners):
            try:
                listener(event, payload)
            except Exception:
                log.exception("planner listener failed")

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock()).replace(microsecond=0)

    def when(self, text: str) -> tuple[float, bool]:
        """A timestamp for what the author said, and whether it included a time of day."""
        value, has_time = parse_when(text, self.now())
        return value.timestamp(), has_time

    # ---- plan items -----------------------------------------------------------------------------
    def add_item(self, title: str, due_at: Optional[float] = None, notes: str = "", book_id: Optional[str] = None,
                 all_day: bool = False) -> PlanItem:
        title = (title or "").strip()
        if not title:
            raise ValidationError("a plan item needs a title")
        now = self.clock()
        item_id = self.db.execute(
            "INSERT INTO plan_items (title, notes, book_id, due_at, all_day, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", (title[:300], (notes or "").strip(), book_id, due_at, int(all_day), now, now)
        ).lastrowid
        self._emit("plan", item_id)
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> PlanItem:
        row = self.db.query_one("SELECT * FROM plan_items WHERE id=?", (int(item_id),))
        if row is None:
            raise NotFound(f"no plan item {item_id}")
        return PlanItem.from_row(row)

    def update_item(self, item_id: int, **changes: Any) -> PlanItem:
        item = self.get_item(item_id)
        allowed = {"title", "notes", "book_id", "due_at", "all_day"}
        bad = set(changes) - allowed - {"done"}
        if bad:
            raise ValidationError(f"can't change {', '.join(sorted(bad))}")
        fields = {k: v for k, v in changes.items() if k in allowed}
        if "title" in fields and not (fields["title"] or "").strip():
            raise ValidationError("a plan item needs a title")
        if "done" in changes:
            fields["done_at"] = (item.done_at or self.clock()) if changes["done"] else None
        if fields:
            fields["updated_at"] = self.clock()
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE plan_items SET {sets} WHERE id=?", (*fields.values(), item.id))
            if changes.get("done"):  # a finished task's reminder no longer needs to ring
                self.db.execute("UPDATE reminders SET status='done' WHERE plan_item_id=? AND status='pending'",
                                (item.id,))
            elif "due_at" in fields and fields["due_at"] is not None:
                self.db.execute("UPDATE reminders SET scheduled_at=?, due_at=? WHERE plan_item_id=? AND "
                                "status='pending'", (fields["due_at"], fields["due_at"], item.id))
            self._emit("plan", item.id)
        return self.get_item(item.id)

    def delete_item(self, item_id: int) -> PlanItem:
        item = self.get_item(item_id)
        self.db.execute("DELETE FROM plan_items WHERE id=?", (item.id,))
        self._emit("plan", item.id)
        self._emit("reminder", None)
        return item

    def items(self, start: Optional[float] = None, end: Optional[float] = None, undated: bool = False,
              include_done: bool = True) -> list[PlanItem]:
        """Items due in [start, end) (all dated items when both are None), plus undated ones if asked."""
        where, params = [], []
        dated = "due_at IS NOT NULL"
        if start is not None:
            dated += " AND due_at >= ?"
            params.append(start)
        if end is not None:
            dated += " AND due_at < ?"
            params.append(end)
        where.append(f"({dated})" + (" OR due_at IS NULL" if undated else ""))
        sql = "SELECT * FROM plan_items WHERE (" + " ".join(where) + ")"
        if not include_done:
            sql += " AND done_at IS NULL"
        sql += " ORDER BY due_at IS NULL, due_at, id"
        return [PlanItem.from_row(r) for r in self.db.query(sql, tuple(params))]

    def busy_days(self, start: float, end: float) -> set[date]:
        rows = self.db.query("SELECT due_at FROM plan_items WHERE due_at >= ? AND due_at < ?", (start, end))
        return {datetime.fromtimestamp(r[0]).date() for r in rows}

    # ---- reminders ------------------------------------------------------------------------------
    def add_reminder(self, text: str, due_at: float, repeat: str = "none", alarm: bool = True,
                     plan_item_id: Optional[int] = None, book_id: Optional[str] = None) -> Reminder:
        text = (text or "").strip()
        if not text:
            raise ValidationError("what should the reminder say?")
        if repeat not in REPEATS:
            raise ValidationError(f"repeat must be one of {', '.join(REPEATS)}")
        now = self.clock()
        if due_at < now - LATE_GRACE_S:
            if repeat == "none":
                raise ValidationError(f"{describe_time(due_at, now)} has already passed")
            due_at = next_time(datetime.fromtimestamp(due_at), repeat, datetime.fromtimestamp(now)).timestamp()
        rid = self.db.execute(
            "INSERT INTO reminders (text, scheduled_at, due_at, repeat, alarm, status, plan_item_id, book_id, "
            "created_at) VALUES (?,?,?,?,?,'pending',?,?,?)",
            (text[:500], due_at, due_at, repeat, int(alarm), plan_item_id, book_id, now)).lastrowid
        self._emit("reminder", rid)
        return self.get_reminder(rid)

    def get_reminder(self, reminder_id: int) -> Reminder:
        row = self.db.query_one("SELECT * FROM reminders WHERE id=?", (int(reminder_id),))
        if row is None:
            raise NotFound(f"no reminder {reminder_id}")
        return Reminder.from_row(row)

    def reminders(self, statuses: tuple[str, ...] = ("ringing", "pending"), start: Optional[float] = None,
                  end: Optional[float] = None) -> list[Reminder]:
        marks = ",".join("?" for _ in statuses)
        sql, params = f"SELECT * FROM reminders WHERE status IN ({marks})", list(statuses)
        if start is not None:
            sql += " AND due_at >= ?"
            params.append(start)
        if end is not None:
            sql += " AND due_at < ?"
            params.append(end)
        sql += " ORDER BY status != 'ringing', due_at, id"
        return [Reminder.from_row(r) for r in self.db.query(sql, tuple(params))]

    def ringing(self) -> list[Reminder]:
        return self.reminders(("ringing",))

    def fire_due(self) -> list[Reminder]:
        """Start ringing every pending reminder whose time has come. Returns the ones that started now."""
        with self._lock:
            now = self.clock()
            rows = self.db.query("SELECT id FROM reminders WHERE status='pending' AND due_at <= ? ORDER BY due_at",
                                 (now,))
            fired = []
            for row in rows:
                self.db.execute("UPDATE reminders SET status='ringing', fired_at=? WHERE id=? AND status='pending'",
                                (now, row[0]))
                fired.append(self.get_reminder(row[0]))
        for reminder in fired:
            self._emit("ringing", reminder)
        return fired

    def next_due(self) -> Optional[Reminder]:
        found = self.reminders(("pending",))
        return found[0] if found else None

    def snooze(self, reminder_id: int, minutes: float = 10) -> Reminder:
        r = self.get_reminder(reminder_id)
        if r.status not in ("ringing", "pending"):
            raise ValidationError("that reminder is already finished")
        minutes = float(minutes)
        if not 1 <= minutes <= 24 * 60:
            raise ValidationError("snooze between 1 minute and 24 hours")
        self.db.execute("UPDATE reminders SET status='pending', due_at=? WHERE id=?",
                        (self.clock() + minutes * 60, r.id))
        self._emit("stopped", r.id)
        return self.get_reminder(r.id)

    def dismiss(self, reminder_id: int) -> Reminder:
        """Stop a ringing reminder. A repeating one is set for its next time; a one-off is finished."""
        r = self.get_reminder(reminder_id)
        if r.status in ("done", "cancelled"):
            return r
        nxt = next_time(datetime.fromtimestamp(r.scheduled_at), r.repeat, self.now())
        if nxt is None:
            self.db.execute("UPDATE reminders SET status='done' WHERE id=?", (r.id,))
        else:
            ts = nxt.timestamp()
            self.db.execute("UPDATE reminders SET status='pending', scheduled_at=?, due_at=? WHERE id=?",
                            (ts, ts, r.id))
        self._emit("stopped", r.id)
        return self.get_reminder(r.id)

    def cancel(self, reminder_id: int) -> Reminder:
        r = self.get_reminder(reminder_id)
        self.db.execute("UPDATE reminders SET status='cancelled' WHERE id=?", (r.id,))
        self._emit("stopped", r.id)
        return self.get_reminder(r.id)

    def update_reminder(self, reminder_id: int, text: Optional[str] = None, due_at: Optional[float] = None,
                        repeat: Optional[str] = None, alarm: Optional[bool] = None) -> Reminder:
        r = self.get_reminder(reminder_id)
        if r.status in ("done", "cancelled"):
            raise ValidationError("that reminder is already finished")
        fields: dict = {}
        if text is not None:
            if not text.strip():
                raise ValidationError("what should the reminder say?")
            fields["text"] = text.strip()[:500]
        if repeat is not None:
            if repeat not in REPEATS:
                raise ValidationError(f"repeat must be one of {', '.join(REPEATS)}")
            fields["repeat"] = repeat
        if alarm is not None:
            fields["alarm"] = int(alarm)
        if due_at is not None:
            if due_at < self.clock() - LATE_GRACE_S:
                raise ValidationError(f"{describe_time(due_at, self.clock())} has already passed")
            fields.update(scheduled_at=due_at, due_at=due_at, status="pending")
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE reminders SET {sets} WHERE id=?", (*fields.values(), r.id))
            self._emit("reminder", r.id)
        return self.get_reminder(r.id)


class ReminderClock:
    """Rings due reminders: checks every `interval` seconds on its own thread until stopped."""

    def __init__(self, planner: Planner, interval: float = 5.0):
        self.planner = planner
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="reminder-clock", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.planner.fire_due()
            except Exception:
                log.exception("reminder check failed")
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


def day_bounds(day: date) -> tuple[float, float]:
    start = datetime.combine(day, datetime.min.time())
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def range_bounds(span: str, now: datetime) -> tuple[Optional[float], Optional[float], str]:
    """Start/end timestamps and a label for 'today', 'tomorrow', 'this week', 'next 7 days', 'this month',
    'all' or a date the author named."""
    s = (span or "today").strip().lower()
    today = now.date()
    if s in ("all", "everything", "anything", "upcoming"):
        return (None if s != "upcoming" else datetime.combine(today, datetime.min.time()).timestamp()), None, s
    if s in ("today", "tonight"):
        return (*day_bounds(today), "today")
    if s == "tomorrow":
        return (*day_bounds(today + timedelta(days=1)), "tomorrow")
    if s in ("this week", "week"):
        start = today - timedelta(days=today.weekday())
        return (datetime.combine(start, datetime.min.time()).timestamp(),
                datetime.combine(start + timedelta(days=7), datetime.min.time()).timestamp(), "this week")
    if s in ("next week",):
        start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        return (datetime.combine(start, datetime.min.time()).timestamp(),
                datetime.combine(start + timedelta(days=7), datetime.min.time()).timestamp(), "next week")
    if s in ("next 7 days", "the next 7 days", "7 days", "coming week"):
        return (datetime.combine(today, datetime.min.time()).timestamp(),
                datetime.combine(today + timedelta(days=7), datetime.min.time()).timestamp(), "next 7 days")
    if s in ("this month", "month"):
        start = today.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
        return (datetime.combine(start, datetime.min.time()).timestamp(),
                datetime.combine(end, datetime.min.time()).timestamp(), "this month")
    when, _ = parse_when(s, now)
    return (*day_bounds(when.date()), when.strftime("%a %b %d").replace(" 0", " "))


def utc_offset_label() -> str:
    offset = datetime.now(timezone.utc).astimezone().utcoffset() or timedelta()
    hours = offset.total_seconds() / 3600
    return f"UTC{'+' if hours >= 0 else '-'}{abs(hours):g}"
