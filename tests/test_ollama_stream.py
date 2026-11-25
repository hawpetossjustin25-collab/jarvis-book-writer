"""Streamed Ollama answers: a slow CPU may take hours on an upgrade, as long as it keeps writing."""
import json
import sys

import pytest
import requests

from jarvis_books.llm import local
from jarvis_books.llm.errors import LLMError
from jarvis_books.llm.local import LocalEngine, OllamaClient, launch_command
from jarvis_books.llm.models import LLMRequest
from tests.fakes import FakeOllama


class Resp:
    status_code = 200

    def __init__(self, lines, fail_after=None):
        self.lines, self.fail_after, self.closed = lines, fail_after, False

    def iter_lines(self):
        for n, line in enumerate(self.lines):
            if self.fail_after is not None and n == self.fail_after:
                raise requests.exceptions.ConnectionError("Read timed out.")
            yield json.dumps(line).encode()

    def close(self):
        self.closed = True


class Session:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def request(self, method, url, json=None, stream=False, timeout=None):
        self.calls.append({"url": url, "json": json, "stream": stream, "timeout": timeout})
        return self.resp


DONE = {"done": True, "done_reason": "stop", "message": {"role": "assistant", "content": ""},
        "prompt_eval_count": 9000, "prompt_eval_duration": int(600e9), "eval_count": 3, "eval_duration": int(1e9)}


def test_streamed_answer_is_joined_with_its_tool_calls_and_timings():
    resp = Resp([{"message": {"content": '{"a"'}}, {"message": {"content": ": 1}"}},
                 {"message": {"tool_calls": [{"function": {"name": "f", "arguments": {}}}]}}, DONE])
    session = Session(resp)
    body = OllamaClient(session=session, launcher=lambda: False).chat({"model": "m"}, timeout=7200, idle_s=900)
    call = session.calls[0]
    assert call["stream"] and call["json"]["stream"] is True and call["timeout"] == (10, 900)
    assert body["message"]["content"] == '{"a": 1}'
    assert body["message"]["tool_calls"] == [{"function": {"name": "f", "arguments": {}}}]
    assert body["prompt_eval_count"] == 9000 and body["done_reason"] == "stop" and resp.closed


def test_a_model_that_goes_quiet_is_reported_as_too_slow_not_crashed():
    resp = Resp([{"message": {"content": "x"}}, DONE], fail_after=1)
    client = OllamaClient(session=Session(resp), launcher=lambda: False)
    with pytest.raises(LLMError, match="took too long.*nothing new for 15 min") as err:
        client.chat({"model": "m"}, timeout=7200, idle_s=900)
    assert err.value.kind == "network" and resp.closed


def test_an_answer_cut_off_before_done_or_past_the_cap_is_an_error(monkeypatch):
    client = OllamaClient(session=Session(Resp([{"message": {"content": "half"}}])), launcher=lambda: False)
    with pytest.raises(LLMError, match="stopped before finishing"):
        client.chat({"model": "m"}, timeout=60, idle_s=30)
    clock = iter([0.0, 100.0, 200.0])
    monkeypatch.setattr(local.time, "monotonic", lambda: next(clock))
    client = OllamaClient(session=Session(Resp([{"message": {"content": "a"}}, DONE])), launcher=lambda: False)
    with pytest.raises(LLMError, match="took too long"):
        client.chat({"model": "m"}, timeout=50, idle_s=30)


def test_ollama_errors_mid_stream_are_raised():
    client = OllamaClient(session=Session(Resp([{"error": "model runner has unexpectedly stopped"}])),
                          launcher=lambda: False)
    with pytest.raises(LLMError, match="unexpectedly stopped"):
        client.chat({"model": "m"}, timeout=60, idle_s=30)


def test_plain_chat_still_waits_for_one_whole_answer():
    class Plain(Resp):
        def json(self):
            return DONE
    session = Session(Plain([]))
    assert OllamaClient(session=session, launcher=lambda: False).chat({"model": "m"}, timeout=5) == DONE
    assert session.calls[0]["json"]["stream"] is False and session.calls[0]["timeout"] == 5


