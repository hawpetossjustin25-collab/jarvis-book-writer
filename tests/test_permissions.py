"""The yes/no gate: Jarvis can't change anything important without the author's own yes."""
from __future__ import annotations

import pytest

from jarvis_books.assistant.chat import Assistant
from jarvis_books.assistant.permissions import (CHANGE, NEEDS_YES, OUTSIDE, TOOL_LEVELS, PermissionGate, heard_answer,
                                                level_of)
from jarvis_books.assistant.tools import DECLARATIONS, ToolBox


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def box(library):
    from jarvis_books.jobs import JobManager
    clock = Clock()
    gate = PermissionGate(library, clock=clock)
    tools = ToolBox(library, writer=None, index=None, jobs=JobManager(library, workers=1), gate=gate)
    b = library.create_book("Midnight Rings", "fantasy", "Rings that open doors.")
    library.set_outline(b.id, [{"number": 1, "title": "Doors", "outline": "x"}])
    tools.current_book_id = b.id
    return tools, gate, clock, b


def outline_title(library, book_id):
    return library.get_chapter(book_id, 1).title


def test_every_tool_has_a_level_and_unknown_tools_are_most_sensitive():
    assert {d["name"] for d in DECLARATIONS} == set(TOOL_LEVELS)
    assert level_of("rm_rf_everything") == "system"


@pytest.mark.parametrize("text,answer", [
    ("yes", True), ("Yeah go ahead", True), ("okay", True), ("do it", True), ("no", False), ("nope", False),
    ("don't do that", False), ("wait", False), ("I know the ending", None), ("yes... no, wait", None), ("", None),
])
def test_heard_answer(text, answer):
    assert heard_answer(text) is answer


def test_reading_runs_but_changes_wait_for_yes(box, library):
    tools, gate, _, b = box
    assert tools.call("list_books")["books"]
    asked = tools.call("change_outline", {"chapter": 1, "title": "Keys"})
    assert asked["needs_approval"] and "chapter 1" in asked["asking"] and "Midnight Rings" in asked["asking"]
    assert outline_title(library, b.id) == "Doors"  # nothing happened yet
    out = gate.answer(True, asked["request_id"])
    assert out["status"] == "approved" and outline_title(library, b.id) == "Keys"
    log = library.list_permissions()
    assert log[0]["status"] == "approved" and log[0]["via"] == "button" and log[0]["tool"] == "change_outline"


def test_no_leaves_everything_as_it_was(box, library):
    tools, gate, _, b = box
    tools.call("change_outline", {"chapter": 1, "title": "Keys"})
    assert gate.answer(False)["status"] == "denied"
    assert outline_title(library, b.id) == "Doors" and library.list_permissions()[0]["status"] == "denied"


def test_gemini_cannot_approve_itself(box, library):
    tools, gate, _, b = box
    assert "invalid" in tools.call("change_outline", {"chapter": 1, "title": "Keys", "confirmed": True})["error"]
    assert gate.pending is None
    tools.call("change_outline", {"chapter": 1, "title": "Keys"})
    assert gate.answer(True, request_id=gate.pending.id + 99) is None  # a yes for some other request
    assert outline_title(library, b.id) == "Doors" and gate.pending is not None


def test_a_yes_is_used_once_and_expires(box, library):
    tools, gate, clock, b = box
    rid = tools.call("change_outline", {"chapter": 1, "title": "Keys"})["request_id"]
    clock.t += 121
    assert gate.answer(True, rid)["status"] == "expired"
    assert outline_title(library, b.id) == "Doors"
    rid = tools.call("change_outline", {"chapter": 1, "title": "Keys"})["request_id"]
    assert gate.answer(True, rid)["status"] == "approved"
    assert gate.answer(True, rid) is None  # can't be replayed
    stale = tools.call("change_outline", {"chapter": 1, "title": "Gates"})["request_id"]
    clock.t += 121
    gate.expire_stale()
    assert gate.pending is None and library.list_permissions()[0]["id"] == stale
    assert library.list_permissions()[0]["status"] == "expired"


