"""Planner, reminders and the alarm clock, and Jarvis's tools for them."""
import time
from datetime import datetime, timedelta

import pytest

from fakes import FakeGemini
from jarvis_books.assistant import ToolBox
from jarvis_books.assistant.permissions import TOOL_LEVELS
from jarvis_books.assistant.planner_tools import PLANNER_TOOLS
from jarvis_books.errors import NotFound, ValidationError
from jarvis_books.jobs import JobManager
from jarvis_books.memory import StoryIndex
from jarvis_books.planner import Planner, ReminderClock, next_time, parse_when, range_bounds
from jarvis_books.writer import BookWriter

MON = datetime(2026, 11, 16, 16, 41)  # a Monday afternoon


@pytest.mark.parametrize("said, expected, has_time", [
    ("in 20 minutes", datetime(2026, 11, 16, 17, 1), True),
    ("in an hour", datetime(2026, 11, 16, 17, 41), True),
    ("in half an hour", datetime(2026, 11, 16, 17, 11), True),
    ("in 2 hours and 30 minutes", datetime(2026, 11, 16, 19, 11), True),
    ("10 minutes", datetime(2026, 11, 16, 16, 51), True),
    ("at 5", datetime(2026, 11, 16, 17, 0), True),
    ("5pm", datetime(2026, 11, 16, 17, 0), True),
    ("at 4", datetime(2026, 11, 17, 16, 0), True),  # 4 pm already passed today
    ("at 8", datetime(2026, 11, 16, 20, 0), True),
    ("17:30", datetime(2026, 11, 16, 17, 30), True),
    ("today at 9", datetime(2026, 11, 16, 21, 0), True),
    ("tonight", datetime(2026, 11, 16, 20, 0), True),
    ("at 7 tonight", datetime(2026, 11, 16, 19, 0), True),
    ("tomorrow at 9", datetime(2026, 11, 17, 9, 0), True),
    ("tomorrow at 9 pm", datetime(2026, 11, 17, 21, 0), True),
    ("tomorrow at 5", datetime(2026, 11, 17, 17, 0), True),
    ("tomorrow morning", datetime(2026, 11, 17, 9, 0), True),
    ("tomorrow", datetime(2026, 11, 17, 9, 0), False),
    ("friday 3pm", datetime(2026, 11, 20, 15, 0), True),
    ("monday at 8am", datetime(2026, 11, 23, 8, 0), True),  # today's 8 am has passed: next Monday
    ("next monday at 8am", datetime(2026, 11, 23, 8, 0), True),
    ("nov 20 at 3:30 pm", datetime(2026, 11, 20, 15, 30), True),
    ("20 november", datetime(2026, 11, 20, 9, 0), False),
    ("12/25", datetime(2026, 12, 25, 9, 0), False),
    ("march 3", datetime(2027, 3, 3, 9, 0), False),  # already past this year
    ("2026-11-20 15:30", datetime(2026, 11, 20, 15, 30), True),
    ("2026-11-20T15:30", datetime(2026, 11, 20, 15, 30), True),
    ("2026-11-20", datetime(2026, 11, 20, 9, 0), False),
    ("noon", datetime(2026, 11, 17, 12, 0), True),
])
def test_times_the_way_people_say_them(said, expected, has_time):
    assert parse_when(said, MON) == (expected, has_time)


@pytest.mark.parametrize("said", ["", "whenever", "at 25:00", "13 pm", "2026-02-30 10:00", "feb 30"])
def test_unclear_times_are_refused(said):
    with pytest.raises(ValueError):
        parse_when(said, MON)


def test_repeats():
    fri = datetime(2026, 11, 20, 8, 0)
    assert next_time(fri, "daily", fri) == datetime(2026, 11, 21, 8, 0)
    assert next_time(fri, "weekdays", fri) == datetime(2026, 11, 23, 8, 0)
    assert next_time(fri, "weekly", fri) == datetime(2026, 11, 27, 8, 0)
    assert next_time(datetime(2026, 1, 31, 8), "monthly", datetime(2026, 1, 31, 9)) == datetime(2026, 2, 28, 8)
    assert next_time(fri, "daily", fri + timedelta(days=10)) == datetime(2026, 12, 1, 8, 0)  # skips missed days
    assert next_time(fri, "none", fri) is None
    with pytest.raises(ValidationError):
        next_time(fri, "hourly", fri)


class Clock:
    def __init__(self, start=MON):
        self.t = start.timestamp()

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw).total_seconds()


@pytest.fixture
def planner(library):
    return Planner(library.db, clock=Clock())


def test_plan_items_by_day_done_and_deleted(planner, library):
    book = library.create_book("Glass City", "science fiction", "idea")
    at = lambda s: planner.when(s)[0]  # noqa: E731
    a = planner.add_item("Edit chapter 4", at("tomorrow at 10 am"), book_id=book.id)
    b = planner.add_item("Call editor", at("tomorrow at 2 pm"), notes="about the cover")
    planner.add_item("Buy notebook")
    day = planner.items(*range_bounds("tomorrow", planner.now())[:2])
    assert [i.title for i in day] == ["Edit chapter 4", "Call editor"]
    assert [i.title for i in planner.items(start=0, end=0, undated=True)] == ["Buy notebook"]
    assert planner.update_item(a.id, done=True).done
    assert not planner.items(*range_bounds("tomorrow", planner.now())[:2], include_done=False)[0].done
    planner.delete_item(b.id)
    with pytest.raises(NotFound):
        planner.get_item(b.id)
    with pytest.raises(ValidationError):
        planner.add_item("  ")


