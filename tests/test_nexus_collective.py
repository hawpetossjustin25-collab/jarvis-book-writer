import subprocess
import threading
import time
from datetime import datetime

import pytest

from jarvis_books.nexus.collective import CollectiveError, Coordinator
from jarvis_books.nexus.resources import ContributionLimits, ResourceProvider, available_for_contribution
from jarvis_books.nexus.worker import LocalLink, Worker, digest_handler


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Probe(ResourceProvider):
    """Real limits logic, scripted telemetry."""

    def __init__(self, cpu=10.0, free=8.0, battery=None):
        super().__init__(system="linux")
        self.tel = {"cpu_percent": cpu, "memory_free_gb": free, "battery": battery or {"present": False},
                    "temperature_c": None, "storage_free_gb": 50.0, "at": 0}

    def telemetry(self):
        return dict(self.tel)


PROFILE = {"memory": {"total_gb": 16.0}, "network": {"latency_ms": 5}, "models": []}


def enroll(c, name="laptop", local=True, runtimes=("cpu",), limits=None, profile=PROFILE):
    code = c.issue_enrollment(name)["code"]
    return c.enroll(code, name, profile, limits or {"max_cpu_percent": 90}, local=local, runtimes=list(runtimes))


@pytest.fixture
def coord(tmp_path):
    clock = Clock()
    c = Coordinator(tmp_path, clock=clock)
    c.test_clock = clock
    yield c
    c.close()


def test_join_codes_work_once_and_expire(coord):
    code = coord.issue_enrollment("pc", ttl_s=60)["code"]
    first = coord.enroll(code, "pc", PROFILE)
    assert coord.authenticate(first["token"]) == first["worker_id"]
    with pytest.raises(CollectiveError, match="wrong, used or expired"):
        coord.enroll(code, "again", PROFILE)
    late = coord.issue_enrollment("pc2", ttl_s=60)["code"]
    coord.test_clock.t += 61
    with pytest.raises(CollectiveError):
        coord.enroll(late, "pc2", PROFILE)
    with pytest.raises(CollectiveError):
        coord.enroll("guess-1234", "x", PROFILE)
    assert [e["kind"] for e in coord.events(kind="worker.registered")] == ["worker.registered"]


def test_credentials_are_short_lived_refreshable_and_revocable(coord):
    e = enroll(coord)
    coord.test_clock.t += 1800
    new = coord.refresh(e["token"])
    with pytest.raises(CollectiveError):
        coord.authenticate(e["token"])  # the old one stops working once refreshed
    coord.test_clock.t += 3601
    with pytest.raises(CollectiveError, match="expired"):
        coord.authenticate(new["token"])
    fresh = coord.credential_for_local(e["worker_id"])
    coord.set_status(e["worker_id"], "revoked")
    with pytest.raises(CollectiveError):
        coord.authenticate(fresh)
    with pytest.raises(CollectiveError):
        coord.heartbeat(e["worker_id"], {})


def test_task_runs_end_to_end_with_validated_output_and_lifecycle_events(coord):
    e = enroll(coord)
    w = Worker(LocalLink(coord, e["token"]), Probe(), ContributionLimits(max_cpu_percent=90),
               {"data.digest": digest_handler}, ["cpu"])
    t = coord.submit("data.digest", {"items": ["one two", "three"]},
                     output_schema={"type": "object", "required": ["digests"]})
    assert t["state"] == "assigned" and t["worker_id"] == e["worker_id"]
    out = w.run_once()
    assert out["state"] == "completed"
    done = coord.task(t["id"])
    assert done["state"] == "completed" and done["result"]["digests"][0]["words"] == 2
    assert [h["state"] for h in done["history"]] == ["completed"] and done["history"][0]["software"]
    kinds = [ev["kind"] for ev in coord.events() if ev["subject"] == t["id"]]
    assert kinds == ["task.created", "task.assigned", "task.accepted", "task.started", "task.completed"]