def test_the_exact_asked_action_runs_even_if_the_open_book_changes(box, library):
    tools, gate, _, b = box
    other = library.create_book("Other Book", "mystery", "x")
    library.set_outline(other.id, [{"number": 1, "title": "Elsewhere", "outline": "x"}])
    tools.call("change_outline", {"chapter": 1, "title": "Keys"})  # about the open book
    tools.current_book_id = other.id
    gate.answer(True)
    assert outline_title(library, b.id) == "Keys" and outline_title(library, other.id) == "Elsewhere"


def test_a_new_question_replaces_the_old_one(box, library):
    tools, gate, _, b = box
    first = tools.call("change_outline", {"chapter": 1, "title": "Keys"})["request_id"]
    tools.call("change_outline", {"chapter": 1, "title": "Gates"})
    assert gate.answer(True, first) is None
    gate.answer(True)
    assert outline_title(library, b.id) == "Gates"
    assert [r["status"] for r in library.list_permissions()[:2]] == ["approved", "replaced"]


def test_allow_for_session_is_only_for_change_level(box, library):
    tools, gate, _, b = box
    tools.call("change_outline", {"chapter": 1, "title": "Keys"})
    gate.answer(True, allow_session=True)
    done = tools.call("change_outline", {"chapter": 1, "title": "Gates"})
    assert "needs_approval" not in done and outline_title(library, b.id) == "Gates"
    assert library.list_permissions()[0]["status"] == "auto"
    gate.forget_allowances()
    assert tools.call("change_outline", {"chapter": 1, "title": "Locks"})["needs_approval"]
    gate.answer(False)
    tools.call("publish_audiobook", {"privacy": "private"})
    assert gate.pending.level == OUTSIDE
    gate.answer(False, allow_session=True)
    tools.call("publish_audiobook", {"privacy": "private"})
    gate.answer(True, allow_session=True)  # publisher missing -> error result, but never pre-approved
    assert "publish_audiobook" not in gate.allowed
    assert tools.call("publish_audiobook", {"privacy": "private"})["needs_approval"]


def test_spoken_yes_only_counts_after_jarvis_has_asked(box, library):
    tools, gate, _, b = box
    tools.call("change_outline", {"chapter": 1, "title": "Keys"}, source="voice")
    assert gate.hear(" yes do it") is None  # still the same breath as the request
    gate.arm()  # Jarvis finished asking
    assert gate.hear(" I") is None and gate.hear(" think") is None
    out = gate.hear(" yeah")
    assert out["approved"] and outline_title(library, b.id) == "Keys"
    assert library.list_permissions()[0]["via"] == "voice"


def test_typed_no_settles_and_the_user_direct_run_needs_no_question(box, library):
    tools, gate, _, b = box
    tools.call("change_outline", {"chapter": 1, "title": "Keys"}, source="chat")
    assert gate.hear("no thanks")["status"] == "denied"
    assert tools.run("change_outline", {"chapter": 1, "title": "Keys"})["chapter"] == 1
    assert outline_title(library, b.id) == "Keys" and gate.pending is None


class FakeEngine:
    """Gemini stand-in: asks to change the outline, then reacts to the app's note."""

    def __init__(self):
        self.seen = []

    def get_runtime_configuration(self):
        return type("C", (), {"history_turns": 4})()

    def generate(self, req):
        from jarvis_books.llm.models import LLMResponse
        last = req.history[-1]["parts"][0]
        self.seen.append(last)
        if "text" in last and "rename" in last["text"]:
            return LLMResponse(text="", model="fake", function_calls=[{"name": "change_outline",
                                                          "args": {"chapter": 1, "title": "Keys"}}])
        if "functionResponse" in last:
            return LLMResponse(text="Rename chapter 1 to Keys - yes or no?", model="fake")
        return LLMResponse(text="Done." if "said YES" in last["text"] else "Okay, left it.", model="fake")


def test_chat_round_trip_asks_then_does_it_on_the_authors_typed_yes(box, library):
    tools, gate, _, b = box
    engine = FakeEngine()
    chat = Assistant(engine, library, tools)
    assert "yes or no" in chat.send("rename chapter one to Keys")
    assert outline_title(library, b.id) == "Doors" and gate.pending is not None
    assert chat.send("yes") == "Done."
    assert outline_title(library, b.id) == "Keys"
    assert "said YES" in engine.seen[-1]["text"]


def test_needs_yes_levels():
    assert set(NEEDS_YES) == {CHANGE, OUTSIDE, "system"}
