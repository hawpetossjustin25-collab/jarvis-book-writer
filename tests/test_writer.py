import json
import threading

import pytest

from fakes import body
from jarvis_books.assistant import Assistant, ToolBox
from jarvis_books.jobs import JobManager
from jarvis_books.memory import ContextBuilder, StoryIndex
from jarvis_books.writer import BookWriter, Cancelled
from jarvis_books.writer.continuity import check_chapter, unknown_names

PROSE = ("Rain hammered the vault doors while Mara counted the guards. " * 30 + "\n\n") * 4


def story_responder(model_id, payload):
    schema = (payload.get("generationConfig") or {}).get("responseSchema")
    props = (schema or {}).get("properties", {})
    if "logline" in props:
        return body(json.dumps({
            "title": "The Midnight Vault", "genre": "thriller", "logline": "A thief robs the bank that ruined her.",
            "premise": "heist", "audience": "adult", "pov": "third limited", "tense": "past", "structure": "three_act",
            "themes": ["justice"], "synopsis": "Mara plans one last job.", "style_notes": "lean",
            "characters": [{"name": "Mara Voss", "role": "protagonist", "summary": "a safecracker", "want": "revenge",
                            "need": "trust", "flaw": "pride", "arc": "learns to trust", "voice": "dry"}],
            "locations": [{"name": "Halvard Bank", "summary": "the target"}],
            "threads": [{"name": "The ledger", "summary": "proof of fraud"}]}))
    if "chapters" in props and props["chapters"].get("type") == "ARRAY":
        return body(json.dumps({"chapters": [
            {"number": n, "title": t, "outline": f"Mara {t.lower()}.", "beat": "setup", "target_words": 1500}
            for n, t in ((1, "Cases the bank"), (2, "Recruits a crew"), (3, "Breaks in"))]}))
    if "chapters" in props:
        return body(json.dumps({"title": "Probe", "chapters": 3}))
    if "scenes" in props:
        return body(json.dumps({"scenes": [
            {"pov": "Mara", "setting": "street", "goal": "case", "conflict": "guards", "outcome": "yes, but",
             "beats": "watch", "target_words": 800},
            {"pov": "Mara", "setting": "roof", "goal": "enter", "conflict": "alarm", "outcome": "no",
             "beats": "climb", "target_words": 700}], "hook": "The vault was already open."}))
    if "verdict" in props:
        return body(json.dumps({"verdict": "accept", "issues": []}))
    if "character_updates" in props:
        return body(json.dumps({"summary": "Mara cased the bank and found it already breached.",
                                "character_updates": [{"name": "Mara Voss", "summary": "shaken", "status": "alive"}],
                                "new_entities": [{"kind": "character", "name": "Inspector Hale", "summary": "a cop"}],
                                "open_threads": [{"name": "Who opened the vault", "summary": "mystery"}],
                                "resolved_threads": []}))
    if payload.get("tools"):
        return body(calls=[{"name": "lookup_character", "args": {"name": "Mara"}}])
    return body(PROSE, output_tokens=1200)


@pytest.fixture
def studio(library, make_engine, fake):
    fake.responder = story_responder
    engine = make_engine(fake)
    index = StoryIndex(library, engine)
    writer = BookWriter(library, engine, index)
    jobs = JobManager(library, workers=1)
    yield library, engine, index, writer, jobs, fake
    jobs.shutdown(wait=True)


def test_create_book_builds_bible_and_outline(studio):
    library, _, _, writer, _, _ = studio
    stages = []
    book = writer.create_book("a heist thriller about revenge", progress=lambda p, m: stages.append(p))
    assert book.title == "The Midnight Vault" and book.genre == "thriller"
    chapters = library.list_chapters(book.id)
    assert [c.number for c in chapters] == [1, 2, 3]
    assert all(c.target_words >= book.chapter_words_min for c in chapters)
    names = {e.name for e in library.list_bible(book.id)}
    assert {"Mara Voss", "Halvard Bank", "The ledger"} <= names
    assert stages[-1] == 1.0


def test_write_chapter_full_pipeline(studio):
    library, engine, index, writer, _, fake = studio
    book = writer.create_book("a heist thriller")
    ch = writer.write_chapter(book.id, 1)
    assert ch.status == "accepted" and ch.word_count > 1000
    assert ch.summary.startswith("Mara cased")
    assert library.revisions(ch.id)[0].source in ("ai-draft", "ai-revision")
    bible = {e.name: e for e in library.list_bible(book.id)}
    assert "Inspector Hale" in bible and bible["Mara Voss"].data["status"] == "alive"
    assert bible["Who opened the vault"].data["status"] == "open"
    assert index.search("vault guards", book.id)
    # the writer prompt carried the scene plan and the story bible
    draft_payloads = [p for _, p in fake.calls if "Scenes to write now" in json.dumps(p)]
    assert draft_payloads and "Mara Voss" in json.dumps(draft_payloads[0])
    assert writer.next_unwritten(book.id).number == 2


