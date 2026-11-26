"""The Collective's door on the NEXUS contract: owner operations (status, tasks, join codes, limits) and the
worker protocol (enroll, heartbeat, claim, start, complete, fail, refresh). Worker calls carry the worker's own
short-lived credential in `worker_token`; it is checked on every call and never logged or returned in history."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from jarvis_books.nexus.collective import WORKLOADS, CollectiveError, Coordinator
from jarvis_books.nexus.contracts import ContractError, NexusRequest, NexusResponse, failure, success, \
    validate_payload
from jarvis_books.nexus.resources import ContributionLimits, ResourceProvider, available_for_contribution
from jarvis_books.nexus.worker import LocalLink, Worker, digest_handler

log = logging.getLogger(__name__)
NAME = "collective"
TOKEN = {"type": "string", "minLength": 20, "maxLength": 200}
TID = {"type": "string", "pattern": "^t-[0-9a-f]{16}$"}
WID = {"type": "string", "pattern": "^w-[0-9a-f]{12}$"}
LIMITS = {"type": "object"}


def _obj(props: dict, required: tuple = ()) -> dict:
    return {"type": "object", "additionalProperties": False, "properties": props, "required": list(required)}


PAYLOADS = {
    "status": _obj({}),
    "workers": _obj({}),
    "worker": _obj({"worker_id": WID}, ("worker_id",)),
    "tasks": _obj({"state": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}),
    "task": _obj({"task_id": TID}, ("task_id",)),
    "decisions": _obj({"task_id": TID}, ("task_id",)),
    "events": _obj({"after_id": {"type": "integer", "minimum": 0}, "kind": {"type": "string", "maxLength": 60},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500}}),
    "submit": _obj({"workload": {"enum": sorted(WORKLOADS)}, "payload": {"type": "object"},
                    "requirements": {"type": "object"}, "priority": {"type": "integer"},
                    "timeout_s": {"type": "number"}, "deadline": {"type": "number"},
                    "max_attempts": {"type": "integer"}, "idempotency_key": {"type": "string", "maxLength": 200},
                    "output_schema": {"type": "object"}}, ("workload", "payload")),
    "cancel": _obj({"task_id": TID}, ("task_id",)),
    "join_code": _obj({"label": {"type": "string", "maxLength": 80},
                       "minutes": {"type": "integer", "minimum": 1, "maximum": 1440}}),
    "limits": _obj({"worker_id": WID, "limits": LIMITS}, ("worker_id", "limits")),
    "set_status": _obj({"worker_id": WID, "status": {"enum": ["active", "paused", "revoked"]}},
                       ("worker_id", "status")),
    # worker protocol
    "enroll": _obj({"code": {"type": "string", "minLength": 8, "maxLength": 40},
                    "name": {"type": "string", "minLength": 1, "maxLength": 80}, "profile": {"type": "object"},
                    "limits": LIMITS, "software": {"type": "string", "maxLength": 80},
                    "runtimes": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 20},
                    "security": {"type": "object"}}, ("code", "name", "profile")),
    "heartbeat": _obj({"worker_token": TOKEN, "telemetry": {"type": "object"}, "profile": {"type": "object"},
                       "runtimes": {"type": "array", "items": {"type": "string"}, "maxItems": 20}},
                      ("worker_token", "telemetry")),
    "claim": _obj({"worker_token": TOKEN}, ("worker_token",)),
    "start": _obj({"worker_token": TOKEN, "task_id": TID, "model": {"type": "string", "maxLength": 200}},
                  ("worker_token", "task_id")),
    "complete": _obj({"worker_token": TOKEN, "task_id": TID, "result": {}, "resources": {"type": "object"},
                      "software": {"type": "string", "maxLength": 80}}, ("worker_token", "task_id", "result")),
    "fail": _obj({"worker_token": TOKEN, "task_id": TID, "error": {"type": "string", "maxLength": 2000},
                  "retryable": {"type": "boolean"}, "software": {"type": "string", "maxLength": 80}},
                 ("worker_token", "task_id", "error")),
    "refresh": _obj({"worker_token": TOKEN}, ("worker_token",)),
}
READS = {"status", "workers", "worker", "tasks", "task", "decisions", "events"}
WORKER_OPS = {"enroll", "heartbeat", "claim", "start", "complete", "fail", "refresh"}
OWNER_ONLY = {"join_code", "limits", "set_status"}


class CollectiveOS:
    """The coordinator plus this computer's own worker, which joins the Collective like any other worker."""

    def __init__(self, home, os1=None, provider: Optional[ResourceProvider] = None,
                 ollama=None, clock=time.time, limits: Optional[dict] = None, poll_s: float = 5.0):
        self.coordinator = Coordinator(home, clock=clock)
        self.provider = provider or ResourceProvider(home)
        self.os1, self.ollama = os1, ollama
        self.poll_s = poll_s
        self.worker: Optional[Worker] = None
        self._limits = ContributionLimits.from_dict(limits)
        self._lock = threading.Lock()
        self._meta = home

    # ---- this computer's worker
    def handlers(self) -> dict[str, Callable]:
        out = {"data.digest": digest_handler}
        if self.ollama is not None:
            out["llm.generate"] = self._generate
        if self.os1 is not None:
            out["search.query"] = self._search
            if getattr(self.os1.search_engine, "embedder", None) is not None:
                out["embed.text"] = self._embed
        return out

    def runtimes(self) -> list[str]:
        out = ["cpu"]
        if self.ollama is not None:
            try:
                if self.ollama.version():
                    out.append("ollama")
            except Exception:
                pass
        if self.os1 is not None:
            out.append("nexus-index")
            if getattr(self.os1.search_engine, "embedder", None) is not None:
                out.append("embedder")
        return out

    def _generate(self, payload: dict, stop: threading.Event) -> dict:
        model = payload.get("model") or ""
        if not model:
            models = [m.get("name") or m.get("model") for m in self.ollama.list_models()]
            if not models:
                raise RuntimeError("no local model is installed")
            model = models[0]
        messages = ([{"role": "system", "content": payload["system"]}] if payload.get("system") else []) + \
            [{"role": "user", "content": payload["prompt"]}]
        reply = self.ollama.chat({"model": model, "messages": messages, "think": False,
                                  "options": {"num_predict": payload.get("max_tokens", 512)}})
        return {"model": model, "text": (reply.get("message") or {}).get("content", ""),
                "output_tokens": reply.get("eval_count"), "prompt_tokens": reply.get("prompt_eval_count")}

    def _search(self, payload: dict, stop: threading.Event) -> dict:
        resp = self.os1.call("search", {"query": payload["query"], "max_results": payload.get("max_results", 10)})
        if not resp.ok:
            raise RuntimeError(resp.error_message)
        return {"results": [{"url": r.get("url"), "title": r.get("title"), "score": r.get("score")}
                            for r in resp.results]}

    def _embed(self, payload: dict, stop: threading.Event) -> dict:
        embedder = self.os1.search_engine.embedder
        vectors = embedder.embed(payload["texts"])
        return {"model": embedder.model, "vectors": vectors}

    def start_local_worker(self) -> dict:
        """Enrolls this computer once (remembered in collective.db) and starts its worker loop."""
        with self._lock:
            if self.worker is not None and self.worker._thread is not None and self.worker._thread.is_alive():
                return self.worker.status()
            c = self.coordinator
            mine = [w for w in c.workers(include_revoked=False) if w["local"]]
            profile = self.provider.profile(self.ollama.version if self.ollama is not None else None,
                                            network=False)
            runtimes = self.runtimes()
            profile["available"] = available_for_contribution(profile, self._limits)
            if self.ollama is not None:
                try:
                    profile["models"] = [m.get("name") or m.get("model") for m in self.ollama.list_models()]
                except Exception:
                    profile["models"] = []
            if mine:
                wid = mine[0]["id"]
                token = c.credential_for_local(wid)
                c.heartbeat(wid, self.provider.telemetry(), profile, runtimes)
            else:
                code = c.issue_enrollment("this computer")["code"]
                wid = c.enroll(code, profile.get("network", {}).get("hostname") or "this computer", profile,
                               self._limits.to_dict(), software=Worker.SOFTWARE, local=True,
                               runtimes=runtimes)["worker_id"]
                token = c.credential_for_local(wid)
            limits = ContributionLimits.from_dict(c.worker(wid)["limits"])
            self.worker = Worker(LocalLink(c, token), self.provider, limits, self.handlers(), runtimes,
                                 poll_s=self.poll_s)
            self.worker.start()
            log.info("collective: this computer is worker %s (%s)", wid, ", ".join(runtimes))
            return self.worker.status()

    def stop_local_worker(self) -> None:
        with self._lock:
            if self.worker is not None:
                self.worker.stop()

    def close(self) -> None:
        self.stop_local_worker()
        self.coordinator.close()

    # ---- contract entry point
    def handle(self, request: NexusRequest | dict | str, role: str = "owner") -> NexusResponse:
        started = time.perf_counter()
        try:
            if isinstance(request, str):
                request = NexusRequest.from_json(request)
            elif isinstance(request, dict):
                request = NexusRequest.from_dict(request)
        except ContractError as exc:
            raw = request if isinstance(request, dict) else {}
            bogus = NexusRequest("jarvis", NAME, "invalid", {}, str(raw.get("request_id") or "invalid")[:100])
            return failure(bogus, NAME, exc.code, str(exc), started)
        if request.destination != NAME:
            return failure(request, NAME, "invalid_request", f"this is the Collective, not {request.destination}",
                           started)
        schema = PAYLOADS.get(request.operation)
        if schema is None:
            return failure(request, NAME, "unknown_operation", f"the Collective can do: {', '.join(sorted(PAYLOADS))}",
                           started)
        if role == "reader" and request.operation not in READS:
            return failure(request, NAME, "invalid_request", f"a reader may not {request.operation}", started)
        if role != "owner" and request.operation in OWNER_ONLY:
            return failure(request, NAME, "unauthorized", f"only the owner may {request.operation}", started)
        try:
            validate_payload(schema, request.payload)
            results, extra = getattr(self, "_op_" + request.operation)(request.payload)
        except CollectiveError as exc:
            code = exc.code if exc.code in ("unauthorized", "not_found", "conflict") else "invalid_request"
            return failure(request, NAME, code, str(exc), started)
        except ContractError as exc:
            return failure(request, NAME, exc.code, str(exc), started)
        except ValueError as exc:
            return failure(request, NAME, "invalid_request", str(exc), started)
        except Exception as exc:
            log.exception("collective %s failed", request.operation)
            return failure(request, NAME, "internal", f"{type(exc).__name__}: {exc}", started)
        return success(request, NAME, results, started, **extra)

    def call(self, operation: str, payload: dict, role: str = "owner") -> NexusResponse:
        return self.handle(NexusRequest("jarvis", NAME, operation, payload), role)

    # ---- owner operations
    def _op_status(self, p):
        st = self.coordinator.status()
        st["this_computer"] = self.worker.status() if self.worker is not None else {"state": "off"}
        return [], st

    def _op_workers(self, p):
        return [_public(w) for w in self.coordinator.workers()], {}

    def _op_worker(self, p):
        return [_public(self.coordinator.worker(p["worker_id"]))], {}

    def _op_tasks(self, p):
        return self.coordinator.tasks(p.get("state"), p.get("limit", 50)), {}

    def _op_task(self, p):
        return [self.coordinator.task(p["task_id"])], {}

    def _op_decisions(self, p):
        return self.coordinator.decisions(p["task_id"]), {}

    def _op_events(self, p):
        return self.coordinator.events(p.get("after_id", 0), p.get("kind"), p.get("limit", 100)), {}

    def _op_submit(self, p):
        args = {k: p[k] for k in ("requirements", "priority", "timeout_s", "deadline", "max_attempts",
                                  "idempotency_key", "output_schema") if k in p}
        return [self.coordinator.submit(p["workload"], p["payload"], **args)], {}

    def _op_cancel(self, p):
        return [self.coordinator.cancel(p["task_id"])], {}

    def _op_join_code(self, p):
        out = self.coordinator.issue_enrollment(p.get("label", ""), p.get("minutes", 15) * 60.0)
        return [out], {}

    def _op_limits(self, p):
        out = self.coordinator.set_limits(p["worker_id"], p["limits"])
        if self.worker is not None and self._is_mine(p["worker_id"]):
            self.worker.limits = ContributionLimits.from_dict(out)
        return [out], {}

    def _op_set_status(self, p):
        return [_public(self.coordinator.set_status(p["worker_id"], p["status"]))], {}

    def _is_mine(self, wid: str) -> bool:
        try:
            return self.coordinator.worker(wid)["local"]
        except CollectiveError:
            return False

    # ---- worker protocol
    def _who(self, p) -> str:
        return self.coordinator.authenticate(p["worker_token"])

    def _op_enroll(self, p):
        out = self.coordinator.enroll(p["code"], p["name"], p["profile"], p.get("limits"), p.get("software", ""),
                                      p.get("security"), local=False, runtimes=p.get("runtimes"))
        return [out], {}

    def _op_heartbeat(self, p):
        return [self.coordinator.heartbeat(self._who(p), p["telemetry"], p.get("profile"), p.get("runtimes"))], {}

    def _op_claim(self, p):
        task = self.coordinator.claim(self._who(p))
        return ([task] if task else []), {}

    def _op_start(self, p):
        return [self.coordinator.start(self._who(p), p["task_id"], p.get("model", ""))], {}

    def _op_complete(self, p):
        return [self.coordinator.complete(self._who(p), p["task_id"], p["result"], p.get("resources"),
                                          p.get("software", ""))], {}

    def _op_fail(self, p):
        return [self.coordinator.fail(self._who(p), p["task_id"], p["error"], p.get("retryable", True),
                                      p.get("software", ""))], {}

    def _op_refresh(self, p):
        return [self.coordinator.refresh(p["worker_token"])], {}


def _public(worker: dict) -> dict:
    prof = worker["profile"]
    return {k: worker[k] for k in ("id", "name", "status", "healthy", "local", "runtimes", "memory_gb", "limits",
                                   "telemetry", "software", "last_seen", "enrolled_at")} | {
        "cpu": (prof.get("cpu") or {}).get("name"), "threads": (prof.get("cpu") or {}).get("threads"),
        "memory_total_gb": (prof.get("memory") or {}).get("total_gb"),
        "gpus": [g.get("name") for g in prof.get("gpus", [])], "npus": [n.get("name") for n in prof.get("npus", [])],
        "models": prof.get("models", []), "os": prof.get("os")}
