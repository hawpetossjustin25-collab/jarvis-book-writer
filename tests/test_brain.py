"""Gemini + LFM3: the local engine, the automatic switch, and switching by asking Jarvis."""
import json

import pytest
from conftest import hardware
from fakes import FakeGemini, FakeOllama

from jarvis_books.llm.brain import GEMINI, LOCAL, Brain
from jarvis_books.llm.errors import InvalidStructuredOutput, LLMError
from jarvis_books.llm.local import LocalEngine, to_json_schema, to_messages
from jarvis_books.llm.models import LLMRequest
from jarvis_books.studio import Studio


def local(tmp_path, client=None, **kw):
    return LocalEngine(client or FakeOllama(), state_path=tmp_path / "local.json", hardware_probe=hardware, **kw)


# ---- the local engine ----------------------------------------------------------------------
def test_finds_lfm3_and_reads_its_real_limits(tmp_path):
    eng = local(tmp_path)
    assert eng.initialize()
    assert eng.profile.context_window == 262144 and eng.profile.supports_tools
    # 6 attention layers x 8 kv heads x 64 dims x K+V x f16 bytes
    assert eng.profile.kv_bytes_per_token == 6 * 8 * 64 * 2 * 2
    # A slow CPU starts with a small context so the first answer comes back in time.
    assert eng.num_ctx == LocalEngine.MIN_CTX
    cfg = eng.get_runtime_configuration()
    assert cfg.routes["chat"] == (eng.model,) and "tts" not in cfg.routes and "live" not in cfg.routes
    assert cfg.concurrency == 1


def test_context_grows_with_measured_speed_and_is_remembered(tmp_path):
    client = FakeOllama()
    eng = local(tmp_path, client)
    eng.initialize()
    for _ in range(4):  # this computer turns out to read 400 tokens/s and write 20
        client.replies.append({"message": {"content": "hi"}, "prompt_eval_count": 4000,
                               "prompt_eval_duration": int(10e9), "eval_count": 200, "eval_duration": int(10e9)})
        eng.generate(LLMRequest(role="chat", system="s", instruction="hello"))
    assert eng.speed.prompt_tps > 300 and eng.num_ctx > LocalEngine.MIN_CTX
    assert eng.output_tokens() > 1000
    again = local(tmp_path, FakeOllama())
    again.initialize()
    assert again.num_ctx == eng.num_ctx  # measurements survive a restart


