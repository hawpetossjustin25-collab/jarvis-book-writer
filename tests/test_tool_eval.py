"""The tool-calling test set: cases are fair, scoring is right, and a brain is measured without running tools."""
import json

import pytest

from jarvis_books.assistant.tools import DECLARATIONS, ToolBox
from jarvis_books.llm.errors import LLMError
from jarvis_books.llm.models import LLMResponse
from jarvis_books.llm.tool_eval import (ToolCase, check_cases, load_cases, route_coverage, run_tool_eval,
                                        score)


def case(tools, args=None, cid="x-01", request="do it"):
    return ToolCase(id=cid, category=cid.split("-")[0], request=request,
                    tools=tuple(tools if isinstance(tools, list) else [tools]), args=args or {})


def test_shipped_cases_are_fair_and_cover_every_area():
    cases = load_cases()
    assert len(cases) >= 70
    assert check_cases(cases, DECLARATIONS) == []
    areas = {c.category for c in cases}
    assert {"books", "memory", "app", "computer", "web", "planner", "research", "game", "chat"} <= areas
    assert any(not c.wants_tool for c in cases)


def test_check_cases_finds_unfair_cases():
    bad = [case("no_such_tool", cid="a-01"), case("game_bid", {"price": 5}, cid="a-02"),
           case("media_control", {"action": "pause"}, cid="a-03"), case(None, {"x": 1}, cid="a-04"),
           case("list_books", cid="a-04")]
    problems = check_cases(bad, DECLARATIONS)
    assert any("unknown tool no_such_tool" in p for p in problems)
    assert any("game_bid has no argument 'price'" in p for p in problems)
    assert any("'pause' is not one of" in p for p in problems)
    assert any("no-tool case" in p for p in problems)
    assert any("duplicate id" in p for p in problems)


def test_load_cases_says_which_line_is_broken(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps({"id": "a", "category": "c", "request": "r", "tool": "list_books"}) + "\n{oops\n")
    with pytest.raises(ValueError, match="line 2"):
        load_cases(path)


@pytest.mark.parametrize("calls,ok,why", [
    ([{"name": "game_bid", "args": {"amount": 2500}}], True, "right tool"),
    ([{"name": "game_bid", "args": {"amount": "$2,500"}}], True, "right tool"),
    ([{"name": "game_bid", "args": {"amount": 2600}}], False, "wanted 2500"),
    ([{"name": "game_bid", "args": {}}], False, "without amount"),
    ([{"name": "game_pass", "args": {}}], False, "called game_pass"),
    ([], False, "no tool call"),
])
def test_score_checks_tool_and_details(calls, ok, why):
    got, reason = score(case("game_bid", {"amount": 2500}), calls)
    assert got is ok and why in reason


def test_score_strings_booleans_alternatives_and_no_tool():
    assert score(case("open_book", {"book": "shadow harbor"}), [{"name": "open_book", "args": {"book": "Shadow Harbor"}}])[0]
    assert score(case("researcher_switch", {"on": False}), [{"name": "researcher_switch", "args": {"on": "off"}}])[0]
    assert not score(case("researcher_switch", {"on": False}), [{"name": "researcher_switch", "args": {"on": "on"}}])[0]
    assert score(case(["read_chapter", "play_audiobook"], {"chapter": 4}), [{"name": "play_audiobook", "args": {}}])[0]
    assert score(case(None), [])[0]
    ok, why = score(case(None), [{"name": "list_books", "args": {}}])
    assert not ok and "wanted no tool" in why


class ScriptedBrain:
    """Answers each request from a table: the request text -> function calls, or an exception."""
    def __init__(self, table, clock):
        self.table, self.clock, self.requests = table, clock, []

    def generate(self, request):
        self.requests.append(request)
        text = request.history[-1]["parts"][0]["text"]
        answer = self.table[text]
        self.clock.now += 0.5
        if isinstance(answer, Exception):
            raise answer
        return LLMResponse(text="" if answer else "Doing well!", model="fake-brain", function_calls=answer)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_run_tool_eval_scores_a_brain_without_running_any_tool():
    cases = [case("game_bid", {"amount": 2500}, "game-01", "Bid 2500."),
             case("list_books", None, "books-01", "What books do I have?"),
             case(None, None, "chat-01", "Hi Jarvis"),
             case("web_search", None, "web-01", "Search the web for cats")]
    clock = Clock()
    brain = ScriptedBrain({"Bid 2500.": [{"name": "game_bid", "args": {"amount": 2500}}],
                           "What books do I have?": [{"name": "open_book", "args": {"book": "x"}}],
                           "Hi Jarvis": [],
                           "Search the web for cats": LLMError("overloaded", kind="overload")}, clock)
    seen = []
    report = run_tool_eval(brain, DECLARATIONS, cases, system="You are Jarvis.",
                           select=lambda text: [d for d in DECLARATIONS if d["name"] == "game_bid"],
                           progress=lambda p, m: seen.append(m), clock=clock)
    assert report.passed == 2 and len(report.results) == 4 and report.accuracy == 0.5
    assert report.by_category() == {"books": {"passed": 0, "total": 1}, "chat": {"passed": 1, "total": 1},
                                    "game": {"passed": 1, "total": 1}, "web": {"passed": 0, "total": 1}}
    assert report.latency() == {"median_s": 0.5, "p90_s": 0.5}
    assert report.models() == {"fake-brain": 3}
    assert [f["id"] for f in report.failures()] == ["books-01", "web-01"]
    assert report.results[3].reason == "brain error"
    first = brain.requests[0]
    assert first.tools is DECLARATIONS and [d["name"] for d in first.local_tools] == ["game_bid"]
    assert first.temperature == 0.0 and "Tool test" in first.system
    assert seen[-1] == report.summary() and report.summary().startswith("2 of 4 tool tests right (50%)")
    assert json.loads(json.dumps(report.to_dict()))["accuracy"] == 0.5


