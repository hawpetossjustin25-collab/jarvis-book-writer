"""The background researcher: noticing interests, research rounds, the notebook, Jarvis's tools and the loop."""
import threading
import time

import pytest

from fakes import FakeGemini
from jarvis_books.assistant.permissions import TOOL_LEVELS
from jarvis_books.assistant.research_tools import RESEARCH_TOOLS
from jarvis_books.errors import NotFound, ValidationError
from jarvis_books.llm.errors import LLMError
from jarvis_books.llm.models import LLMResponse
from jarvis_books.research import ACTIVE_AT, DAY, DRY_LIMIT, ResearchClock, Researcher
from jarvis_books.studio import Studio


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class Model:
    """The local research model: answers what the test queued, by purpose."""

    def __init__(self, model="qwen3:4b", ready=True):
        self.model, self.available, self.problem = model, ready, "" if ready else "isn't downloaded yet"
        self.answers: dict[str, list] = {}
        self.asked = []

    def initialize(self):
        return self.available

    def queue(self, purpose, *answers):
        self.answers.setdefault(purpose, []).extend(answers)

    def generate(self, req):
        self.asked.append(req)
        q = self.answers.get(req.purpose) or []
        if not q:
            raise AssertionError(f"nothing queued for {req.purpose}")
        return LLMResponse(text="", model=self.model, data=q.pop(0))


class Web:
    def __init__(self):
        self.pages = []
        self.asked = []

    def research(self, question, pages=4, max_chars=0):
        self.asked.append(question)
        got = self.pages.pop(0) if self.pages else []
        return {"question": question, **got} if isinstance(got, dict) else {"question": question, "sources": got}


def page(n):
    return {"title": f"Page {n}", "url": f"https://example.com/{n}", "text": f"fact number {n} " * 30}


@pytest.fixture
def res(library, tmp_path):
    clock, settings = Clock(), {"researcher": "1", "research_pace": "10", "research_brain": "local"}
    r = Researcher(library.db, library, Web(), lambda: settings, local=Model(), clock=clock,
                   notes_dir=tmp_path / "notes")
    r.test = type("T", (), {"clock": clock, "settings": settings})
    return r


def say(library, *texts, channel="chat"):
    for t in texts:
        library.add_message(channel, "user", t)
        library.add_message(channel, "assistant", "ok")


def test_topics_add_find_change_and_drop(res):
    t = res.add_topic("Solar generators", focus="cheap portable ones")
    assert t["new"] and t["status"] == "active" and t["source"] == "you"
    assert res.topic("solar")["id"] == t["id"] == res.topic(str(t["id"]))["id"]
    again = res.add_topic("solar generators", focus="")
    assert not again["new"] and again["focus"] == "cheap portable ones"
    assert res.update("solar", status="paused")["status"] == "paused"
    assert res.update("solar", focus="for camping", kind="project")["kind"] == "project"
    with pytest.raises(ValidationError):
        res.update("solar", status="sleeping")
    with pytest.raises(ValidationError):
        res.add_topic("  ")
    res.delete("solar")
    with pytest.raises(NotFound):
        res.topic("solar")
    assert [a["action"] for a in res.activity()][:1] == ["dropped"]


def test_it_notices_what_the_author_keeps_coming_back_to(res, library):
    say(library, "hi")
    assert res.observe() is None  # not enough new to read yet
    say(library, "what tax liens are for sale in Atlanta", "any cheap houses in Phoenix?")
    res.local.queue("researcher interests",
                    {"interests": [{"topic": "Tax lien investing", "why": "asked about Atlanta liens",
                                    "strength": 2, "kind": "topic"}]})
    noticed = res.observe()
    assert noticed[0]["status"] == "watching" and noticed[0]["source"] == "noticed"
    assert "Atlanta" in res.local.asked[-1].instruction and "ok" not in res.local.asked[-1].instruction
    assert res.observe() is None  # the cursor moved past what was read
    say(library, "more tax liens", "best lien deals", "liens in Minneapolis")
    res.local.queue("researcher interests",
                    {"interests": [{"topic": "tax lien investing", "why": "", "strength": 3, "kind": "topic"},
                                   {"topic": "  ", "why": "", "strength": 3, "kind": "topic"}]})
    t = res.observe()[0]
    assert t["status"] == "active" and t["priority"] >= ACTIVE_AT and len(res.topics()) == 1
    assert "keep coming back" in res.activity()[0]["detail"]