def test_context_never_exceeds_free_ram(tmp_path):
    from dataclasses import replace
    tight = replace(hardware(), available_ram_bytes=3 * 1024 ** 3)
    eng = LocalEngine(FakeOllama(), hardware_probe=lambda: tight)
    eng.speed.prompt_tps = 100_000
    eng.initialize()
    room = 3 * 1024 ** 3 - 1_100_000_000 - LocalEngine.HEADROOM
    assert eng.num_ctx <= max(LocalEngine.MIN_CTX, room // eng.profile.kv_bytes_per_token)


def test_not_downloaded_or_not_running_is_reported_plainly(tmp_path):
    missing = local(tmp_path, FakeOllama(installed=False))
    assert not missing.initialize() and "downloaded" in missing.problem
    with pytest.raises(LLMError) as err:
        missing.generate(LLMRequest(role="chat", instruction="hi"))
    assert err.value.kind == "unavailable"
    off = local(tmp_path, FakeOllama(online=False))
    assert not off.initialize() and "isn't running" in off.problem


def test_download_then_ready(tmp_path):
    client = FakeOllama(installed=False)
    eng = local(tmp_path, client)
    seen = []
    assert eng.download(lambda f, m: seen.append(f))
    assert client.pulls == [eng.model] and seen[-1] == 1.0 and eng.available


def test_chat_turns_tools_and_results_reach_ollama_in_its_format(tmp_path):
    client = FakeOllama()
    eng = local(tmp_path, client)
    history = [{"role": "user", "parts": [{"text": "open my book"}]},
               {"role": "model", "parts": [{"functionCall": {"name": "open_book", "args": {"book": "Rings"}}}]},
               {"role": "user", "parts": [{"functionResponse": {"name": "open_book", "response": {"title": "Rings"}}}]}]
    tools = [{"name": "open_book", "description": "Open a book",
              "parameters": {"type": "OBJECT", "properties": {"book": {"type": "STRING"}}, "required": ["book"]}}]
    client.reply("<think>hmm</think>It's open.", calls=[{"name": "read_chapter", "arguments": {"chapter": 1}}])
    resp = eng.generate(LLMRequest(role="chat", system="You are Jarvis", history=history, tools=tools))
    sent = client.chats[-1]
    assert [m["role"] for m in sent["messages"]] == ["system", "user", "assistant", "tool"]
    assert sent["messages"][2]["tool_calls"][0]["function"] == {"name": "open_book", "arguments": {"book": "Rings"}}
    assert sent["tools"][0]["function"]["parameters"] == {
        "type": "object", "properties": {"book": {"type": "string"}}, "required": ["book"]}
    assert sent["options"]["num_ctx"] == eng.num_ctx
    assert resp.text == "It's open." and resp.function_calls == [{"name": "read_chapter", "args": {"chapter": 1}}]
    assert resp.content["parts"][-1] == {"functionCall": {"name": "read_chapter", "args": {"chapter": 1}}}
    assert resp.model.startswith("LFM3")


def test_tools_are_left_out_when_the_model_cannot_use_them(tmp_path):
    client = FakeOllama()
    eng = local(tmp_path, client)
    eng.initialize()
    eng.profile.supports_tools = False
    eng.generate(LLMRequest(role="chat", instruction="hi", tools=[{"name": "x", "parameters": {"type": "OBJECT"}}]))
    assert "tools" not in client.chats[-1]


def test_structured_answers_are_checked_and_retried_once(tmp_path):
    client = FakeOllama()
    eng = local(tmp_path, client)
    schema = {"type": "OBJECT", "properties": {"title": {"type": "STRING"}}, "required": ["title"]}
    client.reply("not json")
    client.reply(json.dumps({"title": "Rings"}))
    resp = eng.generate(LLMRequest(role="planner", instruction="name it", json_schema=schema))
    assert resp.data == {"title": "Rings"} and resp.attempts == 2
    assert client.chats[0]["format"] == to_json_schema(schema)
    client.reply("nope")
    client.reply("still nope")
    with pytest.raises(InvalidStructuredOutput):
        eng.generate(LLMRequest(role="planner", instruction="name it", json_schema=schema))


def test_long_history_is_trimmed_to_fit(tmp_path):
    client = FakeOllama()
    eng = local(tmp_path, client)
    eng.initialize()
    big = "word " * 3000
    history = [{"role": r, "parts": [{"text": big}]} for r in ("user", "model") * 4]
    eng.generate(LLMRequest(role="chat", system="s", history=history, instruction="latest question"))
    sent = client.chats[-1]["messages"]
    assert sent[-1]["content"].endswith("latest question") or "latest question" in sent[-1]["content"]
    assert sum(len(m["content"]) for m in sent) / 4 < eng.num_ctx
    assert sent[1]["role"] == "user"



def test_a_long_research_run_is_shortened_but_never_loses_the_question(tmp_path):
    client = FakeOllama()
    eng = local(tmp_path, client)
    eng.initialize()
    page = {"sources": [{"url": f"https://site{i}.example", "text": "fact " * 800} for i in range(4)]}
    history = [{"role": "user", "parts": [{"text": "earlier chat " * 400}]}, {"role": "model", "parts": [{"text": "ok"}]},
               {"role": "user", "parts": [{"text": "research lighthouses"}]}]
    for _ in range(6):
        history += [{"role": "model", "parts": [{"functionCall": {"name": "research_web", "args": {"question": "q"}}}]},
                    {"role": "user", "parts": [{"functionResponse": {"name": "research_web", "response": page}}]}]
    eng.generate(LLMRequest(role="chat", system="s", history=history))
    sent = client.chats[-1]["messages"]
    assert sent[1] == {"role": "user", "content": "research lighthouses"}
    assert sum(m["role"] == "tool" for m in sent) == 6
    assert sum(len(m["content"]) for m in sent) / 4 < eng.input_tokens()

def test_message_conversion_keeps_text_and_tool_results():
    msgs = to_messages("", [{"role": "user", "parts": [{"functionResponse": {"name": "a", "response": {"x": 1}}},
                                                        {"text": "and?"}]}])
    assert msgs == [{"role": "tool", "tool_name": "a", "content": '{"x": 1}'}, {"role": "user", "content": "and?"}]


# ---- the switch --------------------------------------------------------------------------------
class Stub:
    def __init__(self, name):
        self.name, self.fail, self.calls, self.available, self._config = name, None, 0, True, object()

    def generate(self, request):
        self.calls += 1
        if self.fail:
            raise LLMError(f"{self.name} down", kind=self.fail)
        from jarvis_books.llm.models import LLMResponse
        return LLMResponse(text=self.name, model=self.name)

    def get_runtime_configuration(self):
        if self.fail:
            raise LLMError("down", kind=self.fail)
        return self.name

    def status(self):
        return {"ready": self.available}

    def embed(self, texts):
        return [self.name]


def brain(settings=None, cooldown_s=120):
    clock = [0.0]
    store = {"brain": "gemini", "brain_auto": "1", **(settings or {})}
    g, l = Stub("gemini"), Stub("lfm3")
    b = Brain(g, l, lambda: store, clock=lambda: clock[0], cooldown_s=cooldown_s)
    return b, g, l, store, clock


def ask(b):
    return b.generate(LLMRequest(role="chat", instruction="hi")).text


def test_gemini_answers_first_by_default():
    b, g, l, _, _ = brain()
    assert ask(b) == "gemini" and l.calls == 0 and b.status()["answering_now"] == "Gemini"


@pytest.mark.parametrize("kind", ["quota", "rate", "overload", "network", "unavailable", "unknown", "safety"])
def test_switches_to_lfm3_when_gemini_fails_and_back_when_it_works(kind):
    b, g, l, _, clock = brain()
    switches = []
    b.subscribe(lambda new, old, why: switches.append((new, old, why)))
    ask(b)
    g.fail = kind
    assert ask(b) == "lfm3"
    assert switches[-1][:2] == (LOCAL, GEMINI) and "gemini down" in switches[-1][2]
    calls = g.calls
    assert ask(b) == "lfm3" and g.calls == calls  # Gemini rests instead of failing every message
    assert b.status()["gemini"].startswith("resting")
    g.fail = None
    clock[0] += 121
    assert ask(b) == "gemini" and switches[-1][:2] == (GEMINI, LOCAL)


def test_a_bad_request_does_not_switch():
    b, g, l, _, _ = brain()
    g.fail = "invalid"
    with pytest.raises(LLMError):
        ask(b)
    assert l.calls == 0


def test_choosing_lfm3_first_and_falling_back_to_gemini():
    b, g, l, store, _ = brain({"brain": "local"})
    assert ask(b) == "lfm3"
    l.fail = "unavailable"
    assert ask(b) == "gemini"


def test_automatic_switch_off_never_switches():
    b, g, l, _, _ = brain({"brain_auto": "0"})
    g.fail = "quota"
    with pytest.raises(LLMError) as err:
        ask(b)
    assert l.calls == 0 and "gemini down" in str(err.value)


def test_both_down_says_why_and_a_resting_brain_is_still_the_last_resort():
    b, g, l, _, _ = brain()
    g.fail = l.fail = "unavailable"
    with pytest.raises(LLMError) as err:
        ask(b)
    assert "Gemini" in str(err.value) and "LFM3" in str(err.value)
    l.fail = None
    assert ask(b) == "lfm3"  # both resting: still tried rather than refusing


def test_gemini_only_features_go_straight_to_gemini():
    b, g, l, _, _ = brain()
    assert b.embed(["x"]) == ["gemini"]
    nokey = Brain(None, l, lambda: {})
    with pytest.raises(LLMError):
        nokey.embed(["x"])
    assert ask(nokey) == "lfm3" and nokey.ready()


def test_runtime_configuration_follows_the_working_brain():
    b, g, l, _, _ = brain()
    assert b.get_runtime_configuration() == "gemini"
    g.fail = "quota"
    assert b.get_runtime_configuration() == "lfm3"


# ---- the whole app -----------------------------------------------------------------------------------
def studio(tmp_path, client=None, ollama=None):
    s = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=client or FakeGemini(),
               engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None},
               local_client=ollama or FakeOllama())
    s.start(probe=False)
    return s


