import pytest

from fakes import FakeGemini
from jarvis_books.assistant import ToolBox
from jarvis_books.assistant.nexus_tools import COLLECTIVE_TOOLS
from jarvis_books.assistant.permissions import NEEDS_YES, SYSTEM, TOOL_LEVELS
from jarvis_books.jobs import JobManager
from jarvis_books.memory import StoryIndex
from jarvis_books.nexus.api import NexusHub, systems_for
from jarvis_books.nexus.collective_os import CollectiveOS
from jarvis_books.nexus.contracts import NexusRequest
from jarvis_books.nexus.os1 import OS1
from jarvis_books.nexus.os2 import OS2
from jarvis_books.nexus.resources import ResourceProvider
from jarvis_books.writer import BookWriter
from nexus_site import FAST, FakeWeb, page

A = "https://atl.example/"


class QuietProbe(ResourceProvider):
    def telemetry(self):
        return {"cpu_percent": 5.0, "memory_free_gb": 8.0, "battery": {"present": False}, "temperature_c": None,
                "storage_free_gb": 50.0, "at": 0}


class FakeOllama:
    def __init__(self):
        self.asked = []

    def version(self):
        return "0.9.0"

    def list_models(self):
        return [{"name": "qwen3:4b"}]

    def chat(self, payload, timeout=None):
        self.asked.append(payload)
        return {"message": {"content": "Atlanta is in Georgia."}, "eval_count": 6, "prompt_eval_count": 9}


@pytest.fixture
def os1(tmp_path):
    web = FakeWeb({A: page("Atlanta Tax Sales Guide", "How the Fulton county tax sale works.")})
    o = OS1(tmp_path / "nx", FAST, fetcher_factory=web.factory())
    yield o
    o.close()


@pytest.fixture
def coll(tmp_path, os1):
    c = CollectiveOS(tmp_path / "nx", os1=os1, provider=QuietProbe(tmp_path), ollama=FakeOllama(), poll_s=0.05)
    yield c
    c.close()


def call(c, op, payload, role="owner"):
    return c.handle(NexusRequest("jarvis", "collective", op, payload), role)


def test_levels_gate_work_and_new_computers():
    assert set(COLLECTIVE_TOOLS) <= set(TOOL_LEVELS)
    assert TOOL_LEVELS["collective_status"] not in NEEDS_YES and TOOL_LEVELS["collective_task"] not in NEEDS_YES
    assert all(TOOL_LEVELS[t] in NEEDS_YES for t in ("collective_run", "collective_limits", "collective_worker"))
    assert TOOL_LEVELS["collective_join_code"] == SYSTEM


def test_roles_are_enforced_at_the_door(coll):
    assert call(coll, "status", {}, "reader").ok
    denied = call(coll, "submit", {"workload": "data.digest", "payload": {"items": ["x"]}}, "reader")
    assert not denied.ok and "reader may not submit" in denied.error_message
    assert call(coll, "join_code", {}, "operator").error_code == "unauthorized"
    assert call(coll, "join_code", {}).ok
    assert call(coll, "fly", {}).error_code == "unknown_operation"
    assert call(coll, "task", {"task_id": "nope"}).error_code == "invalid_request"


def test_a_remote_worker_runs_the_whole_protocol_through_the_hub(coll, os1):
    hub = NexusHub(systems_for(os1, OS2(os1), None, coll))
    route = lambda op, p, role="owner": hub.route(NexusRequest("jarvis", "collective", op, p), role)  # noqa: E731
    code = route("join_code", {"label": "desk pc"}).results[0]["code"]
    joined = route("enroll", {"code": code, "name": "desk pc", "runtimes": ["cpu"],
                              "profile": {"memory": {"total_gb": 32}}, "limits": {"max_memory_gb": 8}},
                   "operator").results[0]
    token = joined["token"]
    assert route("enroll", {"code": code, "name": "again", "profile": {}}).error_code == "unauthorized"
    assert route("heartbeat", {"worker_token": token, "telemetry": {"cpu_percent": 3}}).ok
    task = route("submit", {"workload": "data.digest", "payload": {"items": ["hi there"]},
                            "output_schema": {"type": "object", "required": ["digests"]}}).results[0]
    got = route("claim", {"worker_token": token}).results
    assert got and got[0]["id"] == task["id"] and got[0]["payload"] == {"items": ["hi there"]}
    assert route("start", {"worker_token": token, "task_id": task["id"]}).ok
    assert route("complete", {"worker_token": token, "task_id": task["id"],
                              "result": {"digests": [{"words": 2}]}}).results[0]["state"] == "completed"
    assert route("task", {"task_id": task["id"]}, "reader").results[0]["result"] == {"digests": [{"words": 2}]}
    bad = route("claim", {"worker_token": "x" * 40})
    assert bad.error_code == "unauthorized"
    wid = joined["worker_id"]
    assert route("set_status", {"worker_id": wid, "status": "revoked"}).ok
    assert route("heartbeat", {"worker_token": token, "telemetry": {}}).error_code == "unauthorized"
    assert "worker_token" not in str(hub.health())
    hub.close()