def test_interest_fades_when_the_author_moves_on(res, library):
    say(library, "a", "b", "c")
    res.local.queue("researcher interests", {"interests": [{"topic": "Beekeeping", "why": "", "strength": 3,
                                                             "kind": "topic"}]})
    res.observe()
    res.test.clock.now += 14 * DAY
    assert res.topic("beekeeping")["priority"] == pytest.approx(0.75)


def test_a_round_saves_real_findings_and_drops_made_up_links(res):
    res.add_topic("Solar generators", focus="cheap portable ones")
    res.web.pages = [[page(1), page(2)]]
    res.local.queue("researcher question", {"question": "best cheap portable solar generator 2026"})
    res.local.queue("researcher notes", {"findings": [
        {"title": "Jackery 300", "summary": "About $250.", "url": "https://example.com/1"},
        {"title": "Invented", "summary": "Not a page we read.", "url": "https://made-up.example/x"}]})
    out = res.step()
    assert out["question"] == "best cheap portable solar generator 2026"
    assert [f["url"] for f in out["added"]] == ["https://example.com/1"]
    assert res.web.asked == ["best cheap portable solar generator 2026"]
    notes = res.findings("solar")
    assert len(notes) == 1 and notes[0]["title"] == "Jackery 300"
    text = (res.notes_dir / "solar-generators.md").read_text(encoding="utf-8")
    assert "Jackery 300" in text and "https://example.com/1" in text
    t = res.topic("solar")
    assert t["rounds"] == 1 and t["dry_rounds"] == 0 and t["next_run_at"] == res.test.clock.now


def test_pages_already_read_are_skipped_and_dry_topics_rest(res):
    res.add_topic("Solar generators")
    res.web.pages = [[page(1)]]
    res.local.queue("researcher question", {"question": "q1"})
    res.local.queue("researcher notes", {"findings": [{"title": "A", "summary": "B", "url": "https://example.com/1"}]})
    res.step()
    for i in range(DRY_LIMIT):
        res.web.pages = [[page(1)]]  # the same page again: nothing new, so no digest is asked for
        res.local.queue("researcher question", {"question": f"again {i}"})
        res.test.clock.now += DAY
        assert res.step("solar")["added"] == []
    t = res.topic("solar")
    assert t["dry_rounds"] == DRY_LIMIT and t["next_run_at"] == res.test.clock.now + DAY
    assert res.next_topic() is None  # resting
    assert res.update("solar", focus="foldable panels")["next_run_at"] == 0  # a new focus wakes it


def test_most_wanted_due_topic_goes_first(res):
    res.add_topic("A topic")
    res.add_topic("B topic")
    res.db.execute("UPDATE research_topics SET interest=20 WHERE name='B topic'")
    assert res.next_topic()["name"] == "B topic"
    res.update("B topic", status="paused")
    assert res.next_topic()["name"] == "A topic"


def test_it_uses_the_backup_model_when_the_research_model_is_missing(res):
    res.local = Model("qwen3:4b", ready=False)
    res.backup = Model("lfm3")
    res.add_topic("Solar")
    res.backup.queue("researcher question", {"question": "q"})
    res.step()
    assert res.backup.asked and "lfm3 is used" in res.problem
    res.backup = None
    with pytest.raises(LLMError):
        res.step("solar")


def test_gemini_only_when_the_author_picks_it(res):
    class Brain:
        def __init__(self):
            self.asked = []

        def generate(self, req):
            self.asked.append(req)
            return LLMResponse(text="", model="gemini", data={"question": "from gemini"})

    res.brain = Brain()
    res.add_topic("Solar")
    res.local.queue("researcher question", {"question": "from local"})
    assert res.step()["question"] == "from local" and not res.brain.asked
    res.test.settings["research_brain"] = "gemini"
    assert res.step("solar")["question"] == "from gemini"


def test_jarvis_sees_matching_notes_with_links(res):
    assert res.prompt("anything") == ""
    res.add_topic("Solar generators")
    res.db.execute("INSERT INTO research_findings (topic_id, question, title, summary, url, created_at) VALUES "
                   "(1, 'q', 'Jackery 300 price', 'About $250 for the solar generator.', 'https://e.com/j', 1)")
    p = res.prompt("how much is a solar generator")
    assert "Solar generators (active, 1 notes)" in p and "https://e.com/j" in p
    assert "https://e.com/j" not in res.prompt("tell me about dragons")


