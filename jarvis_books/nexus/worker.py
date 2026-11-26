"""NEXUS Collective worker runtime: asks the coordinator for work, checks every task and the owner's limits before
running it, runs it with a timeout and cancellation, and reports the real result.

Lifecycle: enrolled -> health monitoring -> available -> task received -> task validated -> resources checked
-> running -> result validated -> delivered. Each step is logged. `LocalLink` talks to a coordinator in the same
program (Jarvis's own laptop); any object with the same methods can carry the protocol over a network.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Callable, Optional

import jsonschema

from jarvis_books.nexus.collective import WORKLOADS, CollectiveError, Coordinator
from jarvis_books.nexus.resources import ContributionLimits, ResourceProvider

log = logging.getLogger(__name__)
Handler = Callable[[dict, threading.Event], dict]


class LocalLink:
    """The worker protocol against a coordinator in this process, authenticated like a remote worker."""

    def __init__(self, coordinator: Coordinator, token: str):
        self.c, self.token = coordinator, token

    def _wid(self) -> str:
        return self.c.authenticate(self.token)

    def heartbeat(self, telemetry: dict, profile: Optional[dict] = None, runtimes: Optional[list] = None) -> dict:
        return self.c.heartbeat(self._wid(), telemetry, profile, runtimes)

    def claim(self) -> Optional[dict]:
        return self.c.claim(self._wid())

    def start(self, tid: str, model: str = "") -> dict:
        return self.c.start(self._wid(), tid, model)

    def complete(self, tid: str, result, resources: dict, software: str) -> dict:
        return self.c.complete(self._wid(), tid, result, resources, software)

    def fail(self, tid: str, error: str, retryable: bool, software: str) -> dict:
        return self.c.fail(self._wid(), tid, error, retryable, software)

    def cancelled(self, tid: str) -> bool:
        return self.c.is_cancelled(tid)

    def refresh(self) -> None:
        self.token = self.c.refresh(self.token)["token"]


def digest_handler(payload: dict, stop: threading.Event) -> dict:
    out = []
    for item in payload["items"]:
        if stop.is_set():
            raise InterruptedError("cancelled")
        data = item.encode("utf-8")
        out.append({"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "words": len(item.split())})
    return {"digests": out}


class Worker:
    SOFTWARE = "jarvis-worker/1"

    def __init__(self, link, provider: ResourceProvider, limits: ContributionLimits,
                 handlers: dict[str, Handler], runtimes: list[str], poll_s: float = 5.0,
                 heartbeat_s: float = 30.0, clock=time.time):
        self.link, self.provider, self.limits = link, provider, limits
        self.handlers = {k: v for k, v in handlers.items() if k in WORKLOADS}
        self.runtimes, self.poll_s, self.heartbeat_s, self.clock = runtimes, poll_s, heartbeat_s, clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool = ThreadPoolExecutor(max_workers=max(1, limits.max_tasks), thread_name_prefix="nexus-task")
        self._last_beat = 0.0
        self.state = "stopped"
        self.last_refusals: list[str] = []
        self.done = self.failed = 0

    # ---- one step, testable without threads
    def beat(self, force: bool = False) -> dict:
        tel = self.provider.telemetry()
        if force or self.clock() - self._last_beat >= self.heartbeat_s:
            reply = self.link.heartbeat(tel, runtimes=self.runtimes)
            self._last_beat = self.clock()
            if reply.get("limits"):
                self.limits = ContributionLimits.from_dict(reply["limits"])
            if reply.get("status") != "active":
                self.state = reply.get("status", "paused")
        return tel

    def run_once(self) -> Optional[dict]:
        """Checks limits, takes at most one task and runs it to the end. Returns what happened, or None."""
        tel = self.beat()
        refusals = self.limits.refusals(tel)
        self.last_refusals = refusals
        if refusals:
            self.state = "waiting"
            log.info("collective worker waiting: %s", "; ".join(refusals))
            return None
        task = self.link.claim()
        if task is None:
            self.state = "available"
            return None
        return self.execute(task)

    def _reject_reason(self, task: dict) -> str:
        spec = WORKLOADS.get(task.get("workload"))
        if spec is None or task["workload"] not in self.handlers:
            return f"this worker can't run {task.get('workload')!r}"
        for key in ("id", "payload", "timeout_s", "requirements", "attempts", "max_attempts"):
            if key not in task:
                return f"task is missing {key}"
        try:
            jsonschema.validate(task["payload"], spec["payload"])
        except jsonschema.ValidationError as exc:
            return f"task input is invalid: {exc.message}"
        need = float(task["requirements"].get("memory_gb", spec["memory_gb"]))
        refusals = self.limits.refusals(self.provider.telemetry(), running=0, need_memory_gb=need)
        return "; ".join(refusals)

    def execute(self, task: dict) -> dict:
        tid = task["id"]
        problem = self._reject_reason(task)
        if problem:
            log.info("collective task %s refused: %s", tid, problem)
            self.link.fail(tid, problem, True, self.SOFTWARE)  # someone else may be able to run it
            return {"task": tid, "state": "refused", "error": problem}
        self.state = "running"
        self.link.start(tid, task["payload"].get("model", ""))
        stop = threading.Event()
        started, cpu0 = time.perf_counter(), time.process_time()
        future = self._pool.submit(self.handlers[task["workload"]], task["payload"], stop)
        deadline = time.monotonic() + float(task["timeout_s"])
        result, error = None, ""
        while True:
            try:
                result = future.result(timeout=0.5)
                break
            except FutureTimeout:
                if self.link.cancelled(tid) or self._stop.is_set():
                    stop.set()
                    error = "cancelled"
                    break
                if time.monotonic() > deadline:
                    stop.set()
                    error = f"took longer than {task['timeout_s']:g} s"
                    break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
                break
        resources = {"seconds": round(time.perf_counter() - started, 3),
                     "cpu_seconds": round(time.process_time() - cpu0, 3)}
        self.state = "available"
        if error == "cancelled":
            log.info("collective task %s stopped (cancelled)", tid)
            if not self.link.cancelled(tid):
                self.link.fail(tid, "the worker shut down", True, self.SOFTWARE)
            return {"task": tid, "state": "cancelled"}
        if error:
            self.failed += 1
            self.link.fail(tid, error, True, self.SOFTWARE)
            return {"task": tid, "state": "failed", "error": error, **resources}
        reply = self.link.complete(tid, result, resources, self.SOFTWARE)
        if reply.get("state") == "completed":
            self.done += 1
        else:
            self.failed += 1
        return {"task": tid, "state": reply.get("state"), "error": reply.get("error", ""), **resources}

    # ---- background loop
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.state = "available"
        self._thread = threading.Thread(target=self._loop, name="nexus-worker", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        refreshed = self.clock()
        while not self._stop.is_set():
            try:
                if self.clock() - refreshed > 1800:
                    self.link.refresh()
                    refreshed = self.clock()
                did = self.run_once()
            except CollectiveError as exc:
                log.warning("collective worker: %s", exc)
                if exc.code == "unauthorized":
                    self.state = "revoked"
                    return
                did = None
            except Exception:
                log.exception("collective worker step failed")
                did = None
            if did is None:
                self._stop.wait(self.poll_s)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.state = "stopped"

    def status(self) -> dict:
        return {"state": self.state, "done": self.done, "failed": self.failed,
                "waiting_because": self.last_refusals, "limits": self.limits.to_dict(), "runtimes": self.runtimes,
                "workloads": sorted(self.handlers)}
