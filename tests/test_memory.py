"""Jarvis's long-term memory: storage, learning from conversations, recall, tools and the Settings panel."""
import json
import time

import pytest

from fakes import FakeGemini, body
from jarvis_books.errors import NotFound
from jarvis_books.llm.models import LLMResponse
from jarvis_books.memory.longterm import BATCH, JarvisMemory
from jarvis_books.storage.db import SCHEMA_VERSION
from jarvis_books.studio import Studio


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


class LearningEngine:
    """Gemini stand-in for the memory pass: returns whatever the test queued."""

    def __init__(self):
        self.answers, self.asked = [], []

    def generate(self, req):
        self.asked.append(req)
        return LLMResponse(text="", model="fake", data=self.answers.pop(0))

    def embed(self, texts, task_type=""):
        raise RuntimeError("no embeddings in this test")


@pytest.fixture
def mem(library):
    clock = Clock()
    settings = {"memory": "1"}
    engine = LearningEngine()
    m = JarvisMemory(library, engine, lambda: settings, clock=clock, background=False)
    m.test = type("T", (), {"clock": clock, "settings": settings, "engine": engine})
    return m


def chat(library, n, book_id=None, channel="chat"):
    for i in range(n):
        library.add_message(channel, "user" if i % 2 == 0 else "assistant", f"line {i}", book_id)


def test_schema_has_memory_tables(library):
    assert library.db.schema_version == SCHEMA_VERSION >= 7
    assert library.db.query_one("SELECT COUNT(*) FROM memories")[0] == 0


def test_memories_store_scope_and_go_with_their_book(library):
    book = library.create_book("The Rings")
    everywhere = library.add_memory("Justin hates prologues", kind="preference", source="you", importance=3)
    rings = library.add_memory("Lily is 8", book_id=book.id)
    assert [m["id"] for m in library.list_memories(book.id)] == [rings["id"], everywhere["id"]]
    assert [m["id"] for m in library.list_memories(None, only_scope=True)] == [everywhere["id"]]
    library.update_memory(rings["id"], pinned=True)
    assert library.list_memories()[0]["pinned"]
    library.delete_book(book.id)
    assert [m["id"] for m in library.list_memories()] == [everywhere["id"]]
    with pytest.raises(NotFound):
        library.get_memory(rings["id"])


def test_remember_dedupes_and_edit_pin_forget_restore(mem, library):
    a = mem.remember("I hate prologues.")
    b = mem.remember("i hate prologues")
    assert a["new"] and not b["new"] and a["id"] == b["id"] and len(library.list_memories()) == 1
    assert mem.edit(a["id"], "I love short prologues")["text"] == "I love short prologues"
    assert mem.toggle_pin(a["id"])["pinned"]
    row = mem.forget(a["id"])
    assert not library.list_memories()
    back = mem.restore(row)
    assert back["text"] == "I love short prologues" and back["pinned"]


def test_learning_waits_for_a_finished_conversation(mem, library):
    chat(library, 3)
    assert not mem.due("chat") and mem.consolidate("chat") is None and not mem.test.engine.asked
    mem.test.clock.now += 10 ** 9  # the conversation went quiet
    assert mem.due("chat")
    library.add_message("voice", "user", "x")
    chat(library, BATCH, channel="warroom", book_id=library.create_book("B").id)
    assert mem.due("warroom", library.list_books()[0].id)


