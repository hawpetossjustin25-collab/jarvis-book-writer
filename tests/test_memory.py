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
    assert mem.consolidate("chat") == {"summary": "", "added": [], "updated": [], "book_id": None}
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
        if "reflecting on the long-term memory" in raw:
            return body(json.dumps({"merges": [], "retire": [], "lessons": []}))
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


# ---- recursive learning ----------------------------------------------------------------------
class AutoEngine(LearningEngine):
    """Answers roll-ups by itself; reflection answers come from the queue."""

    def __init__(self):
        super().__init__()
        self.folds = 0

    def generate(self, req):
        if req.purpose == "jarvis memory rollup":
            self.asked.append(req)
            self.folds += 1
            return LLMResponse(text="", model="fake", data={"summary": f"fold {self.folds}"})
        return super().generate(req)


@pytest.fixture
def rmem(library):
    clock = Clock()
    engine = AutoEngine()
    m = JarvisMemory(library, engine, lambda: {"memory": "1"}, clock=clock, background=False)
    m.test = type("T", (), {"clock": clock, "engine": engine})
    return m


def summaries(library, n, book_id=None):
    for i in range(n):
        library.add_conversation_summary("chat", book_id, i * 2 + 1, i * 2 + 2, f"talk {i}")


def test_archive_keeps_retired_memories_out_of_recall_but_restorable(library, rmem):
    a = library.add_memory("Old plan: Lily dies")
    library.archive_memory(a["id"], "out of date")
    assert not library.list_memories() and library.archived_memories()[0]["archive_reason"] == "out of date"
    assert rmem.recall("Lily") == [] and "Lily dies" not in rmem.prompt("Lily")
    assert rmem.bring_back(a["id"])["text"] == "Old plan: Lily dies" and len(library.list_memories()) == 1
    assert library.learning_log()[0]["action"] == "brought back"
    for i in range(5):
        library.add_memory(f"memory {i}", importance=1)
    library.prune_memories(3)
    assert len(library.list_memories()) == 3 and len(library.archived_memories()) == 3


def test_summaries_fold_recursively_into_one_long_term_picture(library, rmem, monkeypatch):
    import jarvis_books.memory.longterm as lt
    monkeypatch.setattr(lt, "ROLLUP", 2)
    summaries(library, 8)
    assert rmem.rollup() == 7  # 8 -> 4 level-1 -> 2 level-2 -> 1 level-3
    top = library.big_picture_summaries()
    assert [s["level"] for s in top] == [3, 2] or [s["level"] for s in top][0] == 3
    assert library.unrolled_summaries(None, 0) == [] and len(library.unrolled_summaries(None, 3)) == 1
    first_top = library.unrolled_summaries(None, 3)[0]["id"]
    summaries(library, 8)
    assert rmem.rollup() == 8  # a second level-3 picture is merged with the first one
    tops = library.unrolled_summaries(None, 3)
    assert len(tops) == 1 and tops[0]["id"] != first_top
    assert "fold 15" in rmem.prompt() and "The long view" in rmem.prompt()
    # the merge read the old picture and the new one together
    assert "fold 7" in rmem.test.engine.asked[-1].instruction
    assert library.conversation_summaries(limit=1)[0]["level"] == 0  # "Earlier conversations" stay concrete


def test_rollups_stay_within_their_book(library, rmem, monkeypatch):
    import jarvis_books.memory.longterm as lt
    monkeypatch.setattr(lt, "ROLLUP", 2)
    book = library.create_book("The Rings")
    summaries(library, 2, book.id)
    summaries(library, 1)
    assert rmem.rollup(book.id) == 1 and rmem.rollup(None) == 0
    assert library.big_picture_summaries(book.id)[0]["book_id"] == book.id
    assert "The Rings" in rmem.test.engine.asked[-1].instruction