def test_idempotency_key_never_makes_a_second_task(coord):
    enroll(coord)
    a = coord.submit("data.digest", {"items": ["x"]}, idempotency_key="job-1")
    b = coord.submit("data.digest", {"items": ["something else"]}, idempotency_key="job-1")
    assert b["id"] == a["id"] and b["duplicate"]
    assert len(coord.tasks()) == 1


def test_bad_tasks_are_refused_at_the_door(coord):
    with pytest.raises(CollectiveError, match="unknown workload"):
        coord.submit("mine.bitcoin", {})
    with pytest.raises(CollectiveError, match="items"):
        coord.submit("data.digest", {"wrong": 1})
    with pytest.raises(CollectiveError, match="deadline"):
        coord.submit("data.digest", {"items": ["x"]}, deadline=coord.test_clock.t - 1)
    with pytest.raises(CollectiveError):
        coord.submit("data.digest", {"items": ["x"]}, timeout_s=0)


def test_output_that_breaks_the_expected_format_is_retried_then_failed(coord):
    e = enroll(coord)
    w = Worker(LocalLink(coord, e["token"]), Probe(), ContributionLimits(max_cpu_percent=90),
               {"data.digest": lambda p, s: {"nope": 1}}, ["cpu"])
    t = coord.submit("data.digest", {"items": ["x"]}, max_attempts=2,
                     output_schema={"type": "object", "required": ["digests"]})
    w.run_once()
    assert coord.task(t["id"])["state"] == "assigned" and coord.task(t["id"])["attempts"] == 2
    w.run_once()
    final = coord.task(t["id"])
    assert final["state"] == "failed" and "gave up after 2" in final["error"] and final["result"] is None


def test_a_crashing_task_is_retried_and_a_duplicate_delivery_changes_nothing(coord):
    e = enroll(coord)
    calls = {"n": 0}

    def flaky(payload, stop):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk hiccup")
        return digest_handler(payload, stop)

    w = Worker(LocalLink(coord, e["token"]), Probe(), ContributionLimits(max_cpu_percent=90),
               {"data.digest": flaky}, ["cpu"])
    t = coord.submit("data.digest", {"items": ["x"]})
    assert w.run_once()["state"] == "failed"
    assert w.run_once()["state"] == "completed"
    assert coord.complete(e["worker_id"], t["id"], {"digests": []})["duplicate"]
    assert coord.task(t["id"])["result"]["digests"][0]["bytes"] == 1


def test_timeouts_and_cancellation_stop_the_work(coord):
    e = enroll(coord)

    def slow(payload, stop):
        stop.wait(10)
        raise InterruptedError("stopped")

    w = Worker(LocalLink(coord, e["token"]), Probe(), ContributionLimits(max_cpu_percent=90),
               {"data.digest": slow}, ["cpu"])
    t = coord.submit("data.digest", {"items": ["x"]}, timeout_s=1, max_attempts=1)
    started = time.monotonic()
    out = w.run_once()
    assert out["state"] == "failed" and "longer than 1" in out["error"] and time.monotonic() - started < 5
    assert coord.task(t["id"])["state"] == "failed"
    t2 = coord.submit("data.digest", {"items": ["x"]}, timeout_s=60)
    threading.Timer(0.6, coord.cancel, args=(t2["id"],)).start()
    assert w.run_once()["state"] == "cancelled"
    assert coord.task(t2["id"])["state"] == "cancelled"
    w.stop()