def test_reminder_rings_when_due_and_snooze_and_stop(planner):
    events = []
    planner.subscribe(lambda e, p: events.append(e))
    r = planner.add_reminder("Stretch", planner.when("in 20 minutes")[0])
    assert planner.fire_due() == []
    planner.clock.advance(minutes=20)
    fired = planner.fire_due()
    assert [f.text for f in fired] == ["Stretch"] and planner.ringing()[0].id == r.id and "ringing" in events
    assert planner.fire_due() == []  # rings once, not on every check
    planner.snooze(r.id, 10)
    assert planner.ringing() == [] and planner.get_reminder(r.id).status == "pending"
    planner.clock.advance(minutes=9)
    assert planner.fire_due() == []
    planner.clock.advance(minutes=1)
    assert planner.fire_due()[0].id == r.id
    assert planner.dismiss(r.id).status == "done" and planner.reminders() == []


def test_repeating_reminder_comes_back_at_its_own_time_even_after_a_snooze(planner):
    r = planner.add_reminder("Write", planner.when("tomorrow at 8 am")[0], repeat="weekdays")
    planner.clock.advance(hours=15, minutes=19)  # Tue 8:00
    planner.fire_due()
    planner.snooze(r.id, 30)
    planner.clock.advance(minutes=30)
    planner.fire_due()
    nxt = planner.dismiss(r.id)
    assert nxt.status == "pending" and datetime.fromtimestamp(nxt.due_at) == datetime(2026, 11, 18, 8, 0)


def test_past_and_bad_reminders(planner):
    with pytest.raises(ValidationError, match="already passed"):
        planner.add_reminder("late", planner.clock() - 3600)
    with pytest.raises(ValidationError):
        planner.add_reminder("", planner.clock() + 60)
    with pytest.raises(ValidationError):
        planner.add_reminder("x", planner.clock() + 60, repeat="hourly")
    daily = planner.add_reminder("pills", planner.clock() - 3600, repeat="daily")  # set for its next time
    assert datetime.fromtimestamp(daily.due_at) == datetime(2026, 11, 17, 15, 41)


def test_reminders_missed_while_closed_ring_on_start_and_say_how_late(library):
    clock = Clock()
    planner = Planner(library.db, clock=clock)
    planner.add_reminder("Send the draft", planner.when("at 5 pm")[0])
    clock.advance(hours=3)  # the app was closed
    reopened = Planner(library.db, clock=clock)
    fired = reopened.fire_due()
    assert fired[0].text == "Send the draft" and fired[0].to_dict(clock())["missed_by_minutes"] == 161


def test_finishing_a_task_silences_its_reminder(planner):
    due = planner.when("tomorrow at 10 am")[0]
    item = planner.add_item("Edit chapter 4", due)
    planner.add_reminder("Edit chapter 4", due, plan_item_id=item.id)
    planner.update_item(item.id, done=True)
    assert planner.reminders() == []


def test_clock_thread_rings_reminders(library):
    planner = Planner(library.db)
    rang = []
    planner.subscribe(lambda e, p: rang.append(p.text) if e == "ringing" else None)
    planner.add_reminder("now", time.time())
    clock = ReminderClock(planner, interval=0.05)
    clock.start()
    try:
        deadline = time.time() + 5
        while not rang and time.time() < deadline:
            time.sleep(0.02)
    finally:
        clock.stop()
    assert rang == ["now"] and not clock.running


# ---- Jarvis's tools ----------------------------------------------------------------------------------
@pytest.fixture
def tools(library, make_engine):
    engine = make_engine(FakeGemini())
    index = StoryIndex(library, engine)
    jobs = JobManager(library, workers=1)
    box = ToolBox(library, BookWriter(library, engine, index), index, jobs)
    box.planner = Planner(library.db, clock=Clock())
    yield box
    jobs.shutdown(wait=True)


def test_every_planner_tool_has_a_permission_and_comes_up_when_asked(tools):
    assert set(PLANNER_TOOLS) <= set(TOOL_LEVELS)
    for ask in ("remind me in 20 minutes to stretch", "what's on my plan this week", "snooze it",
                "stop the alarm", "add edit chapter 3 to my to-do list for friday"):
        names = {d["name"] for d in tools.select(ask)}
        assert {"set_reminder", "show_planner", "stop_alarm", "add_to_planner"} <= names, ask