def reflect_scenario(library, rmem) -> dict:
    book = library.create_book("The Rings")
    a = library.add_memory("Justin likes chapters to end on a hook", kind="preference")
    b = library.add_memory("Justin wants every chapter to finish with a cliffhanger", kind="preference")
    old = library.add_memory("Justin plans to write in the mornings", kind="plan")
    pin = library.add_memory("Justin hates prologues", kind="preference", pinned=True)
    e1 = rmem.note("idea_rejected", "The author rejected Jarvis's ending idea 'Mara dies alone'")
    e2 = rmem.note("idea_rejected", "The author rejected Jarvis's ending idea 'The town burns'")
    rmem.note("idea_rejected", "A reaction about one book only is not shown in the all-books pass", book.id)
    rmem.test.engine.answers.append({
        "merges": [{"ids": [a["id"], b["id"]], "text": "Justin wants chapters to end on a cliffhanger",
                    "kind": "preference", "importance": 3},
                   {"ids": [pin["id"], old["id"]], "text": "pinned ones never merge", "kind": "fact",
                    "importance": 1}],
        "retire": [{"id": old["id"], "why": "he now writes at night"}, {"id": pin["id"], "why": "no"}],
        "lessons": [{"replaces": 0, "text": "Justin prefers hopeful endings over gloomy ones", "scope": "all books",
                     "confidence": 2, "evidence": [a["id"]], "events": [e1, e2]},
                    {"replaces": 0, "text": "Based on one thing only", "scope": "all books", "confidence": 3,
                     "evidence": [], "events": [e1]}]})
    out = rmem.reflect(None, force=True)
    assert out["merged"] == 1 and out["retired"] == 1
    assert out["lessons_added"] == ["Justin prefers hopeful endings over gloomy ones"]
    active = {m["text"]: m for m in library.list_memories()}
    assert set(active) == {"Justin wants chapters to end on a cliffhanger", "Justin hates prologues",
                           "Justin prefers hopeful endings over gloomy ones"}
    assert active["Justin wants chapters to end on a cliffhanger"]["strength"] == 2.0
    reasons = {m["text"]: m["archive_reason"] for m in library.archived_memories()}
    assert reasons["Justin wants every chapter to finish with a cliffhanger"] == f"merged into #{a['id']}"
    assert reasons["Justin plans to write in the mornings"] == "he now writes at night"
    lesson = active["Justin prefers hopeful endings over gloomy ones"]
    assert lesson["kind"] == "lesson" and lesson["source"] == "reflection" and lesson["book_id"] is None
    why = [e["text"] for e in rmem.explain(lesson["id"])["evidence"]]
    assert why == ["Justin wants chapters to end on a cliffhanger", "The author rejected Jarvis's ending idea "
                   "'Mara dies alone'", "The author rejected Jarvis's ending idea 'The town burns'"]
    ask = rmem.test.engine.asked[-1].instruction
    assert f"E{e1}" in ask and "pinned" in ask and "one book only" not in ask and ask.count("#") >= 4
    actions = [e["action"] for e in library.learning_log()]
    assert {"merged", "retired", "new lesson", "reflected"} <= set(actions)
    assert not rmem.reflection_due(None)
    return lesson


def test_reflection_merges_retires_and_draws_lessons_from_patterns(library, rmem):
    reflect_scenario(library, rmem)


def test_later_reflection_builds_on_earlier_lessons(library, rmem):
    lesson = reflect_scenario(library, rmem)
    e3 = rmem.note("idea_edited", "The author turned Jarvis's tragic ending into a hopeful one")
    rmem.test.engine.answers.append({"merges": [], "retire": [], "lessons": [
        {"replaces": lesson["id"], "text": "Justin strongly prefers hopeful endings", "scope": "all books",
         "confidence": 3, "evidence": [lesson["id"]], "events": [e3]}]})
    out = rmem.reflect(None, force=True)
    assert out["lessons_updated"] == ["Justin strongly prefers hopeful endings"]
    m = library.get_memory(lesson["id"])
    assert m["importance"] == 3 and -e3 in m["evidence"] and lesson["id"] in m["evidence"]
    assert len(m["evidence"]) == 5 and m["strength"] == pytest.approx(1.75)
    assert "(evidence: #" in rmem.test.engine.asked[-1].instruction  # the earlier lesson was reread with its why