def test_scene_grouping_respects_words_per_call():
    scenes = [{"target_words": 900}, {"target_words": 900}, {"target_words": 900}]
    assert [len(g) for g in BookWriter.group_scenes(scenes, 2000)] == [2, 1]
    assert [len(g) for g in BookWriter.group_scenes(scenes, 600)] == [1, 1, 1]


def test_second_chapter_continues_from_previous_ending(studio):
    library, _, _, writer, _, fake = studio
    book = writer.create_book("heist")
    writer.write_chapter(book.id, 1)
    fake.calls.clear()
    writer.draft_chapter(book.id, 2)
    sent = json.dumps([p for _, p in fake.calls])
    assert "End of the previous chapter" in sent and "Story so far" in sent


def test_cancel_stops_between_steps(studio):
    _, _, _, writer, _, _ = studio
    book = writer.create_book("heist")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        writer.draft_chapter(book.id, 1, cancel=cancel)


def test_truncated_revision_never_replaces_draft(studio):
    library, _, _, writer, _, fake = studio
    book = writer.create_book("heist")
    writer.draft_chapter(book.id, 1)
    before = library.get_chapter(book.id, 1).text
    fake.responder = lambda m, p: body("Too short.")
    after = writer.revise_chapter(book.id, 1, "make it better")
    assert after.text == before


def test_continuity_checks(library):
    book = library.create_book("C", "mystery")
    dead = library.upsert_bible(book.id, "character", "Tom Reed", "victim", {"status": "dead"})
    text = "Later that night Tom said hello. " * 3 + "It was cold. She met Veronica and Jess. Later Veronica left with Jess."
    kinds = {f.kind for f in check_chapter(text, [dead], 3000, "past")}
    assert {"continuity", "length"} <= kinds
    names = unknown_names(text, [dead])
    assert "Veronica" in names and "Jess" in names and "Tom" not in names


def test_jobs_run_in_background_and_report(studio):
    library, _, _, writer, jobs, _ = studio
    book = writer.create_book("heist")
    seen = []
    jobs.subscribe(lambda j: seen.append(j.status))
    job = jobs.submit("write", "w", lambda p, c: [x.number for x in writer.write_next_chapters(book.id, 2, progress=p,
                                                                                            cancel=c)], book.id)
    done = jobs.wait(job.id, timeout=60)
    assert done.status == "done" and done.result == [1, 2]
    assert "running" in seen
    row = library.db.query_one("SELECT status FROM jobs WHERE id=?", (job.id,))
    assert row["status"] == "done"


def test_assistant_calls_tools_and_persists_chat(studio):
    library, engine, index, writer, jobs, fake = studio
    library.create_book("The Midnight Vault", "thriller", "heist")

    def chat(model_id, payload):
        last = payload["contents"][-1]["parts"]
        if any("functionResponse" in p for p in last):
            books = last[0]["functionResponse"]["response"]["books"]
            return body(f"You have {len(books)} book: {books[0]['title']}.")
        assert payload.get("tools")
        return body(calls=[{"name": "list_books", "args": {}}])
    fake.responder = chat
    tools_called = []
    assistant = Assistant(engine, library, ToolBox(library, writer, index, jobs))
    reply = assistant.send("what books do I have?", on_tool=lambda n, a, r: tools_called.append(n))
    assert reply == "You have 1 book: The Midnight Vault."
    assert tools_called == ["list_books"]
    assert [m.role for m in library.recent_messages("chat")] == ["user", "assistant"]


def test_toolbox_validation_and_reading(studio):
    library, _, index, writer, jobs, _ = studio
    tools = ToolBox(library, writer, index, jobs)
    assert "error" in tools.call("read_chapter", {})  # missing required chapter
    assert "error" in tools.call("no_such_tool")
    book = writer.create_book("heist")
    writer.draft_chapter(book.id, 1)
    out = tools.call("read_chapter", {"book": "midnight", "chapter": 1, "max_words": 150})
    assert out["text"] and out["next_paragraph"] is not None
    more = tools.call("read_chapter", {"chapter": 1, "start_paragraph": out["next_paragraph"]})
    assert more["text"] != out["text"]
    assert tools.call("add_note", {"text": "twist: the guard is her brother"})["saved"]
    assert library.list_notes(book.id)[0].text.startswith("twist")
    sel = {d["name"] for d in tools.select("read me chapter two")}
    assert "play_audiobook" in sel and "export_book" not in sel


def test_semantic_and_keyword_search(library, engine):
    book = library.create_book("S", "fantasy")
    library.set_outline(book.id, [{"number": 1, "title": "A", "outline": ""}, {"number": 2, "title": "B", "outline": ""}])
    library.save_chapter_text(library.get_chapter(book.id, 1).id, "The dragon breathed fire over the ship.")
    library.save_chapter_text(library.get_chapter(book.id, 2).id, "A storm broke over the quiet harbor.")
    index = StoryIndex(library, engine)
    index.index_book(book.id)
    assert index.embed_pending() == 2
    hits = index.search("dragon fire", book.id)
    assert hits[0].chapter_number == 1
    assert index.index_book(book.id) == 0  # unchanged passages keep their embeddings
    ctx = ContextBuilder(library, index).chapter_sections(book.id, 2)
    assert any(s.name.startswith("End of the previous chapter") for s in ctx)
