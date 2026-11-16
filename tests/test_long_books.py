"""Long books: up to 8,000 chapters, outlined ahead in batches and written continuously."""
import json
import re

import pytest

from fakes import body
from jarvis_books.assistant import ToolBox
from jarvis_books.jobs import JobManager
from jarvis_books.llm.errors import LLMError
from jarvis_books.memory import ContextBuilder, StoryIndex
from jarvis_books.writer import BookWriter
from jarvis_books.writer.pipeline import MAX_CHAPTERS, OUTLINE_BATCH
from test_writer import story_responder


def outline_asks(fake):
    return [json.dumps(c[1]) for c in fake.calls if "exactly" in json.dumps(c[1]) and "outline" in json.dumps(c[1])]


def long_responder(model_id, payload):
    """Like the story fake, but outlines exactly the chapter range it is asked for."""
    schema = (payload.get("generationConfig") or {}).get("responseSchema") or {}
    chapters = schema.get("properties", {}).get("chapters", {})
    if chapters.get("type") == "ARRAY":
        m = re.search(r"chapters (\d+)-(\d+)", json.dumps(payload))
        lo, hi = (int(m.group(1)), int(m.group(2))) if m else (1, 3)
        return body(json.dumps({"chapters": [
            {"number": n, "title": f"Part {n}", "outline": f"Mara faces trial {n}.", "beat": "", "target_words": 1500}
            for n in range(lo, hi + 1)]}))
    return story_responder(model_id, payload)


@pytest.fixture
def studio(library, make_engine, fake):
    fake.responder = long_responder
    engine = make_engine(fake)
    index = StoryIndex(library, engine)
    writer = BookWriter(library, engine, index)
    writer._wait = lambda cancel, seconds: False  # no real sleeping while a run waits out a limit
    jobs = JobManager(library, workers=1)
    yield library, engine, writer, jobs, fake
    jobs.shutdown(wait=True)


def test_a_long_book_is_outlined_in_batches_not_all_at_once(studio):
    library, _, writer, _, fake = studio
    book = writer.create_book("an endless heist serial", chapters=3000)
    assert library.chapter_count(book.id) == OUTLINE_BATCH
    assert writer.planned_chapters(book.id) == 3000 and writer.remaining(book.id) == 3000
    assert any("chapters 1-25" in a and "3,000-chapter" in a for a in outline_asks(fake))
    writer.extend_outline(book.id)
    assert library.chapter_count(book.id) == 2 * OUTLINE_BATCH
    assert library.get_chapter(book.id, 26).title == "Part 26"
    assert "Continue straight on from chapter 25" in outline_asks(fake)[-1]


def test_writing_runs_past_the_outline_and_stops_at_the_planned_end(studio):
    library, _, writer, _, _ = studio
    book = writer.create_book("serial", chapters=30)
    done = writer.write_next_chapters(book.id, 100)
    assert [c.number for c in done] == list(range(1, 31))
    assert library.chapter_count(book.id) == 30 and writer.remaining(book.id) == 0
    assert library.get_book(book.id).status == "complete"


def test_a_run_waits_out_limits_and_keeps_going(studio):
    library, _, writer, _, _ = studio
    book = writer.create_book("serial")
    real, fails, waits = writer.write_chapter, [2], []
    writer._wait = lambda cancel, seconds: waits.append(seconds) or False

    def flaky(*a, **kw):
        if fails[0]:
            fails[0] -= 1
            raise LLMError("Neither brain could answer. Gemini: daily limit", kind="unavailable")
        return real(*a, **kw)

    writer.write_chapter = flaky
    seen = []
    done = writer.write_next_chapters(book.id, 2, progress=lambda p, m: seen.append(m))
    assert [c.number for c in done] == [1, 2] and waits == [60, 300]
    assert any("Trying again in 1 min" in m for m in seen)


def test_a_real_bug_is_not_waited_out_for_a_day(studio):
    _, _, writer, _, _ = studio
    book = writer.create_book("serial")
    writer.write_chapter = lambda *a, **kw: (_ for _ in ()).throw(LLMError("bad request", kind="invalid"))
    with pytest.raises(LLMError):
        writer.write_next_chapters(book.id, 3)