def test_a_learning_pass_adds_updates_and_respects_pins(mem, library):
    book = library.create_book("The Rings")
    lily = library.add_memory("Lily is 8 years old", book_id=book.id)
    pinned = library.add_memory("Justin writes at night", importance=3, pinned=True)
    chat(library, BATCH, book_id=book.id)
    mem.test.engine.answers.append({"summary": "We made Lily older and planned the captain's death.", "memories": [
        {"replaces": lily["id"], "text": "Lily is 9 years old", "kind": "fact", "scope": "this book", "importance": 2},
        {"replaces": pinned["id"], "text": "Justin writes in the morning", "kind": "about_you", "scope": "all books",
         "importance": 3},
        {"replaces": 0, "text": "The captain dies in chapter 12", "kind": "decision", "scope": "this book",
         "importance": 3},
        {"replaces": 0, "text": "Justin hates prologues", "kind": "preference", "scope": "all books", "importance": 9},
        {"replaces": 0, "text": "Justin hates prologues!", "kind": "preference", "scope": "all books", "importance": 2},
        {"replaces": 999, "text": "ok", "kind": "fact", "scope": "all books", "importance": 1},
    ]})
    out = mem.consolidate("chat")
    assert out["updated"] == ["Lily is 9 years old"]
    assert out["added"] == ["The captain dies in chapter 12", "Justin hates prologues"]
    texts = {m["text"]: m for m in library.list_memories()}
    assert "Lily is 8 years old" not in texts and texts["Lily is 9 years old"]["id"] == lily["id"]
    assert texts["Justin writes at night"]["pinned"] and "Justin writes in the morning" not in texts
    assert texts["The captain dies in chapter 12"]["book_id"] == book.id
    assert texts["Justin hates prologues"]["book_id"] is None and texts["Justin hates prologues"]["importance"] == 3
    ask = mem.test.engine.asked[0]
    assert f"#{lily['id']}" in ask.instruction and "Author: line 0" in ask.instruction and ask.json_schema
    # Those messages are learned from: nothing is due until the author talks again.
    assert not mem.due("chat") and mem.consolidate("chat", force=True) is None
    assert "the captain's death" in mem.prompt("captain", book.id)


def test_learning_switched_off_keeps_nothing(mem, library):
    chat(library, BATCH)
    mem.test.settings["memory"] = "0"
    assert mem.consolidate("chat") == {"summary": "", "added": [], "updated": []}
    assert not mem.test.engine.asked and not library.list_memories() and not mem.due("chat")
    assert mem.remember("Explicit memories still save")["new"]


def test_recall_ranks_by_relevance_and_always_keeps_pins(mem, library):
    book, other = library.create_book("The Rings"), library.create_book("Other")
    for i in range(20):
        library.add_memory(f"filler memory number {i}")
    library.add_memory("The dragon is afraid of rain", book_id=book.id)
    library.add_memory("Secret of the other book", book_id=other.id)
    library.add_memory("Always end chapters on a hook", pinned=True, importance=1)
    found = mem.recall("why is the dragon scared?", book.id, k=3)
    assert found[0]["text"] == "Always end chapters on a hook"
    assert found[1]["text"] == "The dragon is afraid of rain" and found[1]["scope"] == "The Rings"
    assert all(m["text"] != "Secret of the other book" for m in mem.recall("secret other book", book.id, k=30))
    prompt = mem.prompt("dragon", book.id)
    assert prompt.startswith("LONG-TERM MEMORY") and "not story canon" in prompt and "[The Rings]" in prompt


def test_background_learning_runs_off_the_ui_thread_and_shuts_down(library):
    engine = LearningEngine()
    engine.answers.append({"summary": "Talked about pacing.", "memories": []})
    mem = JarvisMemory(library, engine, clock=Clock())
    chat(library, 2, channel="voice")
    mem.schedule("voice", force=True)
    mem.shutdown()
    assert library.conversation_summaries()[0]["summary"] == "Talked about pacing."
    mem.schedule("voice", force=True)  # closed: ignored


def responder(memories):
    def answer(model_id, payload):
        raw = json.dumps(payload)
        if "maintain the long-term memory" in raw:
            return body(json.dumps({"summary": "We talked about Mara.", "memories": memories}))
        return FakeGemini.default(model_id, payload)
    return answer


def make_studio(tmp_path, memories=()):
    from conftest import hardware
    s = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(responder(list(memories))),
               engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None})
    s.start(probe=False)
    return s