def test_chat_keeps_working_on_lfm3_when_gemini_runs_out(tmp_path):
    gemini = FakeGemini()
    s = studio(tmp_path, gemini)
    try:
        assert s.local.available
        gemini.errors = [LLMError("quota", kind="quota") for _ in range(20)]
        reply = s.assistant.send("hello jarvis")
        assert reply == "ok from LFM3" and s.brain.current == LOCAL
    finally:
        s.close()


def test_switching_by_asking_jarvis_is_saved_and_undoable(tmp_path):
    s = studio(tmp_path)
    try:
        out = s.tools.call("switch_brain", {"use": "lfm3"})
        assert out["first_choice"] == "LFM3" and s.settings()["brain"] == "local"
        assert s.assistant.send("hi") == "ok from LFM3"
        s.tools.call("switch_brain", {"automatic": "off"})
        assert not s.brain.automatic()
        s.tools.run("undo_last_change", {})
        assert s.brain.automatic()
        status = s.tools.call("brain_status", {})
        assert status["first_choice"] == "LFM3" and status["lfm3"]["ready"]
    finally:
        s.close()
    again = studio(tmp_path)
    try:
        assert again.brain.primary() == LOCAL  # survives a restart
    finally:
        again.close()


def test_switch_tools_need_no_yes_and_are_offered_when_asked(tmp_path):
    s = studio(tmp_path)
    try:
        assert s.gate.decide("switch_brain") == "free" and s.gate.decide("brain_status") == "free"
        names = {d["name"] for d in s.tools.select("switch to lfm3 please")}
        assert {"switch_brain", "brain_status"} <= names
    finally:
        s.close()