def test_book_length_can_grow_to_8000_but_not_shrink(studio):
    library, _, writer, _, _ = studio
    book = writer.create_book("serial")
    assert writer.set_length(book.id, MAX_CHAPTERS) == 8000 and writer.remaining(book.id) == 8000
    with pytest.raises(ValueError):
        writer.set_length(book.id, 8001)
    with pytest.raises(ValueError):
        writer.set_length(book.id, 2)


def test_jarvis_writes_thousands_in_one_run_and_it_resumes_after_a_restart(studio, library):
    _, engine, writer, jobs, _ = studio
    book = writer.create_book("serial", chapters=4000)
    tools = ToolBox(library, writer, StoryIndex(library, engine), jobs)
    tools.gate.decide = lambda name: "free"
    gate = __import__("threading").Event()
    real = writer.write_chapter
    writer.write_chapter = lambda *a, **kw: (gate.wait(5), real(*a, **kw))[1]
    out = tools.call("write_chapters", {"book": book.title, "count": 3000})
    assert out["started"] and out["count"] == 3000 and "continuous" in out
    assert "already being written" in tools.call("write_chapters", {"book": book.title, "count": 2})["error"]
    assert library.get_book(book.id).settings["writing_run"]["until"] == 3000
    jobs.closing = True  # the app closes mid-run: the run is kept for next time
    jobs.cancel(out["job_id"])
    gate.set()
    jobs.wait(out["job_id"], timeout=30)
    assert library.get_book(book.id).settings["writing_run"]["until"] == 3000
    jobs.closing = False
    writer.write_chapter = real
    asked = []
    writer.write_next_chapters = lambda b, count, *a, **kw: asked.append(count) or []
    resumed = tools.resume_writing()  # what the Studio does when the app starts again
    assert len(resumed) == 1 and jobs.wait(resumed[0], timeout=30).status == "done"
    assert asked == [3000 - written(library, book.id)]  # it carries on from where it stopped, to the same end
    assert library.get_book(book.id).settings["writing_run"] is None
    assert tools.resume_writing() == []


def written(library, book_id):
    return library.book_stats(book_id)["accepted"]


def test_stopping_a_run_keeps_finished_chapters_and_forgets_the_run(studio, library):
    _, engine, writer, jobs, _ = studio
    book = writer.create_book("serial", chapters=60)
    tools = ToolBox(library, writer, StoryIndex(library, engine), jobs)
    tools.gate.decide = lambda name: "free"
    first = [True]
    real = writer.write_chapter

    def one_then_stop(*a, **kw):
        ch = real(*a, **kw)
        if first[0]:
            first[0] = False
            tools.call("stop_writing", {"book": book.title})
        return ch

    writer.write_chapter = one_then_stop
    out = tools.call("write_chapters", {"book": book.title, "count": 0})
    assert out["count"] == 60
    job = jobs.wait(out["job_id"], timeout=30)
    assert job.status == "cancelled" and written(library, book.id) == 1
    assert library.get_book(book.id).settings["writing_run"] is None


def test_context_for_chapter_3000_stays_small(library):
    book = library.create_book("Serial", "fantasy", "idea")
    library.set_outline(book.id, [{"number": n, "title": f"T{n}", "outline": f"o{n}"} for n in range(1, 3001)])
    for n in (1, 2999):
        ch = library.get_chapter(book.id, n)
        library.update_chapter(ch.id, summary=f"summary {n}", status="accepted")
    ctx = ContextBuilder(library, StoryIndex(library, None))
    story = ctx.story_so_far(book.id, 3000)
    assert "summary 2999" in story and "Earlier milestones" in story and len(story.splitlines()) < 120
    outline = ctx.outline_text(book.id, around=3000)
    assert "Ch 3000 T3000" in outline and "Ch 1 T1:" not in outline and len(outline.splitlines()) < 70
