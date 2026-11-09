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


class FakeSettings:
    DEFAULTS = {"voice": "Charon", "edge_voice": "en-US-AndrewNeural", "jarvis_voice": "Charon", "author": "",
                "wake_word": "1", "narration_speed": "1.0", "auto_mute": "1"}

    def __init__(self):
        self.values = dict(self.DEFAULTS)

    def settings(self):
        return dict(self.values)

    def set_setting(self, key, value):
        self.values[key] = value


def test_setting_keys_match_the_studio():
    from jarvis_books.assistant.tools import SETTING_KEYS
    from jarvis_books.studio import Studio
    assert set(SETTING_KEYS) == set(Studio.DEFAULTS)


def test_app_control_levels():
    for tool, level in {"app_status": "read", "get_settings": "read", "open_tab": "safe", "app_action": "safe",
                        "change_setting": "change", "undo_last_change": "change"}.items():
        assert level_of(tool) == level


def test_change_setting_waits_for_yes_and_validates_before_asking(box):
    tools, gate, clock, b = box
    tools.settings_store = store = FakeSettings()
    assert "must be" in tools.call("change_setting", {"key": "narration_speed", "value": "9"})["error"]
    assert "must be" in tools.call("change_setting", {"key": "jarvis_voice", "value": "Bob"})["error"]
    assert gate.pending is None
    ask = tools.call("change_setting", {"key": "jarvis_voice", "value": "puck"})
    assert ask["needs_approval"] and "'Charon' to 'Puck'" in ask["asking"]
    assert store.values["jarvis_voice"] == "Charon"
    assert gate.answer(True)["result"]["value"] == "Puck" and store.values["jarvis_voice"] == "Puck"
    assert tools.call("get_settings")["settings"]["jarvis_voice"] == "Puck"
    tools.run("change_setting", {"key": "auto_mute", "value": "off"})
    assert store.values["auto_mute"] == "0"


def test_undo_last_change_puts_things_back_after_a_yes(box, library):
    tools, gate, clock, b = box
    tools.settings_store = store = FakeSettings()
    assert "nothing to undo" in tools.call("undo_last_change")["error"] and gate.pending is None
    tools.run("change_outline", {"chapter": 1, "title": "Gates"})
    tools.run("update_story_bible", {"kind": "character", "name": "Mara", "summary": "A thief"})
    tools.run("change_setting", {"key": "narration_speed", "value": "1.25x"})
    assert store.values["narration_speed"] == "1.25"

    ask = tools.call("undo_last_change")
    assert ask["needs_approval"] and "narration_speed" in ask["asking"]
    gate.answer(True)
    assert store.values["narration_speed"] == "1.0"
    tools.call("undo_last_change")
    assert gate.answer(True)["result"]["undone"].startswith("adding character 'Mara'")
    assert library.list_bible(b.id, "character") == []
    tools.call("undo_last_change")
    gate.answer(True)
    assert outline_title(library, b.id) == "Doors"


def test_a_yes_to_undo_never_undoes_a_newer_change(box, library):
    tools, gate, clock, b = box
    tools.run("change_outline", {"chapter": 1, "title": "Gates"})
    ask = tools.call("undo_last_change")
    tools.run("change_outline", {"chapter": 1, "title": "Keys"})  # something else changed before the yes
    out = gate.answer(True, ask["request_id"])["result"]
    assert "ask again" in out["error"] and outline_title(library, b.id) == "Keys"


def test_undo_restores_the_story_bible_entry_and_an_added_chapter(box, library):
    tools, gate, clock, b = box
    library.upsert_bible(b.id, "character", "Mara", "A thief")
    tools.run("update_story_bible", {"kind": "Character", "name": "mara", "summary": "A queen"})
    tools.run("change_outline", {"chapter": 0, "title": "Epilogue"})
    assert len(library.list_chapters(b.id)) == 2
    tools.run("undo_last_change")
    assert len(library.list_chapters(b.id)) == 1
    tools.run("undo_last_change")
    assert library.list_bible(b.id, "character")[0].summary == "A thief"


def test_app_controls_need_the_window(box):
    tools, gate, clock, b = box
    assert "window" in tools.call("open_tab", {"tab": "listen"})["error"]
    seen = []
    tools.app_handler = lambda action, args: seen.append((action, args)) or {"ok": True}
    assert tools.call("open_tab", {"tab": "listen", "book": "Midnight"}) == {"ok": True}
    assert seen == [("tab", {"tab": "listen", "book_id": b.id})] and gate.pending is None
    assert "invalid" in tools.call("open_tab", {"tab": "kitchen"})["error"]
    assert tools.call("app_action", {"action": "mute_mic"}) == {"ok": True}
    status = tools.call("app_status")
    assert status["books"] == 1 and status["open_book"] == "Midnight Rings" and status["ok"]


def test_reading_speed_changes_straight_away_and_can_be_undone(box):
    from jarvis_books.assistant.tools import step_speed
    tools, gate, clock, b = box
    tools.settings_store = store = FakeSettings()
    assert level_of("reading_speed") == "safe"
    assert tools.call("reading_speed", {"change": "faster"})["speed"] == "1.1x" and gate.pending is None
    assert store.values["narration_speed"] == "1.1"
    tools.call("reading_speed", {"change": "slower"})
    tools.call("reading_speed", {"change": "slower"})
    assert store.values["narration_speed"] == "0.9"
    tools.call("reading_speed", {"change": "2.5x"})
    assert store.values["narration_speed"] == "2.5"
    tools.call("reading_speed", {"change": "normal"})
    assert store.values["narration_speed"] == "1.0"
    assert "must be" in tools.call("reading_speed", {"change": "9"})["error"]
    store.values["narration_speed"] = "3.0"
    assert "fastest" in tools.call("reading_speed", {"change": "faster"})["note"]
    tools.run("undo_last_change")
    gate.answer(True)
    assert store.values["narration_speed"] == "2.5"  # undo puts back the speed before 'normal'
    assert step_speed(1.3, 1) == 1.5 and step_speed(1.3, -1) == 1.25 and step_speed(0.5, -1) == 0.5
    assert "reading_speed" in [d["name"] for d in tools.select("read a bit faster please")]
