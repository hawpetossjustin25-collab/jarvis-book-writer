"""NEXUS message contracts: the versioned JSON envelope Jarvis, OS1, OS2 and OS3 use to talk to each other."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import jsonschema

VERSION = "1"
SYSTEMS = ("jarvis", "os1", "os2", "os3", "collective", "brain", "telegram")
STATUSES = ("success", "error")
ERROR_CODES = ("invalid_request", "unknown_operation", "not_found", "unsafe_url", "timeout", "unavailable",
               "internal", "unauthorized", "conflict")

_ID = {"type": "string", "minLength": 1, "maxLength": 100}
REQUEST_SCHEMA = {
    "type": "object",
    "required": ["request_id", "source", "destination", "operation", "payload"],
    "additionalProperties": False,
    "properties": {
        "version": {"const": VERSION},
        "request_id": _ID,
        "correlation_id": _ID,
        "source": {"enum": list(SYSTEMS)},
        "destination": {"enum": list(SYSTEMS)},
        "operation": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"},
        "payload": {"type": "object"},
        "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 86_400_000},
        "created_at": {"type": "number"},
    },
}
RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["request_id", "status", "source", "operation", "results", "metadata"],
    "additionalProperties": False,
    "properties": {
        "version": {"const": VERSION},
        "request_id": _ID,
        "correlation_id": _ID,
        "status": {"enum": list(STATUSES)},
        "source": {"enum": list(SYSTEMS)},
        "operation": {"type": "string"},
        "results": {"type": "array"},
        "error": {
            "type": ["object", "null"],
            "required": ["code", "message"],
            "properties": {"code": {"enum": list(ERROR_CODES)}, "message": {"type": "string"}},
        },
        "metadata": {
            "type": "object",
            "required": ["execution_time_ms", "result_count"],
            "properties": {"execution_time_ms": {"type": "number", "minimum": 0},
                           "result_count": {"type": "integer", "minimum": 0}},
        },
    },
}


class ContractError(ValueError):
    """A message doesn't match the NEXUS contract."""

    def __init__(self, message: str, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code


def _check(schema: dict, data: Any, what: str) -> None:
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path) or what
        raise ContractError(f"{where}: {exc.message}") from None


def validate_payload(schema: dict, payload: dict) -> None:
    _check(schema, payload, "payload")


@dataclass(frozen=True)
class NexusRequest:
    source: str
    destination: str
    operation: str
    payload: dict
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    correlation_id: Optional[str] = None
    timeout_ms: Optional[int] = None
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.correlation_id:
            object.__setattr__(self, "correlation_id", self.request_id)
        _check(REQUEST_SCHEMA, self._raw(), "request")

    def _raw(self) -> dict:
        raw = {"version": VERSION, "request_id": self.request_id, "source": self.source,
               "destination": self.destination, "operation": self.operation, "payload": self.payload,
               "created_at": self.created_at, "correlation_id": self.correlation_id or self.request_id}
        if self.timeout_ms is not None:
            raw["timeout_ms"] = self.timeout_ms
        return raw

    def to_dict(self) -> dict:
        return self._raw()

    def to_json(self) -> str:
        return json.dumps(self._raw(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Any) -> "NexusRequest":
        if not isinstance(data, dict):
            raise ContractError("request must be a JSON object")
        _check(REQUEST_SCHEMA, data, "request")
        return cls(source=data["source"], destination=data["destination"], operation=data["operation"],
                   payload=data["payload"], request_id=data["request_id"],
                   correlation_id=data.get("correlation_id"), timeout_ms=data.get("timeout_ms"),
                   created_at=data.get("created_at", time.time()))

    @classmethod
    def from_json(cls, text: str) -> "NexusRequest":
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise ContractError(f"request is not valid JSON ({exc.msg})") from None


@dataclass(frozen=True)
class NexusResponse:
    request_id: str
    source: str
    operation: str
    status: str
    results: list
    execution_time_ms: float
    correlation_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.correlation_id:
            object.__setattr__(self, "correlation_id", self.request_id)
        _check(RESPONSE_SCHEMA, self.to_dict(), "response")

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict:
        out = {"version": VERSION, "request_id": self.request_id,
               "correlation_id": self.correlation_id or self.request_id, "status": self.status,
               "source": self.source, "operation": self.operation, "results": self.results,
               "error": {"code": self.error_code, "message": self.error_message or ""} if self.error_code else None,
               "metadata": {**self.extra, "execution_time_ms": round(self.execution_time_ms, 3),
                            "result_count": len(self.results)}}
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @classmethod
    def from_dict(cls, data: Any) -> "NexusResponse":
        if not isinstance(data, dict):
            raise ContractError("response must be a JSON object")
        _check(RESPONSE_SCHEMA, data, "response")
        meta = dict(data["metadata"])
        elapsed = meta.pop("execution_time_ms")
        meta.pop("result_count")
        err = data.get("error") or {}
        return cls(request_id=data["request_id"], source=data["source"], operation=data["operation"],
                   status=data["status"], results=data["results"], execution_time_ms=elapsed,
                   correlation_id=data.get("correlation_id"), error_code=err.get("code"),
                   error_message=err.get("message"), extra=meta)


def success(request: NexusRequest, source: str, results: list, started: float, **extra) -> NexusResponse:
    return NexusResponse(request.request_id, source, request.operation, "success", results,
                         (time.perf_counter() - started) * 1000, request.correlation_id, extra=extra)


def failure(request: NexusRequest, source: str, code: str, message: str, started: float) -> NexusResponse:
    return NexusResponse(request.request_id, source, request.operation, "error", [],
                         (time.perf_counter() - started) * 1000, request.correlation_id, code, message[:1000])