def test_memory_tools_remember_recall_and_forget_with_a_yes(tmp_path):
    studio = make_studio(tmp_path)
    try:
        tools, gate = studio.tools, studio.gate
        book = studio.library.create_book("The Rings")
        tools.current_book_id = book.id
        assert "remember" in [d["name"] for d in tools.select("remember that I hate prologues")]
        got = tools.call("remember", {"text": "Justin hates prologues"})
        assert got["remembered"] == "Justin hates prologues" and got["scope"] == "all books" and gate.pending is None
        here = tools.call("remember", {"text": "Mara is left-handed", "scope": "this book"})
        assert here["scope"] == "The Rings"
        listed = tools.call("recall_memories")
        assert {m["text"] for m in listed["memories"]} == {"Justin hates prologues", "Mara is left-handed"}
        assert "error" in tools.call("forget_memory", {"memory_id": 999})
        ask = tools.call("forget_memory", {"memory_id": got["memory_id"]})
        assert ask["needs_approval"] and "Justin hates prologues" in ask["asking"]
        gate.answer(False)
        assert len(studio.library.list_memories()) == 2
        tools.call("forget_memory", {"memory_id": got["memory_id"]})
        gate.answer(True)
        assert [m["text"] for m in studio.library.list_memories()] == ["Mara is left-handed"]
        tools.run("undo_last_change")  # undo is gated too when Jarvis asks; the author's button runs it
        assert len(studio.library.list_memories()) == 2
    finally:
        studio.close()


def test_chat_sees_memories_and_voice_turns_and_memory_survives_a_restart(tmp_path):
    studio = make_studio(tmp_path, [{"replaces": 0, "text": "Mara is the hero of The Rings", "kind": "fact",
                                     "scope": "all books", "importance": 3}])
    studio.memory.background = False
    studio.library.add_message("voice", "user", "Let's make Mara braver.")
    studio.library.add_message("voice", "assistant", "Braver Mara it is.")
    studio.memory.consolidate("voice", force=True)
    studio.assistant.send("who is the hero?")
    sent = studio.client.calls[-1][1]
    raw = json.dumps(sent)
    assert "Mara is the hero of The Rings" in raw and "We talked about Mara." in raw
    assert "Let's make Mara braver." in raw  # the voice chat is part of the Chat conversation
    studio.close()

    again = make_studio(tmp_path)
    try:
        assert [m["text"] for m in again.library.list_memories()] == ["Mara is the hero of The Rings"]
        assert "Mara is the hero" in again.memory.prompt("hero")
    finally:
        again.close()


def test_settings_panel_shows_and_edits_memories(qapp_mem):
    win, studio = qapp_mem
    win._memory_add("Justin hates prologues")
    assert win.memory_list.count() == 1 and "[all books] Justin hates prologues" in win.memory_list.item(0).text()
    win.memory_list.setCurrentRow(0)
    win._memory_edit("Justin loves short prologues")
    win.memory_buttons["Pin / unpin"].click()
    assert win.memory_list.item(0).text() == "[pinned] [all books] Justin loves short prologues"
    win.memory_box.setChecked(False)
    assert studio.settings()["memory"] == "0"
    win._show_setting("memory", "1")
    assert win.memory_box.isChecked()
    win.memory_list.setCurrentRow(0)
    win.memory_buttons["Forget"].click()
    assert win.memory_list.count() == 0 and not studio.library.list_memories()


def test_voice_turns_are_saved_for_the_next_conversation(qapp_mem):
    win, studio = qapp_mem
    win._on_voice_transcript("user", "Remember the cat ")
    win._on_voice_transcript("user", "is called Pip.")
    win._on_voice_transcript("jarvis", "Got it.")
    win._on_voice_transcript("turn", "")
    msgs = studio.library.recent_messages("voice", 5)
    assert [(m.role, m.text) for m in msgs] == [("user", "Remember the cat is called Pip."), ("assistant", "Got it.")]
    assert "Remember the cat is called Pip." in win._voice_memory(("chat", "voice"), None)


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def qapp_mem(qapp, tmp_path):
    from jarvis_books.ui.main_window import MainWindow
    studio = make_studio(tmp_path)
    win = MainWindow(studio)
    yield win, studio
    win.close()
    studio.close()