def test_jarvis_sets_and_lists_reminders_without_asking(tools):
    out = tools.call("set_reminder", {"text": "stretch", "when": "in 20 minutes"})
    assert out["set"]["when"] == "today 5:01 PM" and out["set"]["alarm"] is True
    weekly = tools.call("set_reminder", {"text": "back up", "when": "friday 6pm", "repeat": "weekly"})
    assert weekly["set"]["repeat"] == "weekly"
    listed = tools.call("list_reminders")["reminders"]
    assert [r["text"] for r in listed] == ["stretch", "back up"]
    assert "Monday November 16 2026" in tools.call("list_reminders")["now"]
    bad = tools.call("set_reminder", {"text": "x", "when": "whenever"})
    assert "couldn't tell" in bad["error"] and "now" in bad
    assert tools.call("set_reminder", {"text": "x", "when": "in 5 minutes", "repeat": "hourly"})["error"]


def test_jarvis_snoozes_and_stops_the_ringing_alarm(tools):
    plan = tools.planner
    tools.call("set_reminder", {"text": "stretch", "when": "in 1 minute"})
    assert tools.call("stop_alarm")["error"] == "no alarm is ringing right now"
    plan.clock.advance(minutes=1)
    plan.fire_due()
    assert tools.call("app_status")["alarm_ringing"] == ["stretch"]
    assert tools.call("snooze_reminder", {"minutes": 5})["rings_again"] == "today 4:47 PM"
    plan.clock.advance(minutes=5)
    plan.fire_due()
    assert tools.call("stop_alarm")["stopped"] == [{"text": "stretch"}]
    assert plan.ringing() == []


def test_planner_items_through_jarvis(tools, library):
    book = library.create_book("Glass City", "science fiction", "idea")
    tools.current_book_id = book.id
    added = tools.call("add_to_planner", {"title": "Edit chapter 4", "when": "friday at 10 am", "remind": True,
                                          "book": "Glass"})
    assert added["added"]["when"] == "Fri Nov 20 10:00 AM" and added["reminder"]["when"] == "Fri Nov 20 10:00 AM"
    tools.call("add_to_planner", {"title": "Buy notebook"})
    tools.call("add_to_planner", {"title": "Cover meeting", "when": "friday"})
    friday = tools.call("show_planner", {"span": "friday"})
    assert [i["title"] for i in friday["items"]] == ["Cover meeting", "Edit chapter 4"]  # all day comes first
    assert friday["items"][0]["when"] == "Fri Nov 20" and friday["to_do_no_date"][0]["title"] == "Buy notebook"
    assert len(friday["reminders"]) == 1
    item_id = added["added"]["item_id"]
    moved = tools.call("update_planner_item", {"item_id": item_id, "when": "saturday at 11 am"})
    assert moved["updated"]["when"] == "Sat Nov 21 11:00 AM"
    assert tools.call("list_reminders")["reminders"][0]["when"] == "Sat Nov 21 11:00 AM"  # the reminder moved too
    assert tools.call("update_planner_item", {"item_id": item_id, "done": True})["updated"]["done"]
    asked = tools.call("delete_planner_item", {"item_id": item_id})
    assert asked["needs_approval"] and asked["asking"] == "Delete 'Edit chapter 4' from the planner"
    tools.gate.answer(True)
    with pytest.raises(NotFound):
        tools.planner.get_item(item_id)
    assert "a reminder needs a time" in tools.call("add_to_planner", {"title": "x", "remind": True})["error"]


def test_writing_schedule_covers_the_next_unwritten_chapters(tools, library):
    book = library.create_book("Glass City", "science fiction", "idea")
    library.set_outline(book.id, [{"number": n, "title": f"Part {n}", "outline": "o"} for n in range(1, 8)])
    library.update_chapter(library.get_chapter(book.id, 1).id, status="accepted")
    asked = tools.call("plan_writing_schedule", {"book": "Glass", "start": "tomorrow", "time": "9 am",
                                                 "chapters_per_day": 2, "weekdays_only": True})
    assert asked["needs_approval"] and "2 chapters a day from tomorrow at 9 am, until the book is done" \
        in asked["asking"]
    out = tools.run("plan_writing_schedule", {"book": "Glass", "start": "friday", "time": "9:30 pm",
                                              "chapters_per_day": 2, "weekdays_only": True})
    assert out["sessions"] == 3
    assert out["first"] == {"item_id": out["first"]["item_id"], "title": "Write chapters 2-3 (Glass City)",
                            "when": "Fri Nov 20 9:30 PM", "done": False, "book_id": book.id}
    assert out["last"]["title"] == "Write chapters 6-7 (Glass City)" and out["last"]["when"] == "Tue Nov 24 9:30 PM"
    assert len(tools.planner.reminders()) == 3
    tools.writer.set_length(book.id, 3000)  # a long serial: sessions keep going past the outline, capped at a year
    long = tools.run("plan_writing_schedule", {"book": "Glass", "chapters_per_day": 1, "remind": False})
    assert long["sessions"] == 366 and long["chapters_left_unplanned"] == 3000 - 1 - 366
    assert long["first"]["title"] == "Write chapter 2: Part 2 (Glass City)"


def test_jarvis_is_told_the_date_and_time():
    from jarvis_books.assistant.chat import now_line
    assert datetime.now().strftime("%A %B") in now_line()
