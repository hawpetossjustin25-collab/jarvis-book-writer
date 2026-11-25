import pytest

from fakes import FakeGemini
from jarvis_books.assistant import ToolBox
from jarvis_books.assistant.nexus_tools import NEXUS_TOOLS, UNTRUSTED
from jarvis_books.assistant.permissions import NEEDS_YES, TOOL_LEVELS
from jarvis_books.jobs import JobManager
from jarvis_books.memory import StoryIndex
from jarvis_books.nexus.os1 import OS1
from jarvis_books.studio import Studio
from jarvis_books.writer import BookWriter
from nexus_site import FAST, FakeWeb, page

A = "https://atl.example/"


def web():
    return FakeWeb({A: page("Atlanta Tax Sales Guide", "How the Fulton county tax sale works.",
                            (("/deeds", "Tax deeds"),)),
                    A + "deeds": page("Tax deeds in Georgia", "A tax deed transfers the property after the sale.")})


@pytest.fixture
def tools(library, make_engine, tmp_path):
    engine = make_engine(FakeGemini())
    index = StoryIndex(library, engine)
    jobs = JobManager(library, workers=1)
    box = ToolBox(library, BookWriter(library, engine, index), index, jobs)
    box.nexus = OS1(tmp_path / "nx", FAST, fetcher_factory=web().factory(),
                    seed_search=lambda topic, n: [A + "deeds"])
    yield box
    jobs.shutdown(wait=True)
    box.nexus.close()


def test_levels_never_need_a_yes_for_read_only_web_indexing():
    assert set(NEXUS_TOOLS) <= set(TOOL_LEVELS)
    assert not any(TOOL_LEVELS[t] in NEEDS_YES for t in NEXUS_TOOLS)
    assert TOOL_LEVELS["web_index_search"] == "read" and TOOL_LEVELS["web_index_explore"] == "safe"


def test_explore_then_search_then_page_through_the_tool_gate(tools):
    started = tools.call("web_index_explore", {"url": A, "topic": "tax deeds", "pages": 10})
    assert started["started"] and started["job_id"]
    job = tools.jobs.wait(started["job_id"], timeout=30)
    assert job.status == "done", job.error
    assert job.result["pages_fetched"] == 2 and job.result["status"] == "done"
    found = tools.call("web_index_search", {"query": "tax deed"})
    assert found["results"][0]["url"] == A + "deeds" and found["indexed_pages"] == 2
    assert found["note"] == UNTRUSTED and found["results"][0]["last_checked"]
    detail = tools.call("web_index_page", {"url": A + "deeds"})
    assert detail["title"] == "Tax deeds in Georgia" and detail["links_in"] == 1
    assert detail["visits"][0]["outcome"] == "new" and detail["note"] == UNTRUSTED
    status = tools.call("web_index_status", {})
    assert status["searchable"] == 2 and status["recent_explorations"][0]["topic"] == "tax deeds"
    assert status["recent_searches"] == ["tax deed"]


def test_topic_only_exploration_uses_web_search_seeds(tools):
    job = tools.jobs.wait(tools.call("web_index_explore", {"topic": "georgia deeds", "depth": 0})["job_id"], 30)
    assert job.status == "done" and job.result["pages_fetched"] == 1


def test_bad_requests_get_clear_errors(tools):
    assert "web address" in tools.call("web_index_explore", {"url": "not a url"})["error"]
    assert "topic" in tools.call("web_index_explore", {})["error"]
    assert "no stopped" in tools.call("web_index_continue", {})["error"]
    assert "isn't in the index" in tools.call("web_index_page", {"url": "https://nope.example/"})["error"]
    empty = tools.call("web_index_search", {"query": "anything"})
    assert empty["results"] == [] and "web_index_explore" in empty["note"]
    tools.nexus._crawl_lock.acquire()
    try:
        assert "already running" in tools.call("web_index_explore", {"url": A})["error"]
    finally:
        tools.nexus._crawl_lock.release()


def test_refresh_with_nothing_due_finishes_cleanly(tools):
    job = tools.jobs.wait(tools.call("web_index_refresh", {})["job_id"], 30)
    assert job.status == "done" and job.result["pages_fetched"] == 0


def test_web_index_tools_come_up_for_the_small_model(tools):
    names = {d["name"] for d in tools.select("crawl https://atl.example and index it")}
    assert {"web_index_explore", "web_index_search"} <= names


def test_studio_wires_nexus(tmp_path):
    from conftest import hardware
    s = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(),
               engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None})
    s.start(probe=False)
    try:
        assert s.tools.nexus is s.nexus is s.war_tools.nexus
        assert (tmp_path / "home" / "nexus.db").exists()
        assert s.tools.call("web_index_status", {})["pages"] == 0
    finally:
        s.close()
