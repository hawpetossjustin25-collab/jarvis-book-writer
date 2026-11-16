"""Jarvis's planner and reminder tools (mixed into the ToolBox)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from jarvis_books.errors import NotFound, ValidationError
from jarvis_books.planner import MAX_SCHEDULE_ITEMS, REPEATS, describe_time, parse_when, range_bounds

S = {"type": "STRING"}
I = {"type": "INTEGER"}
B = {"type": "BOOLEAN"}
BOOK = {"type": "STRING", "description": "Book title (partial titles are fine). Omit for the open book."}
WHEN = {"type": "STRING", "description": "When, in the author's local time: 'in 20 minutes', 'at 5 pm', "
        "'tomorrow at 9 am', 'friday 3pm', 'nov 20', or ISO like '2026-11-20T15:30'"}
REPEAT = {"type": "STRING", "enum": list(REPEATS)}


def _decl(name: str, description: str, props: Optional[dict] = None, required: Optional[list[str]] = None) -> dict:
    params: dict = {"type": "OBJECT", "properties": props or {}}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


PLANNER_DECLARATIONS = [
    _decl("add_to_planner", "Add a task, writing session or appointment to the author's planner (the Planner "
          "tab). Omit when for a to-do with no date. remind: also ring a reminder alarm at that time.",
          {"title": S, "when": WHEN, "notes": S, "book": BOOK, "remind": B, "alarm": B}, ["title"]),
    _decl("show_planner", "What is on the author's planner, with their reminders. span: today, tomorrow, this "
          "week, next week, next 7 days, this month, upcoming, all, or a day like 'friday' or 'nov 20'.",
          {"span": S, "include_done": B}),
    _decl("update_planner_item", "Change a planner item: done true/false to check it off, a new title, move it "
          "to another time (when), or new notes.",
          {"item_id": I, "title": S, "when": WHEN, "notes": S, "done": B}, ["item_id"]),
    _decl("delete_planner_item", "Delete a planner item and its reminder.", {"item_id": I}, ["item_id"]),
    _decl("set_reminder", "Set a reminder that rings an alarm (sound, a pop-up on top of every window and a "
          "Windows notification) at a time. repeat: none, daily, weekdays, weekly or monthly. alarm false = a "
          "quiet pop-up only.", {"text": S, "when": WHEN, "repeat": REPEAT, "alarm": B, "book": BOOK},
          ["text", "when"]),
    _decl("list_reminders", "The author's reminders that are ringing now or still to come."),
    _decl("change_reminder", "Change a reminder's time, words, repeat or alarm sound.",
          {"reminder_id": I, "text": S, "when": WHEN, "repeat": REPEAT, "alarm": B}, ["reminder_id"]),
    _decl("cancel_reminder", "Cancel a reminder so it never rings again.", {"reminder_id": I}, ["reminder_id"]),
    _decl("snooze_reminder", "Snooze the ringing alarm for some minutes (default 10).",
          {"reminder_id": {"type": "INTEGER", "description": "omit for the one ringing now"}, "minutes": I}),
    _decl("stop_alarm", "Stop the ringing alarm. A repeating reminder is set for its next time.",
          {"reminder_id": {"type": "INTEGER", "description": "omit to stop every ringing alarm"}}),
    _decl("plan_writing_schedule", "Fill the planner with writing sessions for a book's next unwritten "
          "chapters: chapters_per_day each day from start, at time, for days days (0 = until the book is done). "
          "remind rings a reminder at each session.",
          {"book": BOOK, "start": WHEN, "time": {"type": "STRING", "description": "time of day, e.g. '9 am'"},
           "chapters_per_day": I, "days": I, "remind": B, "weekdays_only": B}),
]
PLANNER_TOOLS = tuple(d["name"] for d in PLANNER_DECLARATIONS)
PLANNER_WORDS = ("remind", "reminder", "alarm", "wake me", "snooze", "planner", "plan my", "my plan", "schedule",
                 "calendar", "agenda", "to-do", "todo", "to do list", "task", "appointment", "deadline",
                 "tomorrow", "today", "tonight", "this week", "next week", "o'clock", " am ", " pm ", "minutes",
                 "every day", "weekday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                 "sunday", "what's on", "check off", "done with", "writing session")


class PlannerTools:
    """Mixed into ToolBox; `self.planner` is set by the Studio."""

    planner = None

    def _plan(self):
        if self.planner is None:
            raise NotFound("the planner is not available here")
        return self.planner

    def _now_text(self) -> str:
        return self._plan().now().strftime("%A %B %d %Y, %I:%M %p")

    def _when(self, when: str) -> tuple[float, bool]:
        try:
            return self._plan().when(when)
        except ValueError as exc:
            raise ValidationError(str(exc)) from None

    def _item_when(self, when: str) -> tuple[float, bool]:
        """An all-day item sits at the start of its day, so it is listed first."""
        due, has_time = self._when(when)
        if not has_time:
            due = datetime.combine(datetime.fromtimestamp(due).date(), datetime.min.time()).timestamp()
        return due, has_time

    def _planner_book(self, book: str) -> Optional[str]:
        return self._book(book).id if book else None

    def _guard(fn):  # noqa: N805 - a decorator for the tool methods below
        def wrapped(self, *a, **kw):
            try:
                return fn(self, *a, **kw)
            except ValidationError as exc:
                return {"error": str(exc), "now": self._now_text()}
        wrapped.__name__ = fn.__name__
        return wrapped

    @_guard
    def _t_add_to_planner(self, title: str, when: str = "", notes: str = "", book: str = "", remind: bool = False,
                          alarm: bool = True) -> dict:
        plan = self._plan()
        due, has_time = self._item_when(when) if when.strip() else (None, False)
        book_id = self._planner_book(book)
        if remind and due is None:
            return {"error": "a reminder needs a time: say when", "now": self._now_text()}
        item = plan.add_item(title, due, notes, book_id, all_day=due is not None and not has_time)
        if remind and not has_time:  # an all-day task is reminded in the morning
            due = self._when(when)[0]
        out: dict = {"added": item.to_dict(plan.clock())}
        if remind:
            try:
                out["reminder"] = plan.add_reminder(title, due, alarm=alarm, plan_item_id=item.id,
                                                    book_id=book_id).to_dict(plan.clock())
            except ValidationError as exc:
                out["reminder_error"] = str(exc)
        return out

    @_guard
    def _t_show_planner(self, span: str = "today", include_done: bool = True) -> dict:
        plan = self._plan()
        try:
            start, end, label = range_bounds(span, plan.now())
        except ValueError as exc:
            raise ValidationError(str(exc)) from None
        now = plan.clock()
        items = plan.items(start, end, include_done=include_done)
        out: dict = {"span": label, "now": self._now_text(), "items": [i.to_dict(now) for i in items[:100]],
                     "reminders": [r.to_dict(now) for r in plan.reminders(start=start, end=end)[:50]]}
        if len(items) > 100:
            out["more_items"] = len(items) - 100
        todo = [i.to_dict(now) for i in plan.items(start=0, end=0, undated=True, include_done=False)[:30]]
        if todo:
            out["to_do_no_date"] = todo
        ringing = plan.ringing()
        if ringing:
            out["ringing_now"] = [r.to_dict(now) for r in ringing]
        return out

    @_guard
    def _t_update_planner_item(self, item_id: int, title: Optional[str] = None, when: Optional[str] = None,
                               notes: Optional[str] = None, done: Optional[bool] = None) -> dict:
        plan = self._plan()
        changes: dict = {}
        if title is not None:
            changes["title"] = title
        if notes is not None:
            changes["notes"] = notes
        if done is not None:
            changes["done"] = done
        if when is not None:
            if when.strip():
                due, has_time = self._item_when(when)
                changes.update(due_at=due, all_day=not has_time)
            else:
                changes.update(due_at=None, all_day=False)
        return {"updated": plan.update_item(item_id, **changes).to_dict(plan.clock())}

    @_guard
    def _t_delete_planner_item(self, item_id: int) -> dict:
        item = self._plan().delete_item(item_id)
        return {"deleted": item.title}

    @_guard
    def _t_set_reminder(self, text: str, when: str, repeat: str = "none", alarm: bool = True, book: str = "") -> dict:
        plan = self._plan()
        due, _ = self._when(when)
        r = plan.add_reminder(text, due, repeat=repeat or "none", alarm=alarm, book_id=self._planner_book(book))
        return {"set": r.to_dict(plan.clock()), "now": self._now_text()}

    @_guard
    def _t_list_reminders(self) -> dict:
        plan = self._plan()
        found = plan.reminders()
        return {"now": self._now_text(), "reminders": [r.to_dict(plan.clock()) for r in found[:50]],
                **({"more": len(found) - 50} if len(found) > 50 else {})}

    @_guard
    def _t_change_reminder(self, reminder_id: int, text: Optional[str] = None, when: Optional[str] = None,
                           repeat: Optional[str] = None, alarm: Optional[bool] = None) -> dict:
        plan = self._plan()
        due = self._when(when)[0] if when and when.strip() else None
        r = plan.update_reminder(reminder_id, text=text, due_at=due, repeat=repeat, alarm=alarm)
        return {"changed": r.to_dict(plan.clock())}

    @_guard
    def _t_cancel_reminder(self, reminder_id: int) -> dict:
        return {"cancelled": self._plan().cancel(reminder_id).text}

    def _ringing_one(self, reminder_id: int) -> int:
        if reminder_id:
            return int(reminder_id)
        ringing = self._plan().ringing()
        if not ringing:
            raise ValidationError("no alarm is ringing right now")
        return ringing[0].id

    @_guard
    def _t_snooze_reminder(self, reminder_id: int = 0, minutes: int = 10) -> dict:
        plan = self._plan()
        r = plan.snooze(self._ringing_one(reminder_id), minutes or 10)
        return {"snoozed": r.text, "rings_again": describe_time(r.due_at, plan.clock())}

    @_guard
    def _t_stop_alarm(self, reminder_id: int = 0) -> dict:
        plan = self._plan()
        targets = [int(reminder_id)] if reminder_id else [r.id for r in plan.ringing()]
        if not targets:
            return {"error": "no alarm is ringing right now"}
        out = []
        for rid in targets:
            r = plan.dismiss(rid)
            out.append({"text": r.text, **({"next": describe_time(r.due_at, plan.clock())}
                                           if r.status == "pending" else {})})
        return {"stopped": out}

    def _schedule_numbers(self, book_id: str) -> list[tuple[int, str]]:
        digest = self.library.chapter_digest(book_id)
        todo = [(c["number"], c["title"]) for c in digest if c["status"] != "accepted"]
        planned = self.writer.planned_chapters(book_id)
        last = digest[-1]["number"] if digest else 0
        todo += [(n, "") for n in range(last + 1, planned + 1)]
        return todo

    @_guard
    def _t_plan_writing_schedule(self, book: str = "", start: str = "tomorrow", time: str = "9 am",
                                 chapters_per_day: int = 1, days: int = 0, remind: bool = True,
                                 weekdays_only: bool = False) -> dict:
        plan = self._plan()
        b = self._book(book)
        per_day = int(chapters_per_day or 1)
        if not 1 <= per_day <= 50:
            raise ValidationError("chapters_per_day must be 1 to 50")
        todo = self._schedule_numbers(b.id)
        if not todo:
            return {"error": f"every chapter of '{b.title}' is already written"}
        try:
            first_day = parse_when(start or "tomorrow", plan.now())[0].date()
            at = parse_when(f"tomorrow at {time or '9 am'}", plan.now())[0]  # only the time of day is used
        except ValueError as exc:
            raise ValidationError(str(exc)) from None
        sessions = min(MAX_SCHEDULE_ITEMS, int(days) if days and days > 0 else -(-len(todo) // per_day))
        made, day, now = [], first_day, plan.clock()
        while len(made) < sessions and todo:
            if weekdays_only and day.weekday() >= 5:
                day += timedelta(days=1)
                continue
            chunk, todo = todo[:per_day], todo[per_day:]
            due = datetime.combine(day, at.time()).timestamp()
            title = (f"Write chapter {chunk[0][0]}" + (f": {chunk[0][1]}" if chunk[0][1] else "")
                     if len(chunk) == 1 else f"Write chapters {chunk[0][0]}-{chunk[-1][0]}")
            item = plan.add_item(f"{title} ({b.title})", due, book_id=b.id)
            if remind and due > now:
                plan.add_reminder(f"Writing time: {title}", due, plan_item_id=item.id, book_id=b.id)
            made.append(item)
            day += timedelta(days=1)
        return {"sessions": len(made), "book": b.title, "first": made[0].to_dict(now), "last": made[-1].to_dict(now),
                "reminders": remind, **({"chapters_left_unplanned": len(todo)} if todo else {})}

    def describe_planner(self, name: str, args: dict) -> Optional[str]:
        if name == "delete_planner_item":
            return f"Delete '{self._plan().get_item(args['item_id']).title}' from the planner"
        if name == "plan_writing_schedule":
            n = int(args.get("chapters_per_day") or 1)
            days = int(args.get("days") or 0)
            span = f"for {days} days" if days > 0 else "until the book is done"
            return (f"Fill the planner with writing sessions for '{args.get('book')}': {n} chapter"
                    f"{'s' if n > 1 else ''} a day from {args.get('start') or 'tomorrow'} at "
                    f"{args.get('time') or '9 am'}, {span}"
                    + (" (weekdays only)" if args.get("weekdays_only") else "")
                    + (", with a reminder for each" if args.get("remind", True) else ""))
        return None

    def planner_status(self) -> dict:
        if self.planner is None:
            return {}
        now = self.planner.clock()
        nxt = self.planner.next_due()
        return {"alarm_ringing": [r.text for r in self.planner.ringing()],
                "next_reminder": f"{nxt.text} ({describe_time(nxt.due_at, now)})" if nxt else None}

    _guard = staticmethod(_guard)

