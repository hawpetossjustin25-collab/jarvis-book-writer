"""Scheduler self-improvement: try new Collective scoring weights against the placements it really made, and keep
a change only when it would have picked measurably better computers on placements it never trained on.

A placement counts as a case when two or more eligible computers have measured results for that kind of job. Its
cost is the regret of the pick: the chosen computer's measured cost divided by the best eligible computer's, where
cost = average seconds x (1 + 2 x failure rate). A perfect scheduler scores 1.0. Cases are split for good by a
hash of the task id: 80% to train candidates on, 20% sealed. A candidate is kept only if it beats the current
weights on the training cases by at least 1% and is no worse on the sealed ones. Every try is saved, with the
reason, and rollback returns to the version before (or the defaults)."""
from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import fields
from typing import Optional

from jarvis_books.nexus.collective import Coordinator, ScoreWeights

MIN_CASES = 20
MIN_SEALED = 4
MIN_GAIN = 0.01
FAIL_PENALTY = 2.0
WEIGHT_RANGE = (0.0, 3.0)


def _sealed(task_id: str) -> bool:
    return int(hashlib.sha256(task_id.encode()).hexdigest()[:8], 16) % 5 == 0


class SchedulerTuner:
    def __init__(self, coordinator: Coordinator, clock=time.time):
        self.c, self.clock = coordinator, clock

    def cases(self) -> list[dict]:
        rows = self.c.db.query("SELECT d.task_id, d.features, t.workload FROM decisions d JOIN tasks t "
                               "ON t.id = d.task_id WHERE d.chosen IS NOT NULL ORDER BY d.id")
        stats: dict = {}
        out = []
        for r in rows:
            feats = json.loads(r["features"] or "{}")
            costs = {}
            for wid in feats:
                key = (wid, r["workload"])
                if key not in stats:
                    stats[key] = self.c.worker_stats(wid, r["workload"])
                s = stats[key]
                if s["avg_s"]:
                    costs[wid] = s["avg_s"] * (1 + FAIL_PENALTY * s["failed"] / max(1, s["done"] + s["failed"]))
            if len(costs) >= 2:
                out.append({"task_id": r["task_id"], "features": {w: feats[w] for w in costs}, "costs": costs,
                            "sealed": _sealed(r["task_id"])})
        return out

    @staticmethod
    def regret(weights: ScoreWeights, cases: list[dict]) -> Optional[float]:
        if not cases:
            return None
        total = 0.0
        for case in cases:
            scores = {w: weights.score(f) for w, f in case["features"].items()}
            pick = max(scores, key=lambda k: (scores[k], k))
            total += case["costs"][pick] / min(case["costs"].values())
        return round(total / len(cases), 4)

    def _record(self, weights: ScoreWeights, status: str, metrics: dict, n: int, reason: str) -> int:
        with self.c.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO scheduler_versions(weights, status, train, sealed, baseline_train, baseline_sealed, "
                "cases, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (json.dumps(weights.to_dict()), status, metrics.get("train"), metrics.get("sealed"),
                 metrics.get("baseline_train"), metrics.get("baseline_sealed"), n, reason, self.clock()))
            return cur.lastrowid

    def improve(self, candidates: int = 12, seed: Optional[int] = None) -> dict:
        cases = self.cases()
        train = [c for c in cases if not c["sealed"]]
        sealed = [c for c in cases if c["sealed"]]
        if len(cases) < MIN_CASES or len(sealed) < MIN_SEALED or not train:
            return {"kept": False, "cases": len(cases), "sealed": len(sealed),
                    "reason": f"not enough measured placements yet: {len(cases)} with two or more measured "
                              f"computers ({len(sealed)} sealed); needs {MIN_CASES} ({MIN_SEALED} sealed)"}
        rng = random.Random(seed)
        current = self.c.scheduler.weights
        base = {"baseline_train": self.regret(current, train), "baseline_sealed": self.regret(current, sealed)}
        tried = []
        for _ in range(max(1, candidates)):
            cand = ScoreWeights(**{f.name: round(min(WEIGHT_RANGE[1], max(WEIGHT_RANGE[0],
                                   getattr(current, f.name) * rng.uniform(0.5, 1.5) + rng.uniform(-0.1, 0.1))), 3)
                                   for f in fields(ScoreWeights)})
            tried.append((self.regret(cand, train), cand))
        tried.sort(key=lambda t: t[0])
        best_train, best = tried[0]
        metrics = dict(base, train=best_train, sealed=self.regret(best, sealed))
        if best_train > base["baseline_train"] * (1 - MIN_GAIN):
            reason = (f"best of {len(tried)} tries scored {best_train} on training placements, not 1% better than "
                      f"the current {base['baseline_train']}")
            kept = False
        elif metrics["sealed"] > base["baseline_sealed"]:
            reason = (f"better on training ({best_train} vs {base['baseline_train']}) but worse on sealed placements "
                      f"({metrics['sealed']} vs {base['baseline_sealed']})")
            kept = False
        else:
            reason = (f"picks better computers: training {base['baseline_train']} -> {best_train}, sealed "
                      f"{base['baseline_sealed']} -> {metrics['sealed']} (1.0 is perfect)")
            kept = True
        if kept and not self.c.db.query_one("SELECT 1 FROM scheduler_versions WHERE status='baseline'"):
            self._record(current, "baseline", base, len(cases), "the weights before the first change")
        vid = self._record(best, "kept" if kept else "rejected", metrics, len(cases), reason)
        if kept:
            self.c.scheduler.weights = best
        return {"kept": kept, "version": vid, "cases": len(cases), "sealed": len(sealed), "reason": reason,
                **metrics, "weights": (best if kept else current).to_dict()}

    def rollback(self) -> dict:
        kept = self.c.db.query("SELECT id FROM scheduler_versions WHERE status='kept' ORDER BY id DESC LIMIT 1")
        if not kept:
            return {"rolled_back": False, "reason": "the scheduler is already on its original weights"}
        with self.c.db.transaction() as conn:
            conn.execute("UPDATE scheduler_versions SET status='rolled_back' WHERE id=?", (kept[0]["id"],))
        prev = self.c.db.query_one("SELECT weights FROM scheduler_versions WHERE status IN ('kept','baseline') "
                                   "ORDER BY status='kept' DESC, id DESC")
        self.c.scheduler.weights = ScoreWeights.from_dict(json.loads(prev["weights"])) if prev else ScoreWeights()
        return {"rolled_back": True, "undid_version": kept[0]["id"], "weights": self.c.scheduler.weights.to_dict()}

    def status(self, limit: int = 10) -> dict:
        cases = self.cases()
        hist = [dict(r) for r in self.c.db.query("SELECT * FROM scheduler_versions ORDER BY id DESC LIMIT ?",
                                                 (limit,))]
        for h in hist:
            h["weights"] = json.loads(h["weights"])
        return {"weights": self.c.scheduler.weights.to_dict(), "cases": len(cases),
                "sealed": sum(c["sealed"] for c in cases),
                "regret_now": self.regret(self.c.scheduler.weights, cases), "history": hist}
