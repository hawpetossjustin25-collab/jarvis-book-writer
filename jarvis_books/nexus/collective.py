"""NEXUS Collective: the coordinator that enrolls worker computers, keeps a durable task queue and places each
task on the best eligible worker using measured history, never invented hardware scores.

Task states: created -> queued -> assigned -> accepted -> running -> completed | failed | cancelled | expired.
Everything lives in SQLite (collective.db), so a restarted coordinator picks up exactly where it stopped: leases
that ran out are put back in the queue (or failed once their attempts are used up).
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import jsonschema

from jarvis_books.nexus.resources import ContributionLimits
from jarvis_books.storage.db import Database

log = logging.getLogger(__name__)

STATES = ("created", "queued", "assigned", "accepted", "running", "completed", "failed", "cancelled", "expired")
OPEN = ("queued", "assigned", "accepted", "running")
LEASED = ("assigned", "accepted", "running")
CREDENTIAL_TTL_S = 3600.0
ENROLL_TTL_S = 900.0
STALE_AFTER_S = 120.0

# Workload classes: what each kind of work needs. A worker that can't meet these is filtered out before scoring.
WORKLOADS: dict[str, dict] = {
    "llm.generate": {"class": "language-model inference", "runtime": "ollama", "memory_gb": 3.0,
                     "payload": {"type": "object", "required": ["prompt"], "additionalProperties": False,
                                 "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": 20000},
                                                "model": {"type": "string", "maxLength": 200},
                                                "max_tokens": {"type": "integer", "minimum": 1, "maximum": 4096},
                                                "system": {"type": "string", "maxLength": 4000}}}},
    "embed.text": {"class": "embedding generation", "runtime": "cpu", "memory_gb": 0.5,
                   "payload": {"type": "object", "required": ["texts"], "additionalProperties": False,
                               "properties": {"texts": {"type": "array", "minItems": 1, "maxItems": 256,
                                                        "items": {"type": "string", "maxLength": 20000}},
                                              "model": {"type": "string", "maxLength": 200}}}},
    "data.digest": {"class": "data processing", "runtime": "cpu", "memory_gb": 0.2,
                    "payload": {"type": "object", "required": ["items"], "additionalProperties": False,
                                "properties": {"items": {"type": "array", "minItems": 1, "maxItems": 10000,
                                                         "items": {"type": "string", "maxLength": 100000}}}}},
    "search.query": {"class": "search", "runtime": "nexus-index", "memory_gb": 0.3,
                     "payload": {"type": "object", "required": ["query"], "additionalProperties": False,
                                 "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 500},
                                                "max_results": {"type": "integer", "minimum": 1, "maximum": 50}}}},
}

MIGRATIONS = {1: """
CREATE TABLE workers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
    profile TEXT NOT NULL DEFAULT '{}', limits TEXT NOT NULL DEFAULT '{}', telemetry TEXT NOT NULL DEFAULT '{}',
    runtimes TEXT NOT NULL DEFAULT '[]', memory_gb REAL NOT NULL DEFAULT 0, software TEXT NOT NULL DEFAULT '',
    security TEXT NOT NULL DEFAULT '{}', local INTEGER NOT NULL DEFAULT 0,
    enrolled_at REAL NOT NULL, last_seen REAL NOT NULL, revoked_at REAL);
CREATE TABLE enrollments (
    code_hash TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, expires_at REAL NOT NULL,
    used_by TEXT);
CREATE TABLE credentials (
    token_hash TEXT PRIMARY KEY, worker_id TEXT NOT NULL REFERENCES workers(id), expires_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, workload TEXT NOT NULL, payload TEXT NOT NULL,
    requirements TEXT NOT NULL DEFAULT '{}', output_schema TEXT, priority INTEGER NOT NULL DEFAULT 0,
    timeout_s REAL NOT NULL, deadline REAL, max_attempts INTEGER NOT NULL DEFAULT 3,
    attempts INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, worker_id TEXT, lease_until REAL,
    result TEXT, error TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'owner',
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX tasks_open ON tasks(state, priority DESC, created_at);
CREATE TABLE task_attempts (
    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), worker_id TEXT NOT NULL,
    state TEXT NOT NULL, assigned_at REAL NOT NULL, started_at REAL, finished_at REAL, error TEXT NOT NULL DEFAULT '',
    software TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', resources TEXT NOT NULL DEFAULT '{}');
CREATE INDEX attempts_by_worker ON task_attempts(worker_id, state);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, chosen TEXT, scores TEXT NOT NULL, rejected TEXT NOT NULL,
    at REAL NOT NULL);