def test_owner_limits_keep_a_busy_or_unplugged_computer_out(coord):
    e = enroll(coord)
    busy = Probe(cpu=95.0)
    w = Worker(LocalLink(coord, e["token"]), busy, ContributionLimits(max_cpu_percent=60),
               {"data.digest": digest_handler}, ["cpu"])
    coord.submit("data.digest", {"items": ["x"]})
    assert w.run_once() is None and "95% busy" in w.last_refusals[0]
    low = ContributionLimits(min_battery_percent=40)
    assert low.refusals({"battery": {"present": True, "percent": 20, "plugged_in": False}})
    assert not low.refusals({"battery": {"present": True, "percent": 20, "plugged_in": True}})
    night = ContributionLimits(hours=["22:00-06:00"])
    assert night.in_hours(datetime(2026, 1, 1, 23, 30)) and night.in_hours(datetime(2026, 1, 1, 5, 0))
    assert not night.in_hours(datetime(2026, 1, 1, 12, 0))
    assert ContributionLimits(enabled=False).refusals({})[0].startswith("contribution is paused")
    with pytest.raises(ValueError):
        ContributionLimits.from_dict({"max_cpu_percent": 300})
    with pytest.raises(ValueError):
        ContributionLimits.from_dict({"hours": ["9am-5pm"]})
    with pytest.raises(ValueError, match="unknown limit"):
        ContributionLimits.from_dict({"steal": True})


def test_scheduler_rejects_incompatible_workers_and_explains_why(coord):
    small = enroll(coord, "phone", local=False, limits={"max_memory_gb": 1.0})
    big = enroll(coord, "pc", local=False, runtimes=("cpu", "ollama"), limits={"max_memory_gb": 8.0})
    t = coord.submit("llm.generate", {"prompt": "hi"})
    assert t["worker_id"] == big["worker_id"]
    why = coord.decisions(t["id"])[0]["rejected"][small["worker_id"]]
    assert any("ollama" in r for r in why) and any("needs 3 GB" in r for r in why)
    only_here = coord.submit("data.digest", {"items": ["x"]}, requirements={"local_only": True})
    assert only_here["state"] == "queued"  # neither worker is this computer


def test_scheduler_prefers_measured_faster_reliable_workers_and_skips_stale_ones(coord):
    a = enroll(coord, "a", local=False)
    b = enroll(coord, "b", local=False)
    clock = coord.test_clock
    for wid, secs in ((a["worker_id"], 30.0), (b["worker_id"], 2.0)):  # history: a is slow, b is fast
        for _ in range(3):
            t = coord.submit("data.digest", {"items": ["x"]}, requirements={"local_only": False})
            coord.db.execute("UPDATE tasks SET worker_id=?, state='running' WHERE id=?", (wid, t["id"]))
            coord.db.execute("UPDATE task_attempts SET worker_id=?, state='running', started_at=? WHERE task_id=?",
                             (wid, clock.t, t["id"]))
            clock.t += secs
            coord.complete(wid, t["id"], {"digests": []})
    pick = coord.submit("data.digest", {"items": ["y"]})
    assert pick["worker_id"] == b["worker_id"]
    clock.t += 500  # nobody has sent a heartbeat for a while
    coord.heartbeat(a["worker_id"], {"cpu_percent": 5})
    later = coord.submit("data.digest", {"items": ["z"]})
    assert later["worker_id"] == a["worker_id"]
    assert "no heartbeat lately" in coord.decisions(later["id"])[0]["rejected"][b["worker_id"]]


def test_priority_and_deadlines(coord):
    t_low = coord.submit("data.digest", {"items": ["low"]}, priority=-5)
    t_high = coord.submit("data.digest", {"items": ["high"]}, priority=50)
    soon = coord.submit("data.digest", {"items": ["soon"]}, deadline=coord.test_clock.t + 10)
    e = enroll(coord)
    first = coord.claim(e["worker_id"])
    assert first["id"] == t_high["id"]
    coord.test_clock.t += 11
    coord.sweep()
    assert coord.task(soon["id"])["state"] == "expired" and coord.task(t_low["id"])["state"] != "expired"