def test_clock_runs_rounds_and_waits_when_no_model_answers(res):
    calls = []

    def tick():
        calls.append(time.time())
        if len(calls) == 1:
            raise LLMError("offline", kind="unavailable")
        return None

    res.tick = tick
    res.test.settings["research_pace"] = "1"
    clock = ResearchClock(res, start_delay=0)
    clock.start()
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    assert clock.failures == 1 and "offline" in res.problem
    clock.poke()
    deadline = time.time() + 5
    while len(calls) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert len(calls) == 2 and clock.failures == 0
    clock.stop()
    assert not clock.running


def test_clock_skips_rounds_while_switched_off(res):
    called = threading.Event()
    res.tick = lambda: called.set()
    res.test.settings["researcher"] = "0"
    clock = ResearchClock(res, start_delay=0)
    clock.start()
    time.sleep(0.2)
    clock.stop()
    assert not called.is_set()


@pytest.fixture
def studio(tmp_path):
    from conftest import hardware
    s = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(),
               engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None})
    s.start(probe=False)
    yield s
    s.close()


def test_tool_levels_and_studio_wiring(studio):
    assert set(RESEARCH_TOOLS) <= set(TOOL_LEVELS)
    assert TOOL_LEVELS["drop_research"] == "change" and TOOL_LEVELS["research_topic"] == "safe"
    assert studio.tools.researcher is studio.researcher is studio.war_tools.researcher
    assert studio.assistant.researcher is studio.researcher
    assert studio.researcher.local.model.startswith("qwen3") and studio.researcher.backup is studio.local
    assert studio.settings()["researcher"] == "1"
    names = [d["name"] for d in studio.tools.select("keep researching solar generators for me")]
    assert "research_topic" in names


def test_jarvis_tools_drive_the_researcher(studio):
    t = studio.tools.call("research_topic", {"name": "Solar generators", "kind": "project", "focus": "DIY"})
    assert t["new"] and t["kind"] == "project" and t["source"] == "Jarvis"
    assert studio.tools.call("change_research", {"topic": "solar", "status": "paused"})["status"] == "paused"
    status = studio.tools.call("research_status")
    assert status["on"] and status["topics"][0]["name"] == "Solar generators" and "qwen3" in status["thinks_with"]
    assert studio.tools.call("research_notes", {"topic": "solar"})["note"].startswith("nothing found yet")
    off = studio.tools.call("researcher_switch", {"on": False, "minutes": 30, "brain": "gemini"})
    assert not off["on"] and off["minutes_between_rounds"] == 30 and off["thinks_with"] == "Gemini"
    asked = studio.tools.call("drop_research", {"topic": "solar"})
    assert asked.get("needs_approval")  # deleting notes waits for the author's yes
    assert studio.tools.run("drop_research", {"topic": "solar"})["dropped"] == "Solar generators"


def test_research_now_runs_a_round_as_a_job(studio):
    studio.researcher.local, studio.researcher.web = Model(), Web()
    studio.researcher.add_topic("Solar")
    studio.researcher.local.queue("researcher question", {"question": "solar q"})
    out = studio.tools.call("research_now", {"topic": "solar"})
    job = studio.jobs.get(out["job_id"])
    deadline = time.time() + 10
    while job.status in ("queued", "running") and time.time() < deadline:
        time.sleep(0.02)
    assert job.status == "done" and job.result["question"] == "solar q"


def test_chat_prompt_includes_the_researchers_notes(studio):
    studio.researcher.add_topic("Solar generators")
    studio.db.execute("INSERT INTO research_findings (topic_id, question, title, summary, url, created_at) VALUES "
                      "(1, 'q', 'Jackery price', 'A solar generator for $250.', 'https://e.com/j', 1)")
    seen = []
    real = studio.brain.generate

    def spy(req):
        seen.append(req.system)
        return real(req)

    studio.brain.generate = spy
    studio.assistant.send("what solar generator should I buy")
    assert "https://e.com/j" in seen[0]


