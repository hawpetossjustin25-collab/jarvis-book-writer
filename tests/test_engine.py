import json

import pytest

from fakes import FakeGemini, body
from jarvis_books.llm.errors import LLMError, NoModelAvailable
from jarvis_books.llm.models import LLMRequest, PromptSection
from jarvis_books.llm.prompt import fit_sections
from jarvis_books.llm.ratelimit import next_reset, quota_day

SCHEMA = {"type": "OBJECT", "properties": {"title": {"type": "STRING"}}, "required": ["title"]}


def test_discovery_routes_and_capabilities(engine):
    cfg = engine.get_runtime_configuration()
    assert cfg.routes["writer"][0] == "gemini-9.0-flash-lite"
    assert cfg.routes["planner"][0] == "gemini-9.0-flash"
    assert all("image" not in m for r in ("writer", "planner", "chat") for m in cfg.routes[r])
    assert cfg.routes["embed"] == ("gemini-embedding-9",)
    assert cfg.routes["tts"] == ("gemini-9.0-flash-tts",)
    assert engine.live_model() == "gemini-9.0-live"
    p = engine.profile("gemini-9.0-flash-lite")
    assert p.supports_json_mode is True and p.supports_tools is True and p.context_window == 1_048_576
    b = cfg.budgets["gemini-9.0-flash-lite"]
    assert 0 < b.input_budget_tokens < 1_048_576 and b.output_budget_tokens > 1000


def test_probe_results_are_cached(make_engine, fake, tmp_path):
    make_engine(fake)
    first = len(fake.calls)
    make_engine(fake)
    assert len(fake.calls) == first  # second start reuses verified capability results


def test_transient_probe_failure_is_not_cached_as_unsupported(make_engine):
    fake = FakeGemini()
    fake.errors = [LLMError("timeout", kind="network")] * 2
    eng = make_engine(fake)
    profiles = [eng.profile(m) for m in ("gemini-9.0-flash", "gemini-9.0-flash-lite")]
    assert any(p.supports_json_mode is None for p in profiles)
    assert eng.get_runtime_configuration().routes["planner"]


def test_unavailable_discovery_without_cache_raises(make_engine):
    class Broken(FakeGemini):
        def list_models(self):
            raise LLMError("auth", kind="auth")
    with pytest.raises(LLMError):
        make_engine(Broken())


def test_generate_structured_output_and_metrics(engine, fake):
    fake.responder = lambda m, p: body(json.dumps({"title": "Ash"}))
    r = engine.generate(LLMRequest(role="planner", instruction="title", json_schema=SCHEMA))
    assert r.data == {"title": "Ash"} and r.model == "gemini-9.0-flash"
    rows = engine.store.recent_metrics(0, role="planner")
    assert rows and rows[0]["json_valid"] == 1


def test_invalid_json_is_repaired_with_bounded_retries(engine, fake):
    answers = iter([body("not json"), body(json.dumps({"title": "Fixed"}))])
    fake.responder = lambda m, p: next(answers)
    r = engine.generate(LLMRequest(role="planner", instruction="title", json_schema=SCHEMA))
    assert r.data == {"title": "Fixed"} and r.attempts == 2
    assert "did not match" in fake.calls[-1][1]["contents"][-1]["parts"][0]["text"]


def test_quota_error_falls_back_and_blocks_model(engine, fake):
    fake.errors = [LLMError("quota", kind="quota", quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                            quota_value=20)]
    r = engine.generate(LLMRequest(role="writer", instruction="write"))
    assert r.model == "gemini-8.0-flash-lite"
    assert engine.limiter.blocked_reason("gemini-9.0-flash-lite")
    assert engine.limiter.limits("gemini-9.0-flash-lite")[1] == 20  # learned from the provider


def test_no_fallback_when_disabled(engine, fake):
    fake.errors = [LLMError("quota", kind="quota")]
    with pytest.raises(NoModelAvailable):
        engine.generate(LLMRequest(role="writer", instruction="w", allow_fallback=False))