def test_a_restarted_coordinator_requeues_work_from_a_vanished_worker(tmp_path):
    clock = Clock()
    c = Coordinator(tmp_path, clock=clock)
    gone = enroll(c, "gone", local=False)
    t = c.submit("data.digest", {"items": ["x"]})
    c.claim(gone["worker_id"])
    c.start(gone["worker_id"], t["id"])
    c.close()
    clock.t += 3600  # the coordinator was off; the worker never came back
    c2 = Coordinator(tmp_path, clock=clock)
    task = c2.task(t["id"])
    assert task["state"] == "queued" and task["history"][-1]["state"] == "expired"
    fresh = enroll(c2, "fresh")
    w = Worker(LocalLink(c2, fresh["token"]), Probe(), ContributionLimits(max_cpu_percent=90),
               {"data.digest": digest_handler}, ["cpu"])
    assert w.run_once()["state"] == "completed" and c2.task(t["id"])["attempts"] == 2
    c2.close()


def test_pausing_a_worker_hands_its_unstarted_work_to_others(coord):
    a = enroll(coord, "a", local=True)
    t = coord.submit("data.digest", {"items": ["x"]})
    assert coord.task(t["id"])["worker_id"] == a["worker_id"]
    coord.set_status(a["worker_id"], "paused")
    assert coord.task(t["id"])["state"] == "queued" and coord.claim(a["worker_id"]) is None
    b = enroll(coord, "b", local=False)
    coord.schedule()
    assert coord.task(t["id"])["worker_id"] == b["worker_id"]
    assert coord.set_limits(b["worker_id"], {"max_cpu_percent": 30})["max_cpu_percent"] == 30


def test_a_worker_refuses_tasks_it_cannot_run_and_they_go_back(coord):
    e = enroll(coord, runtimes=("cpu",))
    w = Worker(LocalLink(coord, e["token"]), Probe(free=1.0), ContributionLimits(max_cpu_percent=90),
               {"data.digest": digest_handler}, ["cpu"])
    t = coord.submit("data.digest", {"items": ["x"]}, requirements={"memory_gb": 0.5})
    out = w.run_once()
    assert out["state"] == "refused" and "GB" in out["error"]
    assert coord.task(t["id"])["state"] in ("queued", "assigned")


def test_resource_detection_reports_four_separate_levels():
    def run(args, **kw):
        if args[0] == "nvidia-smi":
            return subprocess.CompletedProcess(args, 0, "NVIDIA GeForce RTX 3060, 12288, 11000, 45\n", "")
        if args[0] == "lspci":
            return subprocess.CompletedProcess(args, 0, "00:02.0 VGA compatible controller: Intel UHD Graphics 620\n"
                                                        "01:00.0 3D controller: NVIDIA GeForce RTX 3060\n", "")
        return subprocess.CompletedProcess(args, 1, "", "")

    p = ResourceProvider(run=run, which=lambda name: f"/usr/bin/{name}", system="linux")
    gpus = p.detect_gpu()
    nv = [g for g in gpus if g["vendor"] == "NVIDIA"][0]
    intel = [g for g in gpus if g["vendor"] == "Intel"][0]
    assert nv["runtime"] == ["cuda"] and nv["vram_gb"] == 12.0 and nv["temperature_c"] == 45.0
    assert intel["detected"] and intel["runtime"] == []  # seen, but nothing here can run AI work on it
    profile = {"cpu": {"threads": 4}, "memory": {"total_gb": 16}, "gpus": gpus, "npus": [],
               "storage": {"free_gb": 100}, "runtimes": {"ollama": "0.5"}}
    off = available_for_contribution(profile, ContributionLimits())
    assert off["gpus"] == [] and off["runtimes"] == ["cpu", "ollama"] and off["storage_gb"] == 0
    on = available_for_contribution(profile, ContributionLimits(gpu=True, storage=True, max_memory_gb=6))
    assert on["gpus"] == ["NVIDIA GeForce RTX 3060"] and "cuda" in on["runtimes"] and on["memory_gb"] == 6
    nothing = ResourceProvider(run=run, which=lambda name: None, system="linux")
    assert nothing.detect_gpu() == []


def test_real_probe_of_this_computer():
    p = ResourceProvider()
    prof = p.profile(network=False)
    assert prof["cpu"]["threads"] >= 1 and prof["memory"]["total_gb"] > 0
    assert prof["storage"]["free_gb"] is not None and 0 <= (p.cpu_load() or 0) <= 100