def test_reflection_runs_by_itself_after_enough_reactions(library, rmem):
    rmem.test.engine.answers.append({"merges": [], "retire": [], "lessons": []})
    for i in range(4):
        rmem.note("said_no", f"no {i}")
    assert not rmem.test.engine.asked
    rmem.note("said_no", "no 4")
    assert len(rmem.test.engine.asked) == 1 and rmem.test.engine.asked[0].purpose == "jarvis memory reflection"
    assert not rmem.reflection_due(None)


def test_author_rewrites_of_jarvis_text_become_reactions(library, rmem):
    book = library.create_book("The Rings")
    ch = library.add_chapter(book.id, title="One")
    library.save_chapter_text(ch.id, "The sky was very dark. Mara was very scared.", source="ai-draft")
    library.save_chapter_text(ch.id, "The sky was very dark. Mara gripped the key.", source="user")
    library.add_memory("Mara is the hero", book_id=book.id)
    rmem.test.engine.answers.append({"merges": [], "retire": [], "lessons": []})
    rmem.reflect(book.id, force=True)
    ev = library.learning_events(book.id)
    assert ev[0]["kind"] == "rewrite" and "Mara was very scared." in ev[0]["detail"]
    assert "Mara gripped the key." in ev[0]["detail"] and "The sky was very dark" not in ev[0]["detail"]
    assert "rewrite) In chapter 1" in rmem.test.engine.asked[-1].instruction
    rmem.test.engine.answers.append({"merges": [], "retire": [], "lessons": []})
    rmem.reflect(book.id, force=True)
    assert len(library.learning_events(book.id)) == 1  # the same rewrite is not counted twice


def test_memories_that_help_get_stronger_and_corrected_ones_start_over(library, mem):
    hero = library.add_memory("Mara is the hero")
    lily = library.add_memory("Lily is 8 years old")
    chat(library, 2)
    mem.prompt("Mara hero Lily")  # Jarvis used both during the conversation
    mem.test.engine.answers.append({"summary": "s", "memories": [
        {"replaces": lily["id"], "text": "Lily is 9 years old", "kind": "fact", "scope": "all books",
         "importance": 2}]})
    mem.consolidate("chat", force=True)
    assert library.get_memory(hero["id"])["strength"] == pytest.approx(1.25)
    assert library.get_memory(lily["id"])["strength"] == 1.0 and library.get_memory(lily["id"])["uses"] == 1


def test_unused_memories_fade_into_the_archive_but_pins_never_do(library, rmem):
    minor = library.add_memory("A minor detail", importance=1)
    big = library.add_memory("A big lasting preference", importance=3)
    pinned = library.add_memory("Pinned forever", importance=1, pinned=True)
    rmem.test.clock.now += 300 * 86400
    assert rmem.fade() == ["A minor detail"]
    rmem.test.clock.now += 600 * 86400
    assert rmem.fade() == ["A big lasting preference"]
    assert [m["id"] for m in library.list_memories()] == [pinned["id"]]
    assert {m["id"] for m in library.archived_memories()} == {minor["id"], big["id"]}
    assert library.learning_log()[0]["action"] == "faded"


def test_writing_uses_what_jarvis_learned(tmp_path):
    studio = make_studio(tmp_path)
    try:
        book = studio.library.create_book("The Rings", premise="A ring")
        studio.library.add_memory("Justin prefers hopeful endings", kind="lesson", evidence=[1, 2])
        studio.library.add_memory("Mara is left-handed", kind="fact", book_id=book.id)
        assert "Justin prefers hopeful endings" in studio.writer._system("BASE", book.id)
        assert "left-handed" not in studio.writer._system("BASE", book.id)
        studio.writer.outline_book(book.id, 2)
        system = json.dumps(studio.client.calls[-1][1].get("systemInstruction"))
        assert "Justin prefers hopeful endings" in system and "THIS AUTHOR'S TASTE" in system
    finally:
        studio.close()