CREATE TABLE events (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}',
    at REAL NOT NULL);
CREATE INDEX events_kind ON events(kind, id);
"""}


class CollectiveError(ValueError):
    def __init__(self, message: str, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


@dataclass
class ScoreWeights:
    """worker_score = compatibility + throughput + memory + network + reliability - load - cost (inputs 0..1)."""
    compatibility: float = 1.0
    throughput: float = 1.0
    memory: float = 0.5
    network: float = 0.3
    reliability: float = 1.0
    load: float = 1.0
    cost: float = 0.5
    local_bonus: float = 0.2  # the coordinator's own computer needs no network hop

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Placement:
    chosen: Optional[str]
    scores: dict = field(default_factory=dict)
    rejected: dict = field(default_factory=dict)


class Scheduler:
    """Filters out workers that can't run a task, then ranks the rest from what they actually did before."""

    def __init__(self, coordinator: "Coordinator", weights: Optional[ScoreWeights] = None):
        self.c, self.weights = coordinator, weights or ScoreWeights()

    def rejections(self, worker: dict, task: dict, now: float) -> list[str]:
        spec = WORKLOADS[task["workload"]]
        req = task["requirements"]
        why = []
        if worker["status"] != "active":
            why.append(f"worker is {worker['status']}")
        if now - worker["last_seen"] > STALE_AFTER_S:
            why.append("no heartbeat lately")
        runtime = req.get("runtime", spec["runtime"])
        if runtime not in worker["runtimes"]:
            why.append(f"no {runtime} runtime")
        need = float(req.get("memory_gb", spec["memory_gb"]))
        if need > worker["memory_gb"]:
            why.append(f"needs {need:g} GB, worker offers {worker['memory_gb']:g} GB")
        if req.get("local_only") and not worker["local"]:
            why.append("task must stay on this computer")
        if req.get("model") and req["model"] not in (worker["profile"].get("models") or []):
            why.append(f"model {req['model']} isn't installed there")
        limits = ContributionLimits.from_dict(worker["limits"])
        tel = worker["telemetry"]
        why += [r for r in limits.refusals(tel, running=self.c.running_on(worker["id"]), need_memory_gb=need)
                if "outside the allowed hours" in r or "paused" in r or "battery" in r or "hot" in r
                or "already running" in r]
        if task["deadline"] is not None:
            est = self.c.measured_seconds(worker["id"], task["workload"])
            if est is not None and now + est > task["deadline"]:
                why.append(f"would take about {est:.0f} s, past the deadline")
        return why

    def place(self, task: dict, now: float) -> Placement:
        workers = self.c.workers(include_revoked=False)
        rejected, eligible = {}, []
        for w in workers:
            why = self.rejections(w, task, now)
            if why:
                rejected[w["id"]] = why
            else:
                eligible.append(w)
        if not eligible:
            return Placement(None, {}, rejected)
        stats = {w["id"]: self.c.worker_stats(w["id"], task["workload"]) for w in eligible}
        speeds = [s["avg_s"] for s in stats.values() if s["avg_s"]]
        fastest = min(speeds) if speeds else None
        max_mem = max(w["memory_gb"] for w in eligible) or 1.0
        wt = self.weights
        scores = {}
        for w in eligible:
            s = stats[w["id"]]
            throughput = (fastest / s["avg_s"]) if fastest and s["avg_s"] else 0.5  # unmeasured: middle
            reliability = (s["done"] + 1) / (s["done"] + s["failed"] + 2)  # Laplace: new workers start at 0.5
            lat = (w["profile"].get("network") or {}).get("latency_ms")
            network = 1.0 if w["local"] else (1.0 / (1.0 + (lat or 200) / 100.0))
            limits = ContributionLimits.from_dict(w["limits"])
            load = self.c.running_on(w["id"]) / max(1, limits.max_tasks)
            cpu = (w["telemetry"].get("cpu_percent") or 0) / 100.0
            cost = (s["avg_s"] / (s["avg_s"] + 60.0)) if s["avg_s"] else 0.5
            score = (wt.compatibility * 1.0 + wt.throughput * throughput + wt.memory * (w["memory_gb"] / max_mem)
                     + wt.network * network + wt.reliability * reliability
                     - wt.load * max(load, cpu) - wt.cost * cost + (wt.local_bonus if w["local"] else 0.0))
            scores[w["id"]] = round(score, 4)
        chosen = max(scores, key=lambda k: (scores[k], k))
        return Placement(chosen, scores, rejected)


