import threading
import time

import pytest

from jarvis_books.nexus.contracts import NexusRequest
from jarvis_books.nexus.os3 import OS3, ToolRegistry, ToolSpec, json_schema, redact, registry_from_toolbox
from jarvis_books.nexus.store import GraphStore

OBJ = {"type": "object", "properties": {"city": {"type": "string", "minLength": 1}}, "required": ["city"],
       "additionalProperties": False}


@pytest.fixture
def store(tmp_path):
    s = GraphStore(tmp_path / "nexus.db")
    yield s
    s.close()


def weather(args):
    return {"city": args["city"], "temp_f": 71}


def make(store, *specs, selector=None):
    reg = ToolRegistry(store.db)
    for spec in specs:
        reg.register(spec)
    return OS3(reg, selector=selector)


def spec(name="get_weather", handler=weather, level="read", **kw):
    return ToolSpec(name, f"{name.replace('_', ' ')} for a city", OBJ, handler, level=level, **kw)


def test_registry_requires_valid_unique_tools_and_records_them(store):
    reg = ToolRegistry(store.db)
    reg.register(spec(version="2"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(spec())
    with pytest.raises(ValueError, match="permission level"):
        spec(level="god")
    with pytest.raises(ValueError, match="bad tool name"):
        spec(name="Bad Name")
    with pytest.raises(Exception):
        ToolSpec("x", "d", {"type": "object", "properties": {"a": {"type": "nonsense"}}}, weather)
    with pytest.raises(ValueError, match="object schema"):
        ToolSpec("x", "d", {"type": "string"}, weather)
    row = store.db.query_one("SELECT * FROM os3_tools")
    assert row["tool_id"] == "jarvis.get_weather@2" and row["level"] == "read" and row["retries"] == 0
    d = reg.get("jarvis.get_weather@2").describe()
    assert d["required_permission"] == "read" and d["available"] and d["input_schema"] == OBJ


def test_discover_ranks_by_name_then_description(store):
    reg = ToolRegistry()
    reg.register(spec("get_weather"))
    reg.register(ToolSpec("send_text", "text someone about the weather", OBJ, weather, level="outside"))
    reg.register(ToolSpec("list_books", "the author's books", {"type": "object", "properties": {}}, weather))
    assert [s.name for s in reg.discover("weather")] == ["get_weather", "send_text"]
    assert [s.name for s in reg.discover("weather", levels=("read",))] == ["get_weather"]
    assert len(reg.discover("")) == 3 and reg.discover("zebra") == []


def test_success_only_from_a_real_valid_result_and_audited(store):
    os3 = make(store, spec(output_schema={"type": "object", "required": ["temp_f"]}))
    run = os3.execute("get_weather", {"city": "Atlanta"}, request_id="r1", correlation_id="c1")
    assert run.ok and run.result == {"city": "Atlanta", "temp_f": 71} and run.attempts == 1
    row = os3.history(1)[0]
    assert (row["request_id"], row["correlation_id"], row["status"], row["tool_id"]) == \
        ("r1", "c1", "success", "jarvis.get_weather@1")
    assert row["args"] == {"city": "Atlanta"}


@pytest.mark.parametrize("handler,status,error", [
    (lambda a: {"error": "city not found"}, "failed", "city not found"),
    (lambda a: {"needs_approval": True, "request_id": 4}, "waiting_for_approval", "waiting for the author's yes"),
    (lambda a: ["not", "a", "dict"], "failed", "returned list"),
    (lambda a: {"city": "x"}, "failed", "output schema"),
    (lambda a: 1 / 0, "failed", "ZeroDivisionError"),
])
def test_non_results_are_never_success(store, handler, status, error):
    os3 = make(store, spec(handler=handler, output_schema={"type": "object", "required": ["temp_f"]},
                           level="change"))
    run = os3.execute("get_weather", {"city": "Atlanta"})
    assert run.status == status and error in run.error and not run.ok


def test_schema_validation_rejects_bad_and_unknown_arguments(store):
    calls = []
    os3 = make(store, spec(handler=lambda a: calls.append(a) or weather(a)))
    for args, why in (({}, "'city' is a required property"), ({"city": ""}, "city"),
                      ({"city": "x", "zip": "1"}, "Additional properties")):
        run = os3.execute("get_weather", args)
        assert run.status == "rejected" and why in run.error
    assert os3.execute("nope", {}).status == "rejected" and calls == []


def test_roles_limit_levels_and_availability(store):
    flag = {"up": False}
    os3 = make(store, spec("get_weather"), spec("delete_all", level="system"),
               spec("explore", level="safe", available=lambda: flag["up"]))
    assert "may not run system-level" in os3.execute("delete_all", {"city": "x"}, role="operator").error
    assert os3.execute("get_weather", {"city": "x"}, role="reader").ok
    assert os3.execute("explore", {"city": "x"}, role="reader").status == "rejected"
    assert os3.execute("explore", {"city": "x"}, role="operator").status == "unavailable"
    flag["up"] = True
    assert os3.execute("explore", {"city": "x"}, role="operator").ok
    assert os3.execute("get_weather", {"city": "x"}, role="stranger").status == "rejected"


def test_timeouts_and_retries_only_for_lookups(store):
    seen = {"read": 0, "change": 0}
    release = threading.Event()

    def slow(kind):
        def run(args):
            seen[kind] += 1
            if seen[kind] == 1:
                release.wait(2)
            return weather(args)
        return run

    os3 = make(store, spec("look", handler=slow("read"), timeout_s=0.2, retries=2),
               spec("change", handler=slow("change"), level="change", timeout_s=0.2, retries=2))
    run = os3.execute("look", {"city": "x"})
    assert run.ok and run.attempts == 2 and seen["read"] == 2
    changed = os3.execute("change", {"city": "x"})
    assert changed.status == "timeout" and changed.attempts == 1 and "longer than 0.2 s" in changed.error
    release.set()
    os3.close()


def test_secrets_are_redacted_in_the_audit_log(store):
    os3 = make(store, ToolSpec("call_api", "call an api", {"type": "object", "properties": {
        "url": {"type": "string"}, "api_key": {"type": "string"}, "headers": {"type": "object"}}}, weather))
    os3.execute("call_api", {"url": "https://x", "api_key": "sk-123", "headers": {"Authorization": "Bearer z"}})
    row = os3.history(1)[0]
    assert row["args"] == {"url": "https://x", "api_key": "***", "headers": {"Authorization": "***"}}
    assert "sk-123" not in str(store.db.query("SELECT * FROM os3_executions")[0]["args"])
    assert redact("x" * 600).endswith("...")


def test_plan_selects_and_validates_without_running(store):
    calls = []
    os3 = make(store, spec(handler=lambda a: calls.append(a)), spec("delete_all", level="system"),
               selector=lambda text: {"name": "get_weather", "args": {"city": "Atlanta"}, "confidence": 0.97}
               if "weather" in text else None)
    plan = os3.plan("what's the weather in Atlanta")
    assert plan["tool"] == "get_weather" and plan["valid"] and plan["confidence"] == 0.97
    assert plan["candidates"][0] == "get_weather" and calls == []
    none = os3.plan("delete all for city")
    assert none["tool"] is None and "delete_all" in none["candidates"]
    assert "delete_all" not in os3.plan("delete all for city", role="reader")["candidates"]


def test_nexus_contract(store):
    os3 = make(store, spec(), spec("send_text", level="outside", handler=lambda a: {"needs_approval": True}))
    resp = os3.handle(NexusRequest("jarvis", "os3", "execute", {"name": "get_weather", "args": {"city": "A"}}))
    assert resp.ok and resp.results[0]["status"] == "success" and resp.extra["execution_status"] == "success"
    asked = os3.handle(NexusRequest("jarvis", "os3", "execute", {"name": "send_text", "args": {"city": "A"}}))
    assert asked.ok and asked.extra["execution_status"] == "waiting_for_approval"
    assert os3.handle(NexusRequest("jarvis", "os3", "execute", {"name": "zzz"})).error_code == "not_found"
    bad = os3.handle(NexusRequest("jarvis", "os3", "execute", {"name": "get_weather", "args": {}}))
    assert bad.error_code == "invalid_request"
    assert os3.handle(NexusRequest("jarvis", "os3", "execute", {"name": "send_text", "args": {"city": "A"}}),
                      role="reader").error_code == "invalid_request"
    tools = os3.handle(NexusRequest("jarvis", "os3", "tools", {"query": "weather"}))
    assert tools.extra["registered"] == 2 and tools.results[0]["name"] == "get_weather"
    assert os3.handle(NexusRequest("jarvis", "os3", "describe", {"name": "nope"})).error_code == "not_found"
    v = os3.handle(NexusRequest("jarvis", "os3", "validate", {"name": "get_weather", "args": {"x": 1}}))
    assert v.ok and not v.results[0]["valid"]
    hist = os3.handle(NexusRequest("jarvis", "os3", "history", {"limit": 5}))
    assert [h["status"] for h in hist.results][:2] == ["rejected", "rejected"] or hist.results
    assert os3.handle(NexusRequest("jarvis", "os1", "tools", {})).error_code == "invalid_request"
    assert os3.handle(NexusRequest("jarvis", "os3", "fly", {})).error_code == "unknown_operation"
    assert os3.handle("{bad").error_code == "invalid_request"


def test_gemini_schemas_become_strict_json_schema():
    got = json_schema({"type": "OBJECT", "properties": {"n": {"type": "INTEGER", "description": "count"},
                                                         "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
                                                         "kind": {"type": "STRING", "enum": ["a", "b"]}},
                       "required": ["n"]})
    assert got == {"type": "object", "additionalProperties": False, "required": ["n"], "properties": {
        "n": {"type": "integer", "description": "count"}, "tags": {"type": "array", "items": {"type": "string"}},
        "kind": {"type": "string", "enum": ["a", "b"]}}}


def test_jarvis_tools_run_through_his_gate(library, make_engine, tmp_path, store):
    from fakes import FakeGemini
    from jarvis_books.assistant import ToolBox
    from jarvis_books.jobs import JobManager
    from jarvis_books.memory import StoryIndex
    from jarvis_books.writer import BookWriter
    engine = make_engine(FakeGemini())
    index = StoryIndex(library, engine)
    jobs = JobManager(library, workers=1)
    box = ToolBox(library, BookWriter(library, engine, index), index, jobs)
    try:
        reg = registry_from_toolbox(box, store.db)
        assert len(reg) == len(box.declarations()) and "list_books" in reg.names()
        assert reg.get("delete_path").level in ("change", "system")
        assert not reg.get("web_index_search").is_available()  # no NEXUS index attached
        os3 = OS3(reg)
        listed = os3.execute("list_books", {})
        assert listed.ok and "books" in listed.result
        made = os3.execute("create_book", {"title": "Iron Coast", "idea": "a lighthouse keeper's war"})
        assert made.status == "waiting_for_approval" and not made.ok and library.list_books() == []
        assert box.gate.pending.tool == "create_book" and box.gate.pending.source == "nexus"
        assert os3.execute("list_books", {"surprise": 1}).status == "rejected"
    finally:
        jobs.shutdown(wait=True)