def test_choosing_lfm3_before_it_is_downloaded_warns_and_gemini_answers(tmp_path):
    s = studio(tmp_path, ollama=FakeOllama(online=False))
    try:
        out = s.tools.call("switch_brain", {"use": "lfm3"})
        assert "warning" in out and "Gemini answers" in out["warning"]
        assert s.assistant.send("hi") != "ok from LFM3"
        assert s.brain.current == GEMINI
    finally:
        s.close()


def test_lfm3_downloads_once_in_the_background_when_ollama_is_there(tmp_path):
    ollama = FakeOllama(installed=False)
    s = studio(tmp_path, ollama=ollama)
    try:
        import time
        for _ in range(100):
            if s.local.available:
                break
            time.sleep(0.05)
        assert ollama.pulls == [s.local.model] and s.local.available
    finally:
        s.close()


def test_without_a_gemini_key_jarvis_still_answers_with_lfm3(tmp_path):
    s = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=None,
               engine_kwargs={"hardware_probe": hardware}, local_client=FakeOllama())
    s.start(probe=False)
    try:
        assert s.engine is None and s.brain.ready()
        assert s.assistant.send("hi") == "ok from LFM3"
    finally:
        s.close()


def test_first_answer_from_the_backup_is_announced():
    b, g, l, _, _ = brain()
    switches = []
    b.subscribe(lambda new, old, why: switches.append((new, old)))
    g.fail = "network"
    assert ask(b) == "lfm3" and switches == [(LOCAL, GEMINI)]


def test_switching_back_to_a_resting_gemini_says_so(tmp_path):
    gemini = FakeGemini()
    s = studio(tmp_path, gemini)
    try:
        gemini.errors = [LLMError("quota", kind="quota") for _ in range(20)]
        s.assistant.send("hello")
        out = s.tools.call("switch_brain", {"use": "gemini"})
        assert "Gemini failed a moment ago" in out["warning"]
    finally:
        s.close()


def test_thinking_is_off_on_a_slow_computer_and_for_chat(tmp_path):
    client = FakeOllama()
    eng = local(tmp_path, client)
    eng.initialize()
    eng.generate(LLMRequest(role="planner", instruction="plan"))
    assert client.chats[-1]["think"] is False  # 6 tokens/s: thinking would take ten times longer
    eng.speed.output_tps = 40
    eng.generate(LLMRequest(role="planner", instruction="plan"))
    assert client.chats[-1]["think"] is True
    eng.generate(LLMRequest(role="chat", instruction="hi"))
    assert client.chats[-1]["think"] is False