class Coordinator:
    def __init__(self, home: Path | str, clock=time.time, software: str = "jarvis",
                 weights: Optional[ScoreWeights] = None):
        self.db = Database(Path(home) / "collective.db", migrations=MIGRATIONS)
        self.clock, self.software = clock, software
        self.scheduler = Scheduler(self, weights)
        self._lock = threading.RLock()
        self.recover()

    def close(self) -> None:
        self.db.close()

    # ---- events (durable; consumers read by id so they can be idempotent)
    def _event(self, conn, kind: str, subject: str, data: Optional[dict] = None) -> None:
        conn.execute("INSERT INTO events(kind, subject, data, at) VALUES (?,?,?,?)",
                     (kind, subject, _j(data or {}), self.clock()))

    def events(self, after_id: int = 0, kind: Optional[str] = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE id > ?", [after_id]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        rows = self.db.query(sql + " ORDER BY id LIMIT ?", (*args, limit))
        return [{"id": r["id"], "kind": r["kind"], "subject": r["subject"], "data": json.loads(r["data"]),
                 "at": r["at"]} for r in rows]

    # ---- enrollment and identity
    def issue_enrollment(self, label: str = "", ttl_s: float = ENROLL_TTL_S) -> dict:
        code = f"{secrets.randbelow(10 ** 8):08d}-{secrets.token_hex(4)}"
        now = self.clock()
        self.db.execute("INSERT INTO enrollments(code_hash, label, created_at, expires_at) VALUES (?,?,?,?)",
                        (_hash(code), label[:80], now, now + ttl_s))
        return {"code": code, "expires_at": now + ttl_s, "label": label[:80]}

    def enroll(self, code: str, name: str, profile: dict, limits: Optional[dict] = None,
               software: str = "", security: Optional[dict] = None, local: bool = False,
               runtimes: Optional[list[str]] = None) -> dict:
        lim = ContributionLimits.from_dict(limits)
        now = self.clock()
        with self._lock, self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM enrollments WHERE code_hash=?", (_hash(code or ""),)).fetchone()
            if row is None or row["used_by"] or row["expires_at"] < now:
                raise CollectiveError("that join code is wrong, used or expired; ask Jarvis for a new one",
                                      "unauthorized")
            wid = "w-" + uuid.uuid4().hex[:12]
            conn.execute("UPDATE enrollments SET used_by=? WHERE code_hash=?", (wid, row["code_hash"]))
            conn.execute("INSERT INTO workers(id, name, profile, limits, runtimes, memory_gb, software, security, "
                         "local, enrolled_at, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (wid, (name or "worker")[:80], _j(profile), _j(lim.to_dict()),
                          _j(runtimes or ["cpu"]), _memory_offer(profile, lim), software[:80],
                          _j(security or {}), int(local), now, now))
            token = self._issue_credential(conn, wid, now)
            self._event(conn, "worker.registered", wid, {"name": name, "local": local})
        return {"worker_id": wid, "token": token, "expires_at": now + CREDENTIAL_TTL_S}

    def _issue_credential(self, conn, wid: str, now: float) -> str:
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO credentials(token_hash, worker_id, expires_at) VALUES (?,?,?)",
                     (_hash(token), wid, now + CREDENTIAL_TTL_S))
        conn.execute("DELETE FROM credentials WHERE expires_at < ?", (now - CREDENTIAL_TTL_S,))
        return token

    def authenticate(self, token: str) -> str:
        row = self.db.query_one("SELECT c.worker_id, c.expires_at, c.revoked, w.status FROM credentials c "
                                "JOIN workers w ON w.id = c.worker_id WHERE c.token_hash=?", (_hash(token or ""),))
        if row is None or row["revoked"] or row["expires_at"] < self.clock() or row["status"] == "revoked":
            raise CollectiveError("worker credential is unknown, expired or revoked", "unauthorized")
        return row["worker_id"]

    def refresh(self, token: str) -> dict:
        wid = self.authenticate(token)
        now = self.clock()
        with self._lock, self.db.transaction() as conn:
            conn.execute("UPDATE credentials SET revoked=1 WHERE token_hash=?", (_hash(token),))
            new = self._issue_credential(conn, wid, now)
        return {"worker_id": wid, "token": new, "expires_at": now + CREDENTIAL_TTL_S}

    def credential_for_local(self, wid: str) -> str:
        """The coordinator's own computer gets credentials directly (it is the same program)."""
        with self._lock, self.db.transaction() as conn:
            row = conn.execute("SELECT local, status FROM workers WHERE id=?", (wid,)).fetchone()
            if row is None or not row["local"] or row["status"] == "revoked":
                raise CollectiveError("not this computer's worker", "unauthorized")
            return self._issue_credential(conn, wid, self.clock())

    # ---- worker state
    def _worker(self, row) -> dict:
        return {"id": row["id"], "name": row["name"], "status": row["status"],
                "profile": json.loads(row["profile"]), "limits": json.loads(row["limits"]),
                "telemetry": json.loads(row["telemetry"]), "runtimes": json.loads(row["runtimes"]),
                "memory_gb": row["memory_gb"], "software": row["software"], "local": bool(row["local"]),
                "enrolled_at": row["enrolled_at"], "last_seen": row["last_seen"],
                "healthy": row["status"] == "active" and self.clock() - row["last_seen"] <= STALE_AFTER_S}

    def worker(self, wid: str) -> dict:
        row = self.db.query_one("SELECT * FROM workers WHERE id=?", (wid,))
        if row is None:
            raise CollectiveError(f"no worker {wid}", "not_found")
        return self._worker(row)

    def workers(self, include_revoked: bool = True) -> list[dict]:
        sql = "SELECT * FROM workers" + ("" if include_revoked else " WHERE status != 'revoked'")
        return [self._worker(r) for r in self.db.query(sql + " ORDER BY enrolled_at")]

    def heartbeat(self, wid: str, telemetry: dict, profile: Optional[dict] = None,
                  runtimes: Optional[list[str]] = None) -> dict:
        now = self.clock()
        with self._lock, self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM workers WHERE id=?", (wid,)).fetchone()
            if row is None or row["status"] == "revoked":
                raise CollectiveError("this worker was removed from the Collective", "unauthorized")
            sets, args = ["telemetry=?", "last_seen=?"], [_j(telemetry), now]
            if profile is not None:
                lim = ContributionLimits.from_dict(json.loads(row["limits"]))
                sets += ["profile=?", "memory_gb=?"]
                args += [_j(profile), _memory_offer(profile, lim)]
                self._event(conn, "worker.capability.updated", wid, {})
            if runtimes is not None:
                sets.append("runtimes=?")
                args.append(_j(runtimes))
            conn.execute(f"UPDATE workers SET {', '.join(sets)} WHERE id=?", (*args, wid))
            self._event(conn, "worker.heartbeat", wid, {"cpu": telemetry.get("cpu_percent")})
            conn.execute("DELETE FROM events WHERE kind='worker.heartbeat' AND id < "
                         "(SELECT COALESCE(MAX(id), 0) - 2000 FROM events)")
        return {"status": row["status"], "limits": json.loads(row["limits"])}

    def set_limits(self, wid: str, limits: dict) -> dict:
        current = self.worker(wid)
        lim = ContributionLimits.from_dict({**current["limits"], **limits})
        with self._lock, self.db.transaction() as conn:
            conn.execute("UPDATE workers SET limits=?, memory_gb=? WHERE id=?",
                         (_j(lim.to_dict()), _memory_offer(current["profile"], lim), wid))
            self._event(conn, "worker.capability.updated", wid, {"limits": lim.to_dict()})
        return lim.to_dict()

    def set_status(self, wid: str, status: str) -> dict:
        if status not in ("active", "paused", "revoked"):
            raise CollectiveError("status is active, paused or revoked")
        self.worker(wid)
        now = self.clock()
        with self._lock, self.db.transaction() as conn:
            conn.execute("UPDATE workers SET status=?, revoked_at=? WHERE id=?",
                         (status, now if status == "revoked" else None, wid))
            if status == "revoked":
                conn.execute("UPDATE credentials SET revoked=1 WHERE worker_id=?", (wid,))
            if status != "active":  # its unstarted work goes to someone else
                for t in conn.execute("SELECT id FROM tasks WHERE worker_id=? AND state IN ('assigned','accepted')",
                                      (wid,)).fetchall():
                    self._requeue(conn, t["id"], f"worker {status}", now, count_attempt=False)
            self._event(conn, {"paused": "worker.paused", "revoked": "worker.revoked"}.get(status, "worker.resumed"),
                        wid, {})
        return self.worker(wid)

    # ---- tasks
    def submit(self, workload: str, payload: dict, requirements: Optional[dict] = None, priority: int = 0,
               timeout_s: float = 300.0, deadline: Optional[float] = None, max_attempts: int = 3,
               idempotency_key: Optional[str] = None, output_schema: Optional[dict] = None,
               scope: str = "owner") -> dict:
        spec = WORKLOADS.get(workload)
        if spec is None:
            raise CollectiveError(f"unknown workload {workload!r}; known: {', '.join(sorted(WORKLOADS))}")
        try:
            jsonschema.validate(payload, spec["payload"])
            if output_schema is not None:
                jsonschema.Draft7Validator.check_schema(output_schema)
        except (jsonschema.ValidationError, jsonschema.SchemaError) as exc:
            raise CollectiveError(f"{workload}: {exc.message}") from None
        if not 1 <= timeout_s <= 86_400 or not 1 <= max_attempts <= 10 or not -100 <= priority <= 100:
            raise CollectiveError("timeout 1-86400 s, attempts 1-10, priority -100..100")
        now = self.clock()
        if deadline is not None and deadline <= now:
            raise CollectiveError("the deadline has already passed")
        with self._lock, self.db.transaction() as conn:
            if idempotency_key:
                old = conn.execute("SELECT id FROM tasks WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if old is not None:
                    return self.task(old["id"]) | {"duplicate": True}
            tid = "t-" + uuid.uuid4().hex[:16]
            conn.execute("INSERT INTO tasks(id, idempotency_key, workload, payload, requirements, output_schema, "
                         "priority, timeout_s, deadline, max_attempts, state, scope, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tid, idempotency_key, workload, _j(payload), _j(requirements or {}),
                          _j(output_schema) if output_schema is not None else None, priority, timeout_s, deadline,
                          max_attempts, "created", scope, now, now))
            self._event(conn, "task.created", tid, {"workload": workload})
            conn.execute("UPDATE tasks SET state='queued' WHERE id=?", (tid,))
        self.schedule()
        return self.task(tid)

    def _task(self, row) -> dict:
        return {"id": row["id"], "workload": row["workload"], "payload": json.loads(row["payload"]),
                "requirements": json.loads(row["requirements"]),
                "output_schema": json.loads(row["output_schema"]) if row["output_schema"] else None,
                "priority": row["priority"], "timeout_s": row["timeout_s"], "deadline": row["deadline"],
                "max_attempts": row["max_attempts"], "attempts": row["attempts"], "state": row["state"],
                "worker_id": row["worker_id"], "lease_until": row["lease_until"],
                "result": json.loads(row["result"]) if row["result"] else None, "error": row["error"],
                "idempotency_key": row["idempotency_key"], "scope": row["scope"],
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def task(self, tid: str) -> dict:
        row = self.db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        if row is None:
            raise CollectiveError(f"no task {tid}", "not_found")
        out = self._task(row)
        out["history"] = [dict(r) for r in self.db.query(
            "SELECT worker_id, state, assigned_at, started_at, finished_at, error, software, model FROM task_attempts "
            "WHERE task_id=? ORDER BY id", (tid,))]
        return out

    def tasks(self, state: Optional[str] = None, limit: int = 50) -> list[dict]:
        if state and state not in STATES:
            raise CollectiveError(f"state is one of {', '.join(STATES)}")
        rows = self.db.query("SELECT * FROM tasks" + (" WHERE state=?" if state else "") +
                             " ORDER BY created_at DESC LIMIT ?", ((state, limit) if state else (limit,)))
        return [self._task(r) for r in rows]

    def running_on(self, wid: str) -> int:
        return self.db.query_one("SELECT COUNT(*) FROM tasks WHERE worker_id=? AND state IN ('accepted','running')",
                                 (wid,))[0]

    def worker_stats(self, wid: str, workload: str) -> dict:
        row = self.db.query_one(
            "SELECT SUM(a.state='completed') AS done, SUM(a.state IN ('failed','expired')) AS failed, "
            "AVG(CASE WHEN a.state='completed' THEN a.finished_at - a.started_at END) AS avg_s "
            "FROM task_attempts a JOIN tasks t ON t.id = a.task_id WHERE a.worker_id=? AND t.workload=?",
            (wid, workload))
        return {"done": row["done"] or 0, "failed": row["failed"] or 0,
                "avg_s": round(row["avg_s"], 4) if row["avg_s"] is not None else None}

    def measured_seconds(self, wid: str, workload: str) -> Optional[float]:
        return self.worker_stats(wid, workload)["avg_s"]

    def schedule(self) -> int:
        """Places queued tasks, highest priority first. Returns how many were assigned."""
        now = self.clock()
        placed = 0
        with self._lock:
            queued = [self._task(r) for r in self.db.query(
                "SELECT * FROM tasks WHERE state='queued' ORDER BY priority DESC, created_at LIMIT 200")]
            for t in queued:
                if t["deadline"] is not None and t["deadline"] <= now:
                    with self.db.transaction() as conn:
                        self._finish(conn, t["id"], "expired", None, "the deadline passed before it could run", now)
                    continue
                choice = self.scheduler.place(t, now)
                with self.db.transaction() as conn:
                    conn.execute("INSERT INTO decisions(task_id, chosen, scores, rejected, at) VALUES (?,?,?,?,?)",
                                 (t["id"], choice.chosen, _j(choice.scores), _j(choice.rejected), now))
                    conn.execute("DELETE FROM decisions WHERE id < (SELECT COALESCE(MAX(id),0) - 5000 FROM decisions)")
                    if choice.chosen is None:
                        continue
                    conn.execute("UPDATE tasks SET state='assigned', worker_id=?, lease_until=?, attempts=attempts+1, "
                                 "updated_at=? WHERE id=? AND state='queued'",
                                 (choice.chosen, now + min(120.0, t["timeout_s"]), now, t["id"]))
                    conn.execute("INSERT INTO task_attempts(task_id, worker_id, state, assigned_at) VALUES (?,?,?,?)",
                                 (t["id"], choice.chosen, "assigned", now))
                    self._event(conn, "task.assigned", t["id"], {"worker": choice.chosen})
                placed += 1
        return placed

    def decisions(self, task_id: str) -> list[dict]:
        return [{"chosen": r["chosen"], "scores": json.loads(r["scores"]), "rejected": json.loads(r["rejected"]),
                 "at": r["at"]} for r in self.db.query("SELECT * FROM decisions WHERE task_id=? ORDER BY id",
                                                       (task_id,))]

    def claim(self, wid: str) -> Optional[dict]:
        """A worker asks for work: it gets its oldest assigned task, now accepted, with a fresh lease."""
        self.sweep()
        self.schedule()
        now = self.clock()
        with self._lock, self.db.transaction() as conn:
            w = conn.execute("SELECT status FROM workers WHERE id=?", (wid,)).fetchone()
            if w is None or w["status"] != "active":
                return None
            conn.execute("UPDATE workers SET last_seen=? WHERE id=?", (now, wid))
            row = conn.execute("SELECT * FROM tasks WHERE worker_id=? AND state='assigned' "
                               "ORDER BY priority DESC, created_at LIMIT 1", (wid,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE tasks SET state='accepted', lease_until=?, updated_at=? WHERE id=?",
                         (now + 60.0, now, row["id"]))
            self._attempt(conn, row["id"], wid, "accepted")
            self._event(conn, "task.accepted", row["id"], {"worker": wid})
            out = self._task(row)
        out["state"] = "accepted"
        return out

    def _attempt(self, conn, tid: str, wid: str, state: str, **cols) -> None:
        sets = ", ".join(["state=?"] + [f"{k}=?" for k in cols])
        conn.execute(f"UPDATE task_attempts SET {sets} WHERE id = (SELECT MAX(id) FROM task_attempts "
                     f"WHERE task_id=? AND worker_id=?)", (state, *cols.values(), tid, wid))

    def _owned(self, conn, wid: str, tid: str, states: tuple) -> Any:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            raise CollectiveError(f"no task {tid}", "not_found")
        if row["worker_id"] != wid or row["state"] not in states:
            raise CollectiveError(f"task {tid} isn't {'/'.join(states)} on this worker (it is {row['state']})",
                                  "conflict")
        return row

    def start(self, wid: str, tid: str, model: str = "") -> dict:
        now = self.clock()
        with self._lock, self.db.transaction() as conn:
            row = self._owned(conn, wid, tid, ("accepted",))
            conn.execute("UPDATE tasks SET state='running', lease_until=?, updated_at=? WHERE id=?",
                         (now + row["timeout_s"] + 30.0, now, tid))
            self._attempt(conn, tid, wid, "running", started_at=now, model=model[:200])
            self._event(conn, "task.started", tid, {"worker": wid})
        return {"state": "running", "lease_until": now + row["timeout_s"] + 30.0}

    def complete(self, wid: str, tid: str, result: Any, resources: Optional[dict] = None,
                 software: str = "") -> dict:
        now = self.clock()
        with self._lock, self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if row is not None and row["state"] == "completed" and row["worker_id"] == wid:
                return {"state": "completed", "duplicate": True}  # a repeated delivery changes nothing
            row = self._owned(conn, wid, tid, ("running",))
            schema = json.loads(row["output_schema"]) if row["output_schema"] else None
            problem = ""
            if schema is not None:
                try:
                    jsonschema.validate(result, schema)
                except jsonschema.ValidationError as exc:
                    problem = f"result didn't match the expected format: {exc.message}"[:500]
            if problem:
                self._attempt(conn, tid, wid, "failed", finished_at=now, error=problem, software=software[:80],
                              resources=_j(resources or {}))
                self._event(conn, "task.failed", tid, {"worker": wid, "error": problem})
                self._requeue(conn, tid, problem, now)
                reply = {"state": self._state(conn, tid), "error": problem}
            else:
                self._attempt(conn, tid, wid, "completed", finished_at=now, software=software[:80],
                              resources=_j(resources or {}))
                self._finish(conn, tid, "completed", result, "", now)
                reply = {"state": "completed"}
        if reply["state"] == "queued":
            self.schedule()
        return reply

    def fail(self, wid: str, tid: str, error: str, retryable: bool = True, software: str = "") -> dict:
        now = self.clock()
        with self._lock, self.db.transaction() as conn:
            self._owned(conn, wid, tid, ("accepted", "running"))
            self._attempt(conn, tid, wid, "failed", finished_at=now, error=error[:500], software=software[:80])
            self._event(conn, "task.failed", tid, {"worker": wid, "error": error[:200]})
            if retryable:
                self._requeue(conn, tid, error, now)
            else:
                self._finish(conn, tid, "failed", None, error, now)
            state = self._state(conn, tid)
        if state == "queued":
            self.schedule()
        return {"state": state}

    def cancel(self, tid: str) -> dict:
        now = self.clock()
        with self._lock, self.db.transaction() as conn:
            row = conn.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise CollectiveError(f"no task {tid}", "not_found")
            if row["state"] not in OPEN + ("created",):
                return {"state": row["state"], "changed": False}
            self._finish(conn, tid, "cancelled", None, "cancelled by request", now)
        return {"state": "cancelled", "changed": True}

    def is_cancelled(self, tid: str) -> bool:
        row = self.db.query_one("SELECT state FROM tasks WHERE id=?", (tid,))
        return row is None or row["state"] in ("cancelled", "expired")

    def _state(self, conn, tid: str) -> str:
        return conn.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()["state"]

    def _requeue(self, conn, tid: str, reason: str, now: float, count_attempt: bool = True) -> None:
        row = conn.execute("SELECT attempts, max_attempts, deadline FROM tasks WHERE id=?", (tid,)).fetchone()
        attempts = row["attempts"] if count_attempt else max(0, row["attempts"] - 1)
        if row["deadline"] is not None and row["deadline"] <= now:
            self._finish(conn, tid, "expired", None, f"deadline passed ({reason})"[:500], now)
        elif attempts >= row["max_attempts"]:
            self._finish(conn, tid, "failed", None, f"gave up after {attempts} attempt(s): {reason}"[:500], now)
        else:
            conn.execute("UPDATE tasks SET state='queued', worker_id=NULL, lease_until=NULL, attempts=?, error=?, "
                         "updated_at=? WHERE id=?", (attempts, reason[:500], now, tid))

    def _finish(self, conn, tid: str, state: str, result: Any, error: str, now: float) -> None:
        conn.execute("UPDATE tasks SET state=?, result=?, error=?, lease_until=NULL, updated_at=? WHERE id=?",
                     (state, _j(result) if result is not None else None, error[:500], now, tid))
        conn.execute("UPDATE task_attempts SET state=?, finished_at=COALESCE(finished_at, ?) WHERE task_id=? AND "
                     "state IN ('assigned','accepted','running')", ("expired" if state == "expired" else
                                                                    "cancelled" if state == "cancelled" else
                                                                    "failed" if state == "failed" else state, now, tid))
        self._event(conn, f"task.{state}", tid, {"error": error[:200]} if error else {})

    def sweep(self) -> dict:
        """Leases that ran out (a worker vanished or hung) go back to the queue; overdue tasks expire."""
        now = self.clock()
        requeued = expired = 0
        with self._lock, self.db.transaction() as conn:
            for row in conn.execute("SELECT id, worker_id FROM tasks WHERE state IN ('assigned','accepted','running') "
                                    "AND lease_until < ?", (now,)).fetchall():
                conn.execute("UPDATE task_attempts SET state='expired', finished_at=?, error='lease ran out' WHERE "
                             "id = (SELECT MAX(id) FROM task_attempts WHERE task_id=?)", (now, row["id"]))
                self._requeue(conn, row["id"], "the worker stopped answering", now)
                requeued += 1
            for row in conn.execute("SELECT id FROM tasks WHERE state IN ('queued','assigned','accepted') AND "
                                    "deadline IS NOT NULL AND deadline <= ?", (now,)).fetchall():
                self._finish(conn, row["id"], "expired", None, "the deadline passed before it could run", now)
                expired += 1
        return {"requeued": requeued, "expired": expired}

    def recover(self) -> dict:
        out = self.sweep()
        if out["requeued"] or out["expired"]:
            log.info("collective recovered: %s", out)
        return out

    def status(self) -> dict:
        counts = {r["state"]: r["n"] for r in self.db.query("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state")}
        workers = self.workers()
        return {"workers": len([w for w in workers if w["status"] != "revoked"]),
                "healthy_workers": len([w for w in workers if w["healthy"]]),
                "paused_workers": len([w for w in workers if w["status"] == "paused"]),
                "tasks": {s: counts.get(s, 0) for s in STATES},
                "workloads": {k: v["class"] for k, v in WORKLOADS.items()},
                "scheduler": self.scheduler.weights.to_dict()}


def _memory_offer(profile: dict, limits: ContributionLimits) -> float:
    total = (profile.get("memory") or {}).get("total_gb")
    return round(min(limits.max_memory_gb, total) if total else limits.max_memory_gb, 2)