def test_overload_retries_with_backoff_when_no_fallback(engine, fake):
    fake.errors = [LLMError("busy", kind="overload")]
    r = engine.generate(LLMRequest(role="writer", instruction="write", allow_fallback=False))
    assert r.attempts == 2 and r.model == "gemini-9.0-flash-lite"


def test_overloaded_model_cools_down_and_route_moves_on(engine, fake):
    route = engine.get_runtime_configuration().routes["writer"]
    fake.errors = [LLMError("busy", kind="overload")]
    r = engine.generate(LLMRequest(role="writer", instruction="write"))
    assert r.model == route[1] and r.attempts == 1  # no wasted retries on the overloaded model
    calls = len(fake.calls)
    r2 = engine.generate(LLMRequest(role="writer", instruction="write again"))
    assert r2.model == route[1]  # still cooling down, so it is skipped without a request
    assert fake.calls[calls][0] == route[1]


def test_auth_error_is_not_retried(engine, fake):
    fake.errors = [LLMError("bad key", kind="auth")]
    with pytest.raises(LLMError) as e:
        engine.generate(LLMRequest(role="writer", instruction="write"))
    assert e.value.kind == "auth"


def test_prompt_fitting_keeps_required_and_trims_low_priority():
    sections = [PromptSection("Book", "core " * 50, 100, True), PromptSection("Passages", "old " * 4000, 10),
                PromptSection("Recent", "recent " * 3000, 80, trim_from="start")]
    fit = fit_sections("Write chapter 3.", sections, 2000)
    assert fit.tokens <= 2100
    assert "core" in fit.text and "Write chapter 3." in fit.text
    assert "Passages" in fit.dropped or "Passages" in fit.trimmed


def test_history_never_starts_with_orphan_tool_reply(engine):
    hist = [{"role": "user", "parts": [{"functionResponse": {"name": "x", "response": {}}}]},
            {"role": "model", "parts": [{"text": "ok"}]}, {"role": "user", "parts": [{"text": "hi"}]}]
    payload, _, _ = engine.optimize_request(LLMRequest(role="chat", history=hist), "gemini-9.0-flash-lite")
    assert payload["contents"][0]["parts"][0] == {"text": "hi"}


def test_adaptation_reduces_words_on_truncation_and_rolls_back(engine, fake):
    cfg = engine.get_runtime_configuration()
    fake.responder = lambda m, p: body("cut off text", finish="MAX_TOKENS")
    for _ in range(engine.policy.min_observations):
        engine.generate(LLMRequest(role="writer", instruction="write"))
    new = engine.get_runtime_configuration()
    assert new.words_per_call < cfg.words_per_call and new.version > cfg.version
    events = [e["kind"] for e in engine.store.events(20)]
    assert "adaptation_applied" in events
    back = engine.rollback(cfg.version, reason="test")
    assert back.words_per_call == cfg.words_per_call
    assert engine.store.get_config(cfg.version) is not None  # earlier versions are preserved


def test_adaptation_disabled_makes_no_changes(engine, fake):
    engine.set_adaptive(False)
    v = engine.get_runtime_configuration().version
    fake.responder = lambda m, p: body("cut", finish="MAX_TOKENS")
    for _ in range(10):
        engine.generate(LLMRequest(role="writer", instruction="write"))
    assert engine.get_runtime_configuration().version == v


def test_embed_and_tts_use_routed_models(engine, fake):
    model, vecs = engine.embed(["the vault", "a dragon"])
    assert model == "gemini-embedding-9" and len(vecs) == 2
    assert engine.tts("Hello", "Kore")[:4] == b"RIFF"


def test_quota_day_resets_at_pacific_midnight():
    t = 1_800_000_000.0
    assert next_reset(t) > t and quota_day(next_reset(t)) != quota_day(t)


def test_status_has_no_secrets(engine):
    text = json.dumps(engine.status())
    assert "key" not in text.lower() or "api" not in text.lower()