def test_this_computer_joins_once_runs_real_work_and_is_remembered(tmp_path, os1):
    ollama = FakeOllama()
    c = CollectiveOS(tmp_path / "nx", os1=os1, provider=QuietProbe(tmp_path), ollama=ollama, poll_s=0.05)
    first = c.start_local_worker()
    assert first["state"] == "available" and "llm.generate" in first["workloads"]
    mine = [w for w in call(c, "workers", {}).results if w["local"]]
    assert len(mine) == 1 and "ollama" in mine[0]["runtimes"] and mine[0]["models"] == ["qwen3:4b"]
    c.close()
    c2 = CollectiveOS(tmp_path / "nx", os1=os1, provider=QuietProbe(tmp_path), ollama=ollama, poll_s=0.05)
    c2.start_local_worker()
    again = [w for w in call(c2, "workers", {}).results if w["local"]]
    assert [w["id"] for w in again] == [mine[0]["id"]]
    c2.close()


@pytest.fixture
def tools(library, make_engine, coll):
    engine = make_engine(FakeGemini())
    index = StoryIndex(library, engine)
    jobs = JobManager(library, workers=1)
    box = ToolBox(library, BookWriter(library, engine, index), index, jobs)
    box.collective = coll
    yield box
    jobs.shutdown(wait=True)



def test_jarvis_tools_run_jobs_on_this_computer(tools, coll):
    coll.start_local_worker()
    out = tools._t_collective_run("llm.generate", text="Where is Atlanta?", wait=20)
    assert out["state"] == "completed" and out["result"]["text"] == "Atlanta is in Georgia."
    assert coll.ollama.asked[-1]["messages"][-1]["content"] == "Where is Atlanta?"
    digest = tools._t_collective_run("data.digest", texts=["a b c"], here_only=True, wait=20)
    assert digest["result"]["digests"][0]["words"] == 3
    status = tools._t_collective_status()
    assert status["computers"][0]["this_computer"] and status["tasks"]["completed"] == 2
    assert tools._t_collective_task(out["task_id"])["history"][0]["state"] == "completed"
    changed = tools._t_collective_limits(max_cpu_percent=25, hours=["22:00-06:00"])
    assert changed["limits"]["max_cpu_percent"] == 25 and coll.worker.limits.hours == ["22:00-06:00"]
    with pytest.raises(ValueError):
        tools._t_collective_limits(max_cpu_percent=500)
    assert len(tools._t_collective_join_code("desk")["code"]) > 8


def test_a_job_no_computer_can_run_says_why(tools, coll):
    coll.start_local_worker()
    out = tools._t_collective_run("llm.generate", text="hi", model="llama-70b", wait=0)
    assert out["state"] == "queued" and "llama-70b isn't installed" in str(out["why_waiting"])


def test_studio_wires_the_collective_into_jarvis(tmp_path):
    from conftest import hardware
    from jarvis_books.studio import Studio
    studio = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(),
                    engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None})
    studio.start(probe=False)
    try:
        _check_studio(studio)
    finally:
        studio.close()


def _check_studio(studio):
    assert "collective" in studio.nexus_hub.systems
    assert studio.tools.collective is studio.nexus_collective
    assert studio.tools.models is studio.nexus_models and studio.war_tools.models is studio.nexus_models
    assert studio.tools.releases is studio.releases
    center = studio.releases.center()
    assert center["running"]["commit"] and center["cycle"]["name"] == "NEXUS 1.0"
    assert studio.settings()["collective"] == "1"
    assert studio.nexus_hub.route(NexusRequest("jarvis", "collective", "status", {})).ok