def test_war_room_choices_and_yes_no_answers_are_learned_from(tmp_path):
    studio = make_studio(tmp_path)
    try:
        studio.memory.background = False
        book = studio.library.create_book("The Rings")
        wr = studio.warroom
        p1 = wr.propose(book.id, "note", "Gloomy ending", "Everyone dies")
        p2 = wr.propose(book.id, "note", "Twist", "The ring is fake")
        p3 = wr.propose(book.id, "note", "Pet", "Mara has a cat")
        wr.reject(p1.id)
        wr.accept(p2.id, content="The ring is a copy")
        wr.accept(p3.id)
        kinds = [e["kind"] for e in studio.library.learning_events(book.id)]
        assert kinds == ["idea_rejected", "idea_edited", "idea_accepted"]
        assert "Everyone dies" in studio.library.learning_events(book.id)[0]["detail"]
        studio.tools.current_book_id = book.id
        assert studio.tools.call("change_setting", {"key": "narration_speed", "value": "1.25"})["needs_approval"]
        studio.gate.answer(False)
        said = studio.library.learning_events(None)
        assert said[-1]["kind"] == "said_no" and "The author said no." in said[-1]["detail"]
        studio.tools.call("forget_memory", {"memory_id": studio.memory.remember("x")["id"]})
        studio.gate.answer(False)
        assert studio.library.learning_events(None)[-1]["id"] == said[-1]["id"]  # memory tools are not lessons
        studio.set_setting("memory", "0")
        assert studio.memory.note("said_no", "ignored") is None
    finally:
        studio.close()


def test_recall_tool_explains_lessons(tmp_path):
    studio = make_studio(tmp_path)
    try:
        why = studio.library.add_memory("Justin rejected four gloomy endings")
        studio.library.add_memory("Justin prefers hopeful endings", kind="lesson", evidence=[why["id"]])
        got = studio.tools.call("recall_memories")
        lesson = next(m for m in got["memories"] if m["kind"] == "lesson")
        assert lesson["because"] == ["Justin rejected four gloomy endings"]
    finally:
        studio.close()


def test_settings_shows_lessons_archive_and_learning_log(qapp_mem):
    win, studio = qapp_mem
    why = studio.library.add_memory("Justin rejected four gloomy endings")
    studio.library.add_memory("Justin prefers hopeful endings", kind="lesson", evidence=[why["id"]])
    old = studio.library.add_memory("Old plan")
    studio.library.archive_memory(old["id"], "out of date")
    studio.library.log_learning("new lesson", "Justin prefers hopeful endings")
    win._refresh_memories()
    texts = [win.memory_list.item(i).text() for i in range(win.memory_list.count())]
    assert "[lesson] [all books] Justin prefers hopeful endings" in texts
    win.memory_list.setCurrentRow(texts.index("[lesson] [all books] Justin prefers hopeful endings"))
    assert "Because: Justin rejected four gloomy endings" in win.memory_detail.text()
    assert win.memory_archive.item(0).text() == "[all books] Old plan  (out of date)"
    assert "new lesson: Justin prefers hopeful endings" in win.learning_log_list.item(0).text()
    win.memory_archive.setCurrentRow(0)
    win.memory_buttons["Bring back"].click()
    assert win.memory_archive.count() == 0 and "[all books] Old plan" in \
        [win.memory_list.item(i).text() for i in range(win.memory_list.count())]
    studio.memory.background = False
    win.memory_buttons["Reflect now"].click()  # runs a reflection pass on Gemini (fake here) without errors
    assert any(e["action"] == "reflected" for e in studio.library.learning_log())
