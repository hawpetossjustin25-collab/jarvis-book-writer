"""Persistent engine state: profiles, capability results, quota usage, metrics, versioned configs."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from jarvis_books.llm.models import CapabilityResult, HardwareProfile, ModelProfile, RuntimeConfig

_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_profiles (
    model_id TEXT PRIMARY KEY, version TEXT, created_at REAL NOT NULL, verified_at REAL NOT NULL,
    confidence REAL NOT NULL, source TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hardware_profiles (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capability_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, model_id TEXT NOT NULL, version TEXT, test_id TEXT NOT NULL,
    timestamp REAL NOT NULL, success INTEGER NOT NULL, latency_s REAL NOT NULL, detail TEXT NOT NULL, confidence REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cap ON capability_results(model_id, version, test_id, timestamp);
CREATE TABLE IF NOT EXISTS quota_usage (model_id TEXT NOT NULL, day TEXT NOT NULL, requests INTEGER NOT NULL,
    PRIMARY KEY (model_id, day));
CREATE TABLE IF NOT EXISTS learned_limits (model_id TEXT NOT NULL, kind TEXT NOT NULL, value INTEGER NOT NULL,
    source TEXT NOT NULL, learned_at REAL NOT NULL, PRIMARY KEY (model_id, kind));
CREATE TABLE IF NOT EXISTS exhausted (model_id TEXT PRIMARY KEY, until REAL NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, model_id TEXT NOT NULL, role TEXT NOT NULL,
    ok INTEGER NOT NULL, error_kind TEXT, latency_s REAL NOT NULL, prompt_tokens INTEGER, output_tokens INTEGER,
    prompt_chars INTEGER, finish_reason TEXT, json_valid INTEGER, trimmed INTEGER NOT NULL DEFAULT 0,
    config_version INTEGER);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);
CREATE TABLE IF NOT EXISTS config_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, reason TEXT NOT NULL,
    status TEXT NOT NULL, data TEXT NOT NULL, parent INTEGER);
CREATE TABLE IF NOT EXISTS adaptation_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    kind TEXT NOT NULL, detail TEXT NOT NULL);
"""