def test_groq_researches_when_picked_and_is_the_last_resort_otherwise(res):
    res.cloud = Model("openai/gpt-oss-20b")
    res.add_topic("Solar")
    res.test.settings["research_brain"] = "groq"
    res.cloud.queue("researcher question", {"question": "from groq"})
    assert res.step()["question"] == "from groq" and not res.local.asked
    res.test.settings["research_brain"] = "local"
    res.local = Model("qwen3:4b", ready=False)
    res.cloud.queue("researcher question", {"question": "groq rescue"})
    assert res.step("solar")["question"] == "groq rescue" and "openai/gpt-oss-20b is used" in res.problem


def test_a_refusing_model_hands_over_to_the_next(res):
    class Busy(Model):
        def generate(self, req):
            raise LLMError("free limit used up", kind="rate")

    res.local, res.backup = Busy(), Model("lfm3")
    res.add_topic("Solar")
    res.backup.queue("researcher question", {"question": "q"})
    assert res.step()["question"] == "q" and "free limit used up" in res.problem


def test_links_copied_back_slightly_differently_still_count_and_dry_rounds_say_why(res):
    res.add_topic("Solar generators")
    res.web.pages = [[{"title": "Guide", "url": "https://www.example.com/guide/", "text": "facts " * 40}]]
    res.local.queue("researcher question", {"question": "q1"})
    res.local.queue("researcher notes", {"findings": [
        {"title": "Guide", "summary": "Useful.", "url": "http://example.com/guide"}]})
    out = res.step()
    assert [f["url"] for f in out["added"]] == ["https://www.example.com/guide/"]  # the page's own link is kept

    res.web.pages = [[]]
    res.local.queue("researcher question", {"question": "q2"})
    out = res.step("solar")
    assert out["added"] == [] and "search found nothing" in out["why"]
    assert "search found nothing" in res.activity(1)[0]["detail"]

    res.web.pages = [[page(7)]]
    res.local.queue("researcher question", {"question": "q3"})
    res.local.queue("researcher notes", {"findings": [{"title": "X", "summary": "Y", "url": "https://other.example/7"}]})
    why = res.step("solar")["why"]
    assert "didn't match" in why and "https://example.com/7" in why  # names the pages it read


def test_a_search_limit_is_no_strike_against_the_topic(res):
    from jarvis_books.research import BLOCKED_WAIT_S
    res.add_topic("Solar generators")
    res.web.pages = [{"sources": [], "blocked": True, "note": "the search engines are limiting searches right now"}]
    res.local.queue("researcher question", {"question": "q1"})
    out = res.step()
    t = res.topic("solar")
    assert out["added"] == [] and "limiting searches" in out["why"]
    assert t["dry_rounds"] == 0 and t["next_run_at"] == res.test.clock.now + BLOCKED_WAIT_S
    assert res.next_topic() is None
    res.test.clock.now += BLOCKED_WAIT_S
    assert res.next_topic()["name"] == "Solar generators"


def test_groq_waits_out_its_per_minute_limit_instead_of_handing_over_to_the_slow_model(res):
    class Minute(Model):
        def generate(self, req):
            if not self.asked:
                self.asked.append(req)
                raise LLMError("tokens per minute used up", kind="rate", retry_after_s=7.0)
            return super().generate(req)

    res.cloud, slept = Minute("openai/gpt-oss-20b"), []
    res.pause = slept.append
    res.add_topic("Solar")
    res.test.settings["research_brain"] = "groq"
    res.cloud.queue("researcher question", {"question": "after the wait"})
    assert res.step()["question"] == "after the wait"
    assert slept == [8.0] and not res.local.asked and res.problem == ""


def test_groq_out_for_the_day_hands_over_without_waiting(res):
    class Daily(Model):
        def generate(self, req):
            raise LLMError("requests per day used up", kind="rate", retry_after_s=3600.0)

    res.cloud, slept = Daily("openai/gpt-oss-20b"), []
    res.pause = slept.append
    res.add_topic("Solar")
    res.test.settings["research_brain"] = "groq"
    res.local.queue("researcher question", {"question": "local"})
    assert res.step()["question"] == "local" and slept == [] and "requests per day" in res.problem


def test_a_local_model_out_of_room_is_not_waited_on(res):
    class Busy(Model):
        def generate(self, req):
            raise LLMError("busy", kind="rate", retry_after_s=1.0)

    res.local, res.backup, slept = Busy(), Model("lfm3"), []
    res.pause = slept.append
    res.add_topic("Solar")
    res.backup.queue("researcher question", {"question": "q"})
    assert res.step()["question"] == "q" and slept == []
