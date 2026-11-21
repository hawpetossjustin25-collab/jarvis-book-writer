"""Groq's free cloud models: the engine, the key, and Groq as the brain's free backup."""
import json

import pytest
import requests
from conftest import hardware
from fakes import FakeGemini, FakeOllama

from jarvis_books.llm.brain import GEMINI, GROQ, LOCAL, Brain
from jarvis_books.llm.errors import InvalidStructuredOutput, LLMError
from jarvis_books.llm.groq import (CHAT_MODEL, MAX_TOOLS, GroqEngine, load_groq_key, save_groq_key,
                                   to_openai_messages)
from jarvis_books.llm.local import LocalEngine
from jarvis_books.llm.models import LLMRequest, LLMResponse
from jarvis_books.studio import Studio


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


class Session:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.posts = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(content="", calls=None):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": f"x{i}", "type": "function", "function": f} for i, f in enumerate(calls)]
    return Resp(200, {"choices": [{"message": msg, "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 50, "completion_tokens": 9}})


def engine(*replies, key="gsk_test"):
    return GroqEngine(lambda: key, session=Session(*replies))


def test_messages_pair_tool_results_with_their_calls():
    msgs = to_openai_messages("sys", [
        {"role": "user", "parts": [{"text": "battery?"}]},
        {"role": "model", "parts": [{"functionCall": {"name": "battery", "args": {}}},
                                    {"functionCall": {"name": "weather", "args": {"city": "Atlanta"}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "weather", "response": {"f": 60}}},
                                   {"functionResponse": {"name": "battery", "response": {"pct": 80}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "orphan", "response": {"x": 1}}}]},
    ])
    assert msgs[0] == {"role": "system", "content": "sys"}
    calls = msgs[2]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["battery", "weather"]
    assert json.loads(calls[1]["function"]["arguments"]) == {"city": "Atlanta"}
    assert msgs[3] == {"role": "tool", "tool_call_id": calls[1]["id"], "content": '{"f": 60}'}
    assert msgs[4]["tool_call_id"] == calls[0]["id"]
    assert msgs[5] == {"role": "user", "content": 'Result of orphan: {"x": 1}'}


def test_unanswered_calls_get_a_placeholder_so_the_request_stays_valid():
    msgs = to_openai_messages("", [
        {"role": "model", "parts": [{"functionCall": {"name": "battery", "args": {}}}]},
        {"role": "user", "parts": [{"text": "never mind"}]}])
    assert msgs[1]["role"] == "tool" and msgs[1]["tool_call_id"] == msgs[0]["tool_calls"][0]["id"]
    assert msgs[2] == {"role": "user", "content": "never mind"}


def test_text_and_tool_calls_come_back_like_gemini():
    eng = engine(ok("", [{"name": "battery", "arguments": "{}"}, {"name": "bad", "arguments": "{nope"},
                    {"name": "odd", "arguments": '{"": {}, "city": "Atlanta"}'}]))
    resp = eng.generate(LLMRequest(role="chat", system="s", instruction="battery?",
                                   tools=[{"name": "battery", "description": "b"}],
                                   local_tools=[{"name": "battery", "description": "b"}]))
    assert resp.function_calls == [{"name": "battery", "args": {}}, {"name": "bad", "args": {}},
                                   {"name": "odd", "args": {"city": "Atlanta"}}]
    assert resp.content["parts"][0] == {"functionCall": {"name": "battery", "args": {}}}
    assert resp.model == f"Groq ({CHAT_MODEL})" and resp.prompt_tokens == 50
    sent = eng.session.posts[0]
    assert sent["headers"]["Authorization"] == "Bearer gsk_test"
    assert sent["json"]["tools"][0]["function"]["name"] == "battery"
    assert sent["json"]["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}
    assert sent["url"].endswith("/chat/completions")


def test_requests_fit_the_free_plan():
    tools = [{"name": f"t{i}", "description": "d" * 300, "parameters": {"type": "OBJECT", "properties": {}}}
             for i in range(200)]
    eng = engine()
    body = eng.build(LLMRequest(role="chat", system="x " * 40000, instruction="hi", tools=tools))
    assert len(body["tools"]) < MAX_TOOLS
    assert len(json.dumps(body)) < eng.input_tokens * 4 + 4000
    assert body["max_completion_tokens"] <= eng.output_tokens
    short = eng.build(LLMRequest(role="chat", instruction="hi", tools=tools, local_tools=tools[:3]))
    assert [t["function"]["name"] for t in short["tools"]] == ["t0", "t1", "t2"]


def test_json_answers_are_checked_and_retried_once():
    schema = {"type": "OBJECT", "properties": {"question": {"type": "STRING"}}, "required": ["question"]}
    eng = engine(ok("not json"), ok('{"question": "solar"}'))
    resp = eng.generate(LLMRequest(role="summarizer", instruction="ask", json_schema=schema))
    assert resp.data == {"question": "solar"} and resp.attempts == 2
    first = eng.session.posts[0]["json"]
    assert first["response_format"] == {"type": "json_object"} and "JSON schema" in first["messages"][0]["content"]
    with pytest.raises(InvalidStructuredOutput):
        engine(ok("{}"), ok("{}")).generate(LLMRequest(role="summarizer", instruction="ask", json_schema=schema))


@pytest.mark.parametrize("status,kind", [(401, "unavailable"), (429, "rate"), (413, "too_big"), (503, "overload"),
                                         (400, "bad_request")])
def test_refusals_are_classified_so_the_brain_moves_on(status, kind):
    eng = engine(Resp(status, {"error": {"message": "nope"}}, {"retry-after": "7"}))
    with pytest.raises(LLMError) as err:
        eng.generate(LLMRequest(role="chat", instruction="hi"))
    assert err.value.kind == kind
    if status == 429:
        assert err.value.retry_after_s == 7.0


def test_network_failure_and_missing_key():
    with pytest.raises(LLMError) as err:
        engine(requests.ConnectionError("down")).generate(LLMRequest(role="chat", instruction="hi"))
    assert err.value.kind == "network"
    eng = engine(key=None)
    assert not eng.available and not eng.initialize() and "no Groq key" in eng.problem
    with pytest.raises(LLMError):
        eng.generate(LLMRequest(role="chat", instruction="hi"))


def test_key_is_saved_sealed_and_read_back(tmp_path, monkeypatch):
    for var in ("JARVIS_GROQ_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert load_groq_key(tmp_path) is None
    path = save_groq_key("  gsk_secret  ", tmp_path)
    assert b"gsk_secret" not in path.read_bytes()  # base64 at least; DPAPI-encrypted on Windows
    assert load_groq_key(tmp_path) == "gsk_secret"
    with pytest.raises(ValueError):
        save_groq_key(" ", tmp_path)


# ---- Groq in the brain -----------------------------------------------------------------------
class Stub:
    def __init__(self, name, fail=None, available=True):
        self.name, self.fail, self.available, self.calls = name, fail, available, []

    def status(self):
        return {"ready": self.available}

    def generate(self, req):
        self.calls.append(req)
        if self.fail:
            raise LLMError(self.fail, kind="rate")
        return LLMResponse(text=self.name, model=self.name)


def brain(settings, gemini, groq, local):
    return Brain(gemini, local, lambda: settings, groq=groq, clock=lambda: 0.0)


def test_groq_stands_in_when_gemini_is_out_of_quota():
    g, q, l = Stub("gemini", fail="quota"), Stub("groq"), Stub("local")
    b = brain({"brain": "gemini", "brain_auto": "1"}, g, q, l)
    assert b.order() == [GEMINI, GROQ, LOCAL]
    req = LLMRequest(role="chat", instruction="hi", tools=[{"name": "a"}, {"name": "b"}], local_tools=[{"name": "a"}])
    assert b.generate(req).text == "groq" and not l.calls
    assert q.calls[0].tools == [{"name": "a"}]  # the short list
    assert b.status()["groq"] == "ready" and b.status()["gemini"].startswith("resting")


def test_groq_is_skipped_without_a_key_and_can_be_first():
    g, q, l = Stub("gemini"), Stub("groq", available=False), Stub("local")
    assert brain({"brain": "gemini"}, g, q, l).order() == [GEMINI, LOCAL]
    assert brain({"brain": "local"}, g, None, l).order() == [LOCAL, GEMINI]
    q.available = True
    assert brain({"brain": "groq"}, g, q, l).order() == [GROQ, GEMINI, LOCAL]
    assert brain({"brain": "groq", "brain_auto": "0"}, g, q, l).order() == [GROQ]
    assert brain({"brain": "groq", "brain_auto": "0"}, None, q, l).ready()


def test_studio_switches_to_groq_by_asking_jarvis(tmp_path, monkeypatch):
    for var in ("JARVIS_GROQ_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    s = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(), local_client=FakeOllama(),
               engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None})
    s.start(probe=False)
    try:
        assert s.brain.groq is s.groq and not s.groq.available
        out = s.tools.run("switch_brain", {"use": "groq"})
        assert s.settings()["brain"] == "groq" and "no Groq key" in out["warning"]
        s.set_groq_key("gsk_x")
        assert s.groq.available and s.researcher.cloud.available and s.brain.status()["groq"] == "ready"
        assert s.researcher.cloud.model != s.groq.model  # research has its own daily allowance
        assert s.tools.run("change_setting", {"key": "research_brain", "value": "groq"})["value"] == "groq"
        assert "Groq" in s.researcher.status()["thinks_with"]
    finally:
        s.close()


def test_gemini_accepts_tool_calls_another_model_made_earlier():
    from jarvis_books.llm.gemini import OTHER_MODEL_SIGNATURE, build_payload
    history = [{"role": "user", "parts": [{"text": "battery?"}]},
               {"role": "model", "parts": [{"text": "checking"}, {"functionCall": {"name": "battery", "args": {}}}]},
               {"role": "model", "parts": [{"functionCall": {"name": "x", "args": {}}, "thoughtSignature": "real"}]}]
    sent = build_payload("", history, None, [], None, None)["contents"]
    assert sent[1]["parts"][1]["thoughtSignature"] == OTHER_MODEL_SIGNATURE and "thoughtSignature" not in sent[1]["parts"][0]
    assert sent[2]["parts"][0]["thoughtSignature"] == "real" and sent[0] is history[0]
    assert "thoughtSignature" not in history[1]["parts"][1]  # the stored history is left as it was
