"""Scheduler self-improvement: measured regret on real placements, sealed check, persistence and rollback."""
import json

from jarvis_books.nexus.collective import Coordinator, ScoreWeights
from jarvis_books.nexus.sched_tune import SchedulerTuner

FAST = {"throughput": 0.5, "reliability": 0.6, "memory": 0.25, "network": 0.33, "load": 0.0, "cost": 0.5,
        "local": False}
SLOW = dict(FAST, reliability=0.5, memory=0.5)  # more memory, but measured 10x slower


def seed(c: Coordinator, n: int, fast=FAST, slow=SLOW, start: int = 0) -> None:
    with c.db.transaction() as conn:
        for i in range(start, start + n):
            tid = f"t{i:04d}"
            conn.execute("INSERT INTO tasks(id, workload, payload, timeout_s, state, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?)", (tid, "search.query", "{}", 60, "completed", i, i))
            wid, secs = ("w-fast", 10.0) if i % 2 else ("w-slow", 100.0)
            conn.execute("INSERT INTO task_attempts(task_id, worker_id, state, assigned_at, started_at, finished_at) "
                         "VALUES (?,?,?,?,?,?)", (tid, wid, "completed", i, i, i + secs))
            scores = {"w-fast": ScoreWeights().score(fast), "w-slow": ScoreWeights().score(slow)}
            conn.execute("INSERT INTO decisions(task_id, chosen, scores, rejected, at, features) VALUES (?,?,?,?,?,?)",
                         (tid, max(scores, key=scores.get), json.dumps(scores), "{}", i,
                          json.dumps({"w-fast": fast, "w-slow": slow})))


def test_weights_score_recorded_features():
    w = ScoreWeights()
    assert round(w.score(SLOW), 4) > round(w.score(FAST), 4)  # the default weights pick the slow computer here
    assert ScoreWeights.from_dict(dict(w.to_dict(), bogus=1)) == w


def test_too_few_measured_placements_changes_nothing(tmp_path):
    c = Coordinator(tmp_path)
    seed(c, 6)
    out = SchedulerTuner(c).improve(seed=1)
    assert out["kept"] is False and "not enough measured placements" in out["reason"]
    assert c.db.query_one("SELECT COUNT(*) FROM scheduler_versions")[0] == 0


def test_a_better_weighting_is_kept_persisted_and_rolled_back(tmp_path):
    c = Coordinator(tmp_path)
    seed(c, 40)
    tuner = SchedulerTuner(c)
    before = tuner.status()
    assert before["cases"] == 40 and before["sealed"] >= 4 and before["regret_now"] == 10.0
    out = tuner.improve(candidates=60, seed=3)
    assert out["kept"] is True, out
    assert out["train"] == 1.0 and out["sealed"] == 1.0 and out["baseline_train"] == 10.0
    assert c.scheduler.weights.score(FAST) > c.scheduler.weights.score(SLOW)
    hist = tuner.status()["history"]
    assert [h["status"] for h in hist] == ["kept", "baseline"] and "picks better computers" in hist[0]["reason"]
    c.close()

    again = Coordinator(tmp_path)  # a restart keeps the improvement
    assert again.scheduler.weights.to_dict() == out["weights"]
    back = SchedulerTuner(again).rollback()
    assert back["rolled_back"] is True and back["weights"] == ScoreWeights().to_dict()
    assert SchedulerTuner(again).rollback()["rolled_back"] is False
    again.close()
    assert Coordinator(tmp_path).scheduler.weights == ScoreWeights()


def test_no_measurable_gain_is_rejected_and_remembered(tmp_path):
    c = Coordinator(tmp_path)
    good = dict(FAST, reliability=0.9, memory=0.5)  # the defaults already pick the fast computer
    seed(c, 40, fast=good, slow=SLOW)
    out = SchedulerTuner(c).improve(candidates=10, seed=2)
    assert out["kept"] is False and "not 1% better" in out["reason"]
    assert c.scheduler.weights == ScoreWeights()
    assert [r["status"] for r in c.db.query("SELECT status FROM scheduler_versions")] == ["rejected"]


def test_unmeasured_workers_do_not_count(tmp_path):
    c = Coordinator(tmp_path)
    seed(c, 30)
    with c.db.transaction() as conn:
        conn.execute("UPDATE task_attempts SET state='failed' WHERE worker_id='w-slow'")
    assert SchedulerTuner(c).cases() == []


def test_the_collective_operation_and_jarvis_tools(tmp_path):
    from jarvis_books.assistant.permissions import CHANGE, READ, level_of
    from jarvis_books.nexus.collective_os import CollectiveOS
    os_ = CollectiveOS(tmp_path)
    seed(os_.coordinator, 40)
    st = os_.call("scheduler", {"action": "status"})
    assert st.ok and st.extra["cases"] == 40
    assert not os_.call("scheduler", {"action": "improve"}, role="reader").ok
    done = os_.call("scheduler", {"action": "improve", "candidates": 60, "seed": 3})
    assert done.ok and done.extra["kept"] is True
    assert os_.call("scheduler", {"action": "rollback"}).extra["rolled_back"] is True
    assert level_of("scheduler_status") == READ and level_of("scheduler_improve") == CHANGE