class EngineStore:
    def __init__(self, path: Path | str):
        self._lock = threading.RLock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- profiles -------------------------------------------------------------
    def save_model_profile(self, profile: ModelProfile) -> None:
        now = time.time()
        self._exec(
            "INSERT INTO model_profiles (model_id, version, created_at, verified_at, confidence, source, data) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(model_id) DO UPDATE SET version=excluded.version, "
            "verified_at=excluded.verified_at, confidence=excluded.confidence, source=excluded.source, data=excluded.data",
            (profile.model_id, profile.version, now, now, profile.confidence, profile.metadata_source,
             json.dumps(profile.to_dict())))

    def model_profiles(self) -> list[ModelProfile]:
        return [ModelProfile.from_dict(json.loads(r["data"])) for r in self._all("SELECT data FROM model_profiles")]

    def save_hardware(self, hw: HardwareProfile) -> None:
        self._exec("INSERT INTO hardware_profiles (created_at, data) VALUES (?,?)", (time.time(), json.dumps(hw.to_dict())))
        self._exec("DELETE FROM hardware_profiles WHERE id NOT IN (SELECT id FROM hardware_profiles ORDER BY id DESC LIMIT 50)")

    def latest_hardware(self) -> Optional[dict]:
        rows = self._all("SELECT data FROM hardware_profiles ORDER BY id DESC LIMIT 1")
        return json.loads(rows[0]["data"]) if rows else None

    # ---- capabilities ---------------------------------------------------------
    def save_capability(self, result: CapabilityResult) -> None:
        self._exec("INSERT INTO capability_results (model_id, version, test_id, timestamp, success, latency_s, detail, "
                   "confidence) VALUES (?,?,?,?,?,?,?,?)",
                   (result.model_id, result.version, result.test_id, result.timestamp or time.time(),
                    int(result.success), result.latency_s, result.detail[:500], result.confidence))

    def capability(self, model_id: str, version: Optional[str], test_id: str, max_age_s: float) -> Optional[CapabilityResult]:
        rows = self._all("SELECT * FROM capability_results WHERE model_id=? AND version IS ? AND test_id=? "
                         "AND timestamp>=? ORDER BY id DESC LIMIT 1",
                         (model_id, version, test_id, time.time() - max_age_s))
        if not rows:
            return None
        r = rows[0]
        return CapabilityResult(r["test_id"], r["model_id"], r["version"], bool(r["success"]), r["latency_s"],
                                r["detail"], r["timestamp"], r["confidence"])

    # ---- quota ----------------------------------------------------------------
    def add_request(self, model_id: str, day: str) -> None:
        self._exec("INSERT INTO quota_usage (model_id, day, requests) VALUES (?,?,1) "
                   "ON CONFLICT(model_id, day) DO UPDATE SET requests=requests+1", (model_id, day))

    def requests_on(self, model_id: str, day: str) -> int:
        rows = self._all("SELECT requests FROM quota_usage WHERE model_id=? AND day=?", (model_id, day))
        return int(rows[0]["requests"]) if rows else 0

    def set_learned_limit(self, model_id: str, kind: str, value: int, source: str) -> None:
        self._exec("INSERT INTO learned_limits (model_id, kind, value, source, learned_at) VALUES (?,?,?,?,?) "
                   "ON CONFLICT(model_id, kind) DO UPDATE SET value=excluded.value, source=excluded.source, "
                   "learned_at=excluded.learned_at", (model_id, kind, int(value), source, time.time()))

    def learned_limit(self, model_id: str, kind: str) -> Optional[int]:
        rows = self._all("SELECT value FROM learned_limits WHERE model_id=? AND kind=?", (model_id, kind))
        return int(rows[0]["value"]) if rows else None

    def set_exhausted(self, model_id: str, until: float, reason: str) -> None:
        self._exec("INSERT INTO exhausted (model_id, until, reason) VALUES (?,?,?) ON CONFLICT(model_id) DO UPDATE "
                   "SET until=excluded.until, reason=excluded.reason", (model_id, until, reason[:300]))

    def exhausted_until(self, model_id: str) -> Optional[float]:
        rows = self._all("SELECT until FROM exhausted WHERE model_id=?", (model_id,))
        if rows and rows[0]["until"] > time.time():
            return float(rows[0]["until"])
        return None

    # ---- metrics ----------------------------------------------------------------
    def record_metric(self, **m: Any) -> None:
        cols = ", ".join(m)
        self._exec(f"INSERT INTO metrics (ts, {cols}) VALUES (?, {', '.join('?' for _ in m)})", (time.time(), *m.values()))
        self._exec("DELETE FROM metrics WHERE ts < ?", (time.time() - 30 * 86400,))

    def recent_metrics(self, since: float = 0.0, model_id: Optional[str] = None, role: Optional[str] = None,
                       limit: int = 200) -> list[dict]:
        sql, params = "SELECT * FROM metrics WHERE ts>=?", [since]
        if model_id:
            sql += " AND model_id=?"
            params.append(model_id)
        if role:
            sql += " AND role=?"
            params.append(role)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._all(sql, tuple(params))]

    # ---- versioned configuration -------------------------------------------------
    def save_config(self, config: RuntimeConfig, reason: str, parent: Optional[int]) -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("UPDATE config_versions SET status='superseded' WHERE status='active'")
                cur = self._conn.execute(
                    "INSERT INTO config_versions (created_at, reason, status, data, parent) VALUES (?,?,?,?,?)",
                    (time.time(), reason, "active", "{}", parent))
                version = int(cur.lastrowid)
                data = {**config.to_dict(), "version": version, "reason": reason}
                self._conn.execute("UPDATE config_versions SET data=? WHERE version=?", (json.dumps(data), version))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return version

    def active_config(self) -> Optional[RuntimeConfig]:
        rows = self._all("SELECT data FROM config_versions WHERE status='active' ORDER BY version DESC LIMIT 1")
        return RuntimeConfig.from_dict(json.loads(rows[0]["data"])) if rows else None

    def config_history(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT version, created_at, reason, status, parent FROM config_versions ORDER BY version DESC LIMIT ?", (limit,))]

    def get_config(self, version: int) -> Optional[RuntimeConfig]:
        rows = self._all("SELECT data FROM config_versions WHERE version=?", (version,))
        return RuntimeConfig.from_dict(json.loads(rows[0]["data"])) if rows else None

    def mark_config(self, version: int, status: str) -> None:
        self._exec("UPDATE config_versions SET status=? WHERE version=?", (status, version))

    def event(self, kind: str, detail: dict) -> None:
        self._exec("INSERT INTO adaptation_events (ts, kind, detail) VALUES (?,?,?)", (time.time(), kind, json.dumps(detail)))

    def events(self, limit: int = 50) -> list[dict]:
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in self._all(
            "SELECT * FROM adaptation_events ORDER BY id DESC LIMIT ?", (limit,))]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
