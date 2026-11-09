"""Bounded, side-effect-free capability probes (cached per model version for a week)."""
from __future__ import annotations

import logging
import time
from typing import Optional

from jarvis_books.llm.errors import LLMError
from jarvis_books.llm.gemini import GeminiClient, build_payload, parse_response
from jarvis_books.llm.models import CapabilityResult, ModelProfile
from jarvis_books.llm.schema import parse_json, validate
from jarvis_books.llm.store import EngineStore

log = logging.getLogger(__name__)

CACHE_S = 7 * 86400
JSON_SCHEMA = {"type": "OBJECT", "properties": {"title": {"type": "STRING"}, "chapters": {"type": "INTEGER"}},
               "required": ["title", "chapters"]}
PROBE_TOOL = {"name": "lookup_character", "description": "Look up a character in the story bible by name.",
              "parameters": {"type": "OBJECT", "properties": {"name": {"type": "STRING"}}, "required": ["name"]}}


def _run(client: GeminiClient, profile: ModelProfile, test_id: str) -> CapabilityResult:
    start = time.perf_counter()
    try:
        if test_id == "json":
            body = client.generate(profile.model_id, build_payload(
                "", [{"role": "user", "parts": [{"text": "Invent a novel title and a chapter count from 10 to 40."}]}],
                JSON_SCHEMA, [], 1024, 0.2), timeout=150)
            parsed = parse_response(body)
            data = parse_json(parsed["text"])
            errors = validate(data, JSON_SCHEMA)
            ok = not errors
            detail = "valid schema output" if ok else "; ".join(errors)
        elif test_id == "tools":
            body = client.generate(profile.model_id, build_payload(
                "Use tools when they help.",
                [{"role": "user", "parts": [{"text": "What does the story bible say about the character Mara?"}]}],
                None, [PROBE_TOOL], 1024, 0.0), timeout=150)
            parsed = parse_response(body)
            calls = parsed["function_calls"]
            ok = bool(calls) and calls[0].get("name") == PROBE_TOOL["name"] and \
                str((calls[0].get("args") or {}).get("name", "")).lower() == "mara"
            detail = "correct tool call" if ok else f"tool calls: {calls!r}"[:300]
        else:
            raise ValueError(f"unknown probe {test_id}")
        return CapabilityResult(test_id, profile.model_id, profile.version, ok, time.perf_counter() - start, detail,
                                time.time(), 0.9 if ok else 0.7)
    except LLMError as exc:
        return CapabilityResult(test_id, profile.model_id, profile.version, False, time.perf_counter() - start,
                                f"{exc.kind}: {exc}", time.time(), 0.5 if exc.retryable else 0.8)
    except ValueError as exc:
        return CapabilityResult(test_id, profile.model_id, profile.version, False, time.perf_counter() - start,
                                f"invalid output: {exc}", time.time(), 0.7)


def detect(client: GeminiClient, store: EngineStore, profile: ModelProfile, acquire=None,
           force: bool = False) -> ModelProfile:
    """Apply verified capability results to the profile (using cached results when fresh)."""
    results: dict[str, Optional[CapabilityResult]] = {}
    for test_id in ("json", "tools"):
        cached = None if force else store.capability(profile.model_id, profile.version, test_id, CACHE_S)
        if cached is None:
            if acquire is not None and not acquire(profile.model_id):
                results[test_id] = None
                continue
            cached = _run(client, profile, test_id)
            if not cached.success and cached.detail.startswith(("network", "overload", "rate")):
                log.info("probe %s on %s inconclusive (%s); will retry next start", test_id, profile.model_id, cached.detail)
                results[test_id] = None
                continue
            store.save_capability(cached)
            log.info("probe %s on %s: %s (%s)", test_id, profile.model_id, cached.success, cached.detail)
        results[test_id] = cached
        if cached and not cached.success and cached.detail.startswith(("unavailable", "auth", "quota")):
            break
    json_r, tools_r = results.get("json"), results.get("tools")
    if json_r is not None:
        profile.supports_json_mode = json_r.success
        if not json_r.success and json_r.detail.startswith(("unavailable", "auth", "quota")):
            profile.available = False
            profile.limitations.append(json_r.detail[:120])
        elif json_r.success:
            profile.available = True
    if tools_r is not None:
        profile.supports_tools = tools_r.success
    verified = [r for r in (json_r, tools_r) if r is not None]
    if verified:
        profile.metadata_source = "models.list + probes"
        profile.confidence = round(sum(r.confidence for r in verified) / len(verified), 2)
    return profile
