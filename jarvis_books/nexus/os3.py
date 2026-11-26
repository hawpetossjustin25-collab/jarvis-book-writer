"""OS3, the tool-calling system: a strict registry of Jarvis's tools and the pipeline that turns a request into a
validated, permission-checked, timed, audited execution.

A call only counts as a success when the tool itself returned a valid result: a tool that answered with an error,
is waiting for the author's yes, or timed out is reported as exactly that. Jarvis's own yes/no gate still decides
every Change, Outside world and System action; OS3 adds caller roles in front of it, never around it.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Callable, Optional

import jsonschema

from jarvis_books.nexus.contracts import ContractError, NexusRequest, NexusResponse, failure, success, \
    validate_payload

log = logging.getLogger(__name__)

LEVELS = ("read", "safe", "change", "outside", "system")
# What each kind of caller may run. "owner" is Justin himself; the gate still asks him about sensitive tools.
ROLES = {"reader": ("read",), "operator": ("read", "safe"), "owner": LEVELS}
STATUSES = ("success", "failed", "waiting_for_approval", "timeout", "rejected", "unavailable")
OUTPUT_ANY = {"type": "object"}
SECRET_KEY = re.compile(r"key|token|secret|password|passwd|auth|cookie|credential|pin", re.I)
_GEMINI_TYPES = {"OBJECT": "object", "STRING": "string", "INTEGER": "integer", "NUMBER": "number",
                 "BOOLEAN": "boolean", "ARRAY": "array"}


def json_schema(gemini: dict) -> dict:
    """A Gemini function-parameter schema as strict JSON Schema (unknown arguments rejected)."""
    out: dict = {}
    kind = _GEMINI_TYPES.get(str(gemini.get("type", "")).upper())
    if kind:
        out["type"] = kind
    for key in ("description", "enum", "minimum", "maximum", "minItems", "maxItems"):
        if key in gemini:
            out[key] = gemini[key]
    if kind == "object":
        out["properties"] = {k: json_schema(v) for k, v in gemini.get("properties", {}).items()}
        out["additionalProperties"] = False
        if gemini.get("required"):
            out["required"] = list(gemini["required"])
    if kind == "array" and "items" in gemini:
        out["items"] = json_schema(gemini["items"])
    return out


def redact(value):
    if isinstance(value, dict):
        return {k: "***" if SECRET_KEY.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "..."
    return value


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], dict] = field(repr=False)
    level: str = "system"
    output_schema: dict = field(default_factory=lambda: dict(OUTPUT_ANY))
    timeout_s: float = 120.0
    retries: int = 0
    version: str = "1"
    namespace: str = "jarvis"
    available: Callable[[], bool] = field(default=lambda: True, repr=False)

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.name):
            raise ValueError(f"bad tool name {self.name!r}")
        if self.level not in LEVELS:
            raise ValueError(f"{self.name}: unknown permission level {self.level!r}")
        if self.timeout_s <= 0 or self.retries < 0:
            raise ValueError(f"{self.name}: timeout must be positive and retries not negative")
        for schema in (self.input_schema, self.output_schema):
            jsonschema.Draft7Validator.check_schema(schema)
        if self.input_schema.get("type") != "object":
            raise ValueError(f"{self.name}: arguments must be an object schema")

    @property
    def tool_id(self) -> str:
        return f"{self.namespace}.{self.name}@{self.version}"

    def is_available(self) -> bool:
        try:
            return bool(self.available())
        except Exception:
            return False

    def describe(self) -> dict:
        return {"tool_id": self.tool_id, "name": self.name, "description": self.description,
                "input_schema": self.input_schema, "output_schema": self.output_schema,
                "required_permission": self.level, "timeout_s": self.timeout_s, "retries": self.retries,
                "version": self.version, "available": self.is_available()}


@dataclass
class Execution:
    tool_id: str
    status: str
    result: Optional[dict]
    error: str
    attempts: int
    elapsed_ms: float

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict:
        return {"tool_id": self.tool_id, "status": self.status, "result": self.result, "error": self.error,
                "attempts": self.attempts, "elapsed_ms": round(self.elapsed_ms, 3)}


class ToolRegistry:
    """Every tool once, by name; registering a tool records it (with its schema) in nexus.db."""

    def __init__(self, db=None, clock=time.time):
        self.db, self.clock = db, clock
        self._tools: dict[str, ToolSpec] = {}
        self._lock = threading.Lock()

    def register(self, spec: ToolSpec, replace: bool = False) -> ToolSpec:
        with self._lock:
            if spec.name in self._tools and not replace:
                raise ValueError(f"tool {spec.name!r} is already registered")
            self._tools[spec.name] = spec
        if self.db is not None:
            self.db.execute("INSERT INTO os3_tools(tool_id, name, version, level, description, input_schema, "
                            "timeout_s, retries, registered_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(tool_id) "
                            "DO UPDATE SET level=excluded.level, description=excluded.description, "
                            "input_schema=excluded.input_schema, timeout_s=excluded.timeout_s, "
                            "retries=excluded.retries",
                            (spec.tool_id, spec.name, spec.version, spec.level, spec.description[:2000],
                             json.dumps(spec.input_schema, sort_keys=True), spec.timeout_s, spec.retries,
                             self.clock()))
        return spec

    def get(self, name: str) -> Optional[ToolSpec]:
        name = name.split("@")[0].split(".")[-1]
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def discover(self, query: str = "", limit: int = 20, levels: tuple = LEVELS) -> list[ToolSpec]:
        """Tools ranked by how well their name and description match the words of `query`."""
        words = set(re.findall(r"[a-z0-9]{3,}", (query or "").lower()))
        scored = []
        for spec in self._tools.values():
            if spec.level not in levels:
                continue
            name_words = set(spec.name.split("_"))
            text = f"{spec.name} {spec.description}".lower()
            score = sum(3 if w in name_words else 1 for w in words if w in text) if words else 0
            if score or not words:
                scored.append((score, spec.name, spec))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [s for _, _, s in scored[:limit]]


class OS3:
    """Validates, permission-checks, runs (with timeout and retries), checks results and audits every call."""

    def __init__(self, registry: ToolRegistry, selector: Optional[Callable[[str], Optional[dict]]] = None,
                 clock=time.time, workers: int = 4):
        self.registry = registry
        self.db = registry.db
        self.selector = selector
        self.clock = clock
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nexus-os3")

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ---- the pipeline
    def validate(self, name: str, args: dict) -> tuple[Optional[ToolSpec], str]:
        spec = self.registry.get(name)
        if spec is None:
            return None, f"no tool named {name!r}"
        try:
            jsonschema.validate(args, spec.input_schema)
        except jsonschema.ValidationError as exc:
            where = "/".join(str(p) for p in exc.absolute_path) or "arguments"
            return spec, f"{where}: {exc.message}"
        return spec, ""

    def execute(self, name: str, args: Optional[dict] = None, role: str = "owner", caller: str = "jarvis",
                request_id: str = "", correlation_id: str = "") -> Execution:
        args = dict(args or {})
        started, attempts = time.perf_counter(), 0
        spec, problem = self.validate(name, args)
        tool_id = spec.tool_id if spec else f"unknown.{name}"
        if spec is None or problem:
            out = Execution(tool_id, "rejected", None, problem, 0, 0.0)
        elif spec.level not in ROLES.get(role, ()):
            out = Execution(tool_id, "rejected", None,
                            f"a {role!r} caller may not run {spec.level}-level tools like {spec.name}", 0, 0.0)
        elif not spec.is_available():
            out = Execution(tool_id, "unavailable", None, f"{spec.name} isn't available right now", 0, 0.0)
        else:
            status, result, error = "failed", None, ""
            while attempts <= spec.retries:
                attempts += 1
                status, result, error = self._run_once(spec, args)
                if status not in ("timeout", "failed") or not self._retryable(spec, status, result):
                    break
                time.sleep(min(0.2 * attempts, 2.0))
            out = Execution(tool_id, status, result, error, attempts, (time.perf_counter() - started) * 1000)
        self._audit(out, args, role, caller, request_id, correlation_id)
        return out

    def _run_once(self, spec: ToolSpec, args: dict) -> tuple[str, Optional[dict], str]:
        future = self._pool.submit(spec.handler, dict(args))
        try:
            result = future.result(timeout=spec.timeout_s)
        except FutureTimeout:
            future.cancel()
            return "timeout", None, f"{spec.name} took longer than {spec.timeout_s:g} s"
        except Exception as exc:
            log.exception("os3 tool %s raised", spec.name)
            return "failed", None, f"{type(exc).__name__}: {exc}"[:1000]
        if not isinstance(result, dict):
            return "failed", None, f"{spec.name} returned {type(result).__name__}, not a result object"
        if result.get("needs_approval"):
            return "waiting_for_approval", result, "waiting for the author's yes"
        if "error" in result:
            return "failed", result, str(result["error"])[:1000]
        try:
            jsonschema.validate(result, spec.output_schema)
        except jsonschema.ValidationError as exc:
            return "failed", result, f"the result didn't match {spec.name}'s output schema: {exc.message}"
        return "success", result, ""

    @staticmethod
    def _retryable(spec: ToolSpec, status: str, result: Optional[dict]) -> bool:
        """Only lookups are retried: running a change twice could do it twice."""
        if spec.level != "read":
            return False
        return status == "timeout" or result is None

    def plan(self, text: str, limit: int = 5, role: str = "owner") -> dict:
        """Tool selection and argument generation for a request, validated but not run."""
        levels = ROLES.get(role, ())
        picked = None
        if self.selector is not None:
            try:
                picked = self.selector(text)
            except Exception:
                log.exception("os3 selector failed")
        candidates = [s.name for s in self.registry.discover(text, limit, levels)]
        if picked:
            spec, problem = self.validate(picked["name"], picked.get("args", {}))
            allowed = spec is not None and spec.level in levels
            return {"tool": picked["name"], "args": picked.get("args", {}),
                    "confidence": round(float(picked.get("confidence", 0.0)), 3), "valid": not problem and allowed,
                    "problem": problem or ("" if allowed else "not allowed for this caller"),
                    "needs_yes": bool(spec and spec.level in ("change", "outside", "system")),
                    "candidates": candidates}
        return {"tool": None, "args": {}, "confidence": 0.0, "valid": False,
                "problem": "no confident pick; choose from the candidates", "needs_yes": False,
                "candidates": candidates}

    def _audit(self, out: Execution, args: dict, role: str, caller: str, request_id: str, correlation_id: str):
        if self.db is None:
            return
        try:
            self.db.execute("INSERT INTO os3_executions(request_id, correlation_id, tool_id, caller, role, args, "
                            "status, error, attempts, elapsed_ms, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (request_id or "-", correlation_id or request_id or "-", out.tool_id, caller[:60], role,
                             json.dumps(redact(args), ensure_ascii=False, default=str)[:4000], out.status,
                             out.error[:1000], out.attempts, out.elapsed_ms, self.clock()))
        except Exception:
            log.exception("os3 audit write failed")

    def history(self, limit: int = 20) -> list[dict]:
        if self.db is None:
            return []
        rows = self.db.query("SELECT * FROM os3_executions ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "args": json.loads(r["args"])} for r in rows]

    # ---- NEXUS contract
    PAYLOADS = {
        "tools": {"type": "object", "additionalProperties": False, "properties": {
            "query": {"type": "string", "maxLength": 500}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}},
        "describe": {"type": "object", "required": ["name"], "additionalProperties": False,
                     "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 100}}},
        "validate": {"type": "object", "required": ["name"], "additionalProperties": False, "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 100}, "args": {"type": "object"}}},
        "plan": {"type": "object", "required": ["text"], "additionalProperties": False, "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 2000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20}}},
        "execute": {"type": "object", "required": ["name"], "additionalProperties": False, "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 100}, "args": {"type": "object"}}},
        "history": {"type": "object", "additionalProperties": False, "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 500}}},
    }

    def handle(self, request: NexusRequest | dict | str, role: str = "owner") -> NexusResponse:
        started = time.perf_counter()
        try:
            if isinstance(request, str):
                request = NexusRequest.from_json(request)
            elif isinstance(request, dict):
                request = NexusRequest.from_dict(request)
        except ContractError as exc:
            raw = request if isinstance(request, dict) else {}
            bogus = NexusRequest("jarvis", "os3", "invalid", {}, str(raw.get("request_id") or "invalid")[:100])
            return failure(bogus, "os3", exc.code, str(exc), started)
        if request.destination != "os3":
            return failure(request, "os3", "invalid_request", f"this is OS3, not {request.destination}", started)
        schema = self.PAYLOADS.get(request.operation)
        if schema is None:
            return failure(request, "os3", "unknown_operation", f"OS3 can do: {', '.join(sorted(self.PAYLOADS))}",
                           started)
        try:
            validate_payload(schema, request.payload)
            p, levels = request.payload, ROLES.get(role, ())
            if request.operation == "tools":
                found = self.registry.discover(p.get("query", ""), p.get("limit", 50), levels)
                return success(request, "os3", [s.describe() for s in found], started, registered=len(self.registry))
            if request.operation == "describe":
                spec = self.registry.get(p["name"])
                if spec is None:
                    return failure(request, "os3", "not_found", f"no tool named {p['name']!r}", started)
                return success(request, "os3", [spec.describe()], started)
            if request.operation == "validate":
                spec, problem = self.validate(p["name"], p.get("args", {}))
                if spec is None:
                    return failure(request, "os3", "not_found", problem, started)
                return success(request, "os3", [{"tool_id": spec.tool_id, "valid": not problem, "problem": problem}],
                               started)
            if request.operation == "plan":
                return success(request, "os3", [self.plan(p["text"], p.get("limit", 5), role)], started)
            if request.operation == "history":
                return success(request, "os3", self.history(p.get("limit", 20)), started)
            run = self.execute(p["name"], p.get("args", {}), role, request.source, request.request_id,
                               request.correlation_id or "")
            if run.status == "rejected" and run.tool_id.startswith("unknown."):
                return failure(request, "os3", "not_found", run.error, started)
            if run.status == "rejected":
                return failure(request, "os3", "invalid_request", run.error, started)
            if run.status == "unavailable":
                return failure(request, "os3", "unavailable", run.error, started)
            if run.status == "timeout":
                return failure(request, "os3", "timeout", run.error, started)
            # failed and waiting results are real answers from the tool, reported with their true status
            return success(request, "os3", [run.to_dict()], started, execution_status=run.status)
        except ContractError as exc:
            return failure(request, "os3", exc.code, str(exc), started)
        except Exception as exc:
            log.exception("nexus os3 %s failed", request.operation)
            return failure(request, "os3", "internal", f"{type(exc).__name__}: {exc}", started)


def registry_from_toolbox(toolbox, db=None, version: str = "1") -> ToolRegistry:
    """Jarvis's own tools as OS3 tools. Each runs through ToolBox.call, so the yes/no gate still asks first."""
    from jarvis_books.assistant.permissions import level_of
    registry = ToolRegistry(db)
    for decl in toolbox.declarations():
        name = decl["name"]
        level = level_of(name)
        available = (lambda: toolbox.nexus is not None) if name.startswith("web_index_") else (lambda: True)
        registry.register(ToolSpec(
            name=name, description=decl.get("description", ""),
            input_schema=json_schema(decl.get("parameters") or {"type": "OBJECT", "properties": {}}),
            handler=lambda args, n=name: toolbox.call(n, args, source="nexus"), level=level,
            timeout_s=60.0 if level == "read" else 300.0, retries=1 if level == "read" else 0,
            version=version, available=available))
    return registry
