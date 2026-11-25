"""Jarvis's tool calls: reads run side by side with a time limit, changes run one at a time in order."""
import threading
import time

from jarvis_books.assistant import chat
from jarvis_books.assistant.chat import SEARCH_LIMIT_NOTE, Assistant


class RecordingTools:
    def __init__(self, delays=None):
        self.delays = delays or {}
        self.log = []
        self.active = 0
        self.most_at_once = 0
        self.lock = threading.Lock()

    def call(self, name, args, source="ai"):
        with self.lock:
            self.active += 1
            self.most_at_once = max(self.most_at_once, self.active)
            self.log.append(("start", name))
        time.sleep(self.delays.get(name, 0.2))
        with self.lock:
            self.active -= 1
            self.log.append(("end", name))
        return {"tool": name, "args": args}


def calls(*names):
    return [{"name": n, "args": {"n": i}} for i, n in enumerate(names)]


def test_independent_reads_run_side_by_side_and_keep_their_order():
    tools = RecordingTools()
    started = time.monotonic()
    out = Assistant(None, None, tools)._run_calls(calls("list_books", "web_search", "system_info"), [True] * 3)
    took = time.monotonic() - started
    assert [r["tool"] for r in out] == ["list_books", "web_search", "system_info"]
    assert [r["args"]["n"] for r in out] == [0, 1, 2]
    assert tools.most_at_once == 3 and took < 0.5


def test_changes_run_one_at_a_time_in_the_order_asked():
    tools = RecordingTools()
    Assistant(None, None, tools)._run_calls(calls("create_book", "write_file", "game_bid"), [True] * 3)
    assert tools.most_at_once == 1
    assert [n for kind, n in tools.log if kind == "start"] == ["create_book", "write_file", "game_bid"]


def test_a_slow_read_is_reported_instead_of_holding_up_the_answer(monkeypatch):
    monkeypatch.setattr(chat, "READ_TIMEOUT_S", 0.3)
    tools = RecordingTools({"read_file": 2.0, "list_books": 0.05})
    started = time.monotonic()
    out = Assistant(None, None, tools)._run_calls(calls("read_file", "list_books"), [True, True])
    assert time.monotonic() - started < 1.5
    assert "took longer than 0.3 seconds" in out[0]["error"]
    assert out[1]["tool"] == "list_books"


def test_slow_research_tools_get_more_time_and_changes_have_no_limit(monkeypatch):
    monkeypatch.setattr(chat, "READ_TIMEOUT_S", 0.1)
    monkeypatch.setattr(chat, "SLOW_READS", {"research_web": 2.0})
    tools = RecordingTools({"research_web": 0.4, "game_advance": 0.4})
    out = Assistant(None, None, tools)._run_calls(calls("research_web", "game_advance"), [True, True])
    assert out[0]["tool"] == "research_web" and out[1]["tool"] == "game_advance"


def test_searches_over_the_limit_are_refused_without_running():
    tools = RecordingTools()
    out = Assistant(None, None, tools)._run_calls(calls("web_search", "web_search"), [True, False])
    assert out[1] == {"error": SEARCH_LIMIT_NOTE}
    assert [n for kind, n in tools.log if kind == "start"] == ["web_search"]