def test_run_tool_eval_stops_when_cancelled():
    clock = Clock()
    brain = ScriptedBrain({"a": [], "b": []}, clock)
    calls = iter([False, True])
    report = run_tool_eval(brain, DECLARATIONS, [case(None, cid="c-01", request="a"), case(None, cid="c-02", request="b")],
                           system="", cancel=lambda: next(calls), clock=clock)
    assert [r.id for r in report.results] == ["c-01"]


def test_route_coverage_of_the_small_brains_tool_list():
    toolbox = ToolBox.__new__(ToolBox)
    toolbox.war_session_id = None
    coverage = route_coverage(load_cases(), toolbox.select)
    assert coverage["share"] >= 0.95, coverage["missing"]
    narrow = route_coverage([case("game_bid", cid="g-01"), case("list_books", cid="b-01"), case(None, cid="c-01")],
                            lambda text: [{"name": "list_books"}])
    assert narrow == {"covered": 1, "total": 2, "share": 0.5, "missing": ["g-01"]}


class StubBrain:
    def __init__(self):
        self.picked = []

    def only(self, which):
        self.picked.append(which)
        return self

    def generate(self, request):
        text = request.history[-1]["parts"][0]["text"]
        calls = [{"name": "list_books", "args": {}}] if "books" in text.lower() else []
        return LLMResponse(text="ok", model="stub", function_calls=calls)


def test_jarvis_tool_runs_the_test_set_as_a_job_and_saves_the_report(tmp_path):
    import threading
    from types import SimpleNamespace

    from jarvis_books.assistant.permissions import SAFE, level_of

    brain = StubBrain()
    submitted = []
    toolbox = ToolBox.__new__(ToolBox)
    toolbox.war_session_id = None
    toolbox.settings_store = SimpleNamespace(brain=brain, home=tmp_path)
    toolbox.jobs = SimpleNamespace(submit=lambda kind, title, fn: submitted.append((kind, title, fn))
                                   or SimpleNamespace(id="job1"))
    assert level_of("test_tool_calling") == SAFE
    assert "test_tool_calling" in {d["name"] for d in toolbox.select("run the tool calling test on lfm3")}
    out = toolbox._t_test_tool_calling("lfm3")
    assert out["job_id"] == "job1" and brain.picked == ["local"]
    kind, title, fn = submitted[0]
    assert kind == "tooltest" and "lfm3" in title
    result = fn(lambda p, m: None, threading.Event())
    assert result.startswith(f"{sum(1 for c in load_cases() if c.tools[0] in ('list_books', None))} of")
    saved = list((tmp_path / "tool_tests").glob("*-lfm3.json"))
    assert len(saved) == 1 and json.loads(saved[0].read_text())["total"] == len(load_cases())
    toolbox._t_test_tool_calling()
    assert brain.picked == ["local"]  # automatic uses the brain with its usual fallback


def test_a_rate_limited_brain_is_waited_for_and_asked_again():
    calls, waits = [], []

    class Busy:
        def generate(self, request):
            calls.append(1)
            if len(calls) < 3:
                raise LLMError("free limit is used up for now", kind="rate", retry_after_s=7)
            return LLMResponse(text="", model="groq", function_calls=[{"name": "list_books", "args": {}}])

    report = run_tool_eval(Busy(), DECLARATIONS, [case("list_books")], "", sleep=waits.append)
    assert report.passed == 1 and waits == [7, 7] and len(calls) == 3


def test_a_brain_out_for_the_day_is_not_waited_for():
    waits = []

    class OutForTheDay:
        def generate(self, request):
            raise LLMError("daily quota", kind="rate", quota_id="GenerateRequestsPerDay")

    report = run_tool_eval(OutForTheDay(), DECLARATIONS, [case("list_books")], "", sleep=waits.append)
    assert report.results[0].reason == "brain error" and waits == []