def test_engine_streams_only_when_capped_and_waits_long_enough_to_read_the_prompt(tmp_path):
    fake = FakeOllama()
    plain = LocalEngine(fake, state_path=tmp_path / "a.json", answer_budget_s=90, prompt_budget_s=60)
    plain.generate(LLMRequest(role="chat", instruction="hi"))
    assert fake.timeouts[-1] == (270, None)
    capped = LocalEngine(fake, state_path=tmp_path / "b.json", answer_budget_s=1800, prompt_budget_s=1800,
                         stream_cap_s=4 * 3600)
    capped.generate(LLMRequest(role="chat", instruction="hi"))
    timeout, idle = fake.timeouts[-1]
    assert timeout == 4 * 3600 and idle >= 1800 + 300  # measured before this answer sped the estimate up
    capped.num_ctx, capped.speed.prompt_tps = 30000, 10.0
    assert capped.quiet_limit_s() == 3 * 30000 / 10 + 300


def test_windows_starts_the_ollama_tray_app_instead_of_a_second_server(tmp_path, monkeypatch):
    exe = tmp_path / "ollama.exe"
    exe.write_text("")
    monkeypatch.setattr(sys, "platform", "win32")
    assert launch_command(str(exe)) == [str(exe), "serve"]
    (tmp_path / "ollama app.exe").write_text("")
    assert launch_command(str(exe)) == [str(tmp_path / "ollama app.exe")]
    monkeypatch.setattr(sys, "platform", "linux")
    assert launch_command(str(exe)) == [str(exe), "serve"]


# ---- speed: streaming, keeping the model loaded and reusing the prompt Ollama already read ----------------------

def _engine(tmp_path, **kw):
    from tests.fakes import FakeOllama
    from jarvis_books.llm.local import LocalEngine
    client = FakeOllama()
    eng = LocalEngine(client, state_path=tmp_path / "s.json", **kw)
    assert eng.initialize()
    return eng, client


def test_the_reply_streams_to_on_text_as_it_is_written(tmp_path):
    from jarvis_books.llm.models import LLMRequest
    eng, client = _engine(tmp_path)
    client.reply("Hello there Justin")
    seen = []
    resp = eng.generate(LLMRequest(role="chat", system="s", instruction="hi", on_text=seen.append))
    assert seen == ["Hello", "Hello there", "Hello there Justin"] and resp.text == "Hello there Justin"
    assert client.timeouts[-1][1] is not None  # streamed, with a quiet limit


def test_keep_alive_is_sent_and_warm_loads_the_model(tmp_path):
    from jarvis_books.llm.models import LLMRequest
    eng, client = _engine(tmp_path, keep_alive=-1)
    assert eng.warm() and client.loaded[-1] == {"name": eng.model, "keep_alive": -1}
    eng.generate(LLMRequest(role="chat", system="s", instruction="hi"))
    assert client.chats[-1]["keep_alive"] == -1


def test_partial_thinking_is_never_shown():
    from jarvis_books.llm.local import _strip_thinking
    assert _strip_thinking("<think>planning", partial=True) == ""
    assert _strip_thinking("<think>x</think>Hi <think>more", partial=True) == "Hi"


def _decl(name):
    return {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}}


def test_the_tool_list_and_system_prompt_stay_the_same_while_they_still_cover_the_request(tmp_path):
    from jarvis_books.llm.models import LLMRequest
    eng, client = _engine(tmp_path)
    big = "rules " * 4000  # longer than its share, so it is cut
    eng.generate(LLMRequest(role="chat", system=big, instruction="a", tools=[_decl("a"), _decl("b"), _decl("c")]))
    first = client.chats[-1]
    eng.generate(LLMRequest(role="chat", system=big, instruction="b", tools=[_decl("b")]))
    second = client.chats[-1]
    assert second["tools"] == first["tools"] and second["messages"][0] == first["messages"][0]
    eng.generate(LLMRequest(role="chat", system=big, instruction="d", tools=[_decl("d")]))
    assert [t["function"]["name"] for t in client.chats[-1]["tools"]] == ["d"]  # a new tool: a new list
    assert client.chats[-1]["messages"][0] == first["messages"][0]


def test_small_speed_changes_do_not_resize_the_context(tmp_path):
    eng, client = _engine(tmp_path)
    before = eng.num_ctx
    eng.speed.prompt_tps *= 1.05
    eng._measure({"prompt_eval_count": 100, "prompt_eval_duration": int(100 / eng.speed.prompt_tps * 1e9)})
    assert eng.num_ctx == before


def test_the_history_window_only_moves_every_few_messages():
    from jarvis_books.llm.history import steady_count
    starts = [total - steady_count(total, 8, 4) for total in range(1, 30)]
    assert all(n <= 11 for n in (steady_count(t, 8, 4) for t in range(1, 30)))
    assert sorted(set(starts)) == [0, 4, 8, 12, 16, 20] and starts == sorted(starts)

