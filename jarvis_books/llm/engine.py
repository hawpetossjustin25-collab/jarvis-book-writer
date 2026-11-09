"""AdaptiveLLMEngine: the single path every Gemini call in the app goes through.

Request path: caller -> engine.generate -> route by role -> rate/quota gate -> fit prompt to the
model's verified budget -> Gemini -> validate (JSON schema) -> record metrics -> adaptation controller.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import replace
from typing import Any, Callable, Optional

from jarvis_books.llm import capabilities, discovery
from jarvis_books.llm.controller import AdaptationController, Proposal
from jarvis_books.llm.errors import InvalidStructuredOutput, LLMError, NoModelAvailable
from jarvis_books.llm.gemini import GeminiClient, build_payload, parse_response
from jarvis_books.llm.hardware import probe_hardware
from jarvis_books.llm.models import HardwareProfile, LLMRequest, LLMResponse, ModelProfile, RuntimeConfig
from jarvis_books.llm.monitor import RuntimeMonitor
from jarvis_books.llm.policy import ROLE_TIERS, EnginePolicy
from jarvis_books.llm.prompt import fit_sections
from jarvis_books.llm.ratelimit import RateLimiter
from jarvis_books.llm.schema import parse_json, validate
from jarvis_books.llm.sizing import compile_config, estimate_tokens, validate_config
from jarvis_books.llm.store import EngineStore

log = logging.getLogger(__name__)

TUNABLE = ("memory_budget_tokens", "history_turns", "words_per_call", "scene_words", "concurrency",
           "tts_workers", "embed_batch")


class AdaptiveLLMEngine:
    def __init__(self, client: GeminiClient, store: EngineStore, policy: Optional[EnginePolicy] = None,
                 hardware_probe: Callable[[], HardwareProfile] = probe_hardware,
                 sleep: Callable[[float], None] = time.sleep, evaluate_every: int = 5,
                 clock: Callable[[], float] = time.time):
        self.client = client
        self.store = store
        self.policy = policy or EnginePolicy()
        self.hardware_probe = hardware_probe
        self.sleep = sleep
        self.limiter = RateLimiter(store, self.policy, clock=clock, sleep=sleep)
        self.monitor = RuntimeMonitor(store)
        self.controller = AdaptationController(self.monitor, self.policy)
        self.evaluate_every = max(1, evaluate_every)
        self._lock = threading.RLock()
        self._config: Optional[RuntimeConfig] = None
        self._profiles: dict[str, ModelProfile] = {}
        self._hardware: Optional[HardwareProfile] = None
        self._pending: Optional[tuple[int, int, dict]] = None  # (new version, parent version, baseline)
        self._since_eval = 0
        self._shutdown = False

    # ---- lifecycle ------------------------------------------------------------
    def initialize(self, probe: bool = True) -> RuntimeConfig:
        """Discover, profile, size, validate and activate."""
        self._hardware = self.hardware_probe()
        self.store.save_hardware(self._hardware)
        try:
            raw = self.client.list_models()
            profiles = discovery.discover(raw)
        except LLMError as exc:
            if exc.kind == "auth":
                raise
            cached = self.store.model_profiles()
            if not cached:
                raise
            log.warning("model discovery failed (%s); using cached profiles", exc.kind)
            profiles = cached
            for p in profiles:
                p.limitations.append("profile from cache (discovery failed)")
        previous = {p.model_id: p for p in self.store.model_profiles()}
        for p in profiles:
            old = previous.get(p.model_id)
            if old and old.version == p.version:  # carry verified capabilities forward
                p.supports_json_mode, p.supports_tools, p.available = old.supports_json_mode, old.supports_tools, old.available
                p.confidence = max(p.confidence, old.confidence)
                p.metadata_source = old.metadata_source
        if probe:
            for p in self._probe_targets(profiles):
                capabilities.detect(self.client, self.store, p, acquire=lambda m: self.limiter.acquire(m, 30))
        for p in profiles:
            self.store.save_model_profile(p)
        with self._lock:
            self._profiles = {p.model_id: p for p in profiles}
        changed = set(previous) != set(self._profiles) or any(
            previous[m].version != self._profiles[m].version for m in set(previous) & set(self._profiles))
        active = self.store.active_config()
        reason = "model list changed" if (active and changed) else ("initial sizing" if not active else "startup re-sizing")
        return self._activate(reason, overrides=self._tuned_values(active) if active and not changed else None)

    def refine(self) -> bool:
        """Verify capabilities of the top models in the background (models probed in parallel) and
        re-size once results are in. Lets the app start instantly with conservative routing."""
        with self._lock:
            targets = self._probe_targets(list(self._profiles.values()))
        before = {p.model_id: (p.supports_json_mode, p.supports_tools, p.available) for p in targets}

        def probe(p: ModelProfile) -> None:
            if not self._shutdown:
                capabilities.detect(self.client, self.store, p, acquire=lambda m: self.limiter.acquire(m, 30))
        try:
            # Daemon threads: a slow probe must never keep the app from exiting.
            threads = [threading.Thread(target=probe, args=(p,), daemon=True) for p in targets]
            for t in threads:
                t.start()
            for t in threads:
                while t.is_alive() and not self._shutdown:
                    t.join(timeout=0.5)
            if self._shutdown:
                return False
            changed = any(before[p.model_id] != (p.supports_json_mode, p.supports_tools, p.available) for p in targets)
            for p in targets:
                self.store.save_model_profile(p)
            if changed:
                self._activate("capabilities verified", overrides=self._tuned_values(self._config))
            return changed
        except Exception as exc:
            if not self._shutdown:
                log.warning("background capability check failed: %s", exc)
            return False

    def _probe_targets(self, profiles: list[ModelProfile]) -> list[ModelProfile]:
        targets: list[ModelProfile] = []
        for tier in ("flash-lite", "flash"):
            tiered = [p for p in discovery.text_candidates(profiles) if p.tier == tier]
            targets.extend(tiered[:2])
        for role, model in self.policy.pinned_models.items():
            match = next((p for p in profiles if p.model_id == model), None)
            if match and match not in targets:
                targets.insert(0, match)
        return targets[: self.policy.probe_model_limit]

    def _tuned_values(self, config: Optional[RuntimeConfig]) -> Optional[dict]:
        if not config:
            return None
        return {k: getattr(config, k) for k in TUNABLE}

    def _activate(self, reason: str, overrides: Optional[dict] = None, parent: Optional[int] = None) -> RuntimeConfig:
        with self._lock:
            profiles = list(self._profiles.values())
            try:
                config = compile_config(profiles, self._hardware, self.policy, self.limiter.limits, reason=reason,
                                        overrides=overrides)
            except Exception:
                if overrides:  # a stale tuned value must not block startup
                    config = compile_config(profiles, self._hardware, self.policy, self.limiter.limits, reason=reason)
                else:
                    raise
            current = self._config.version if self._config else None
            version = self.store.save_config(config, reason, parent if parent is not None else current)
            self._config = replace(config, version=version)
            self.store.event("configured", {"version": version, "reason": reason,
                                            "writer": list(config.routes.get("writer", ()))[:3],
                                            "words_per_call": config.words_per_call,
                                            "memory_budget_tokens": config.memory_budget_tokens})
            log.info("activated config v%d (%s)", version, reason)
            return self._config

    def shutdown(self) -> None:
        self._shutdown = True
        self.store.close()

    # ---- introspection ----------------------------------------------------------
    def get_runtime_configuration(self) -> RuntimeConfig:
        with self._lock:
            if self._config is None:
                raise LLMError("engine not initialized", kind="unavailable")
            return self._config

    def get_active_profile(self) -> dict[str, Any]:
        config = self.get_runtime_configuration()
        return {
            "hardware": self._hardware.to_dict() if self._hardware else None,
            "models": {role: [self._profiles[m].to_dict() for m in models[:1] if m in self._profiles]
                       for role, models in config.routes.items()},
        }

    def profile(self, model_id: str) -> Optional[ModelProfile]:
        return self._profiles.get(model_id)

    def status(self) -> dict[str, Any]:
        config = self.get_runtime_configuration()
        models = {}
        for role in (*ROLE_TIERS, "embed", "tts", "live"):
            for m in config.routes.get(role, ()):
                if m not in models:
                    rpm, rpd, _ = self.limiter.limits(m)
                    models[m] = {"used_today": self.limiter.used_today(m), "daily_limit": rpd, "per_minute": rpm,
                                 "blocked": self.limiter.blocked_reason(m)}
        return {"version": config.version, "reason": config.reason,
                "routes": {r: list(v[:3]) for r, v in config.routes.items()},
                "words_per_call": config.words_per_call, "memory_budget_tokens": config.memory_budget_tokens,
                "concurrency": config.concurrency, "adaptive": config.adaptive, "quota": models,
                "stats": self.monitor.stats().describe(),
                "warnings": list(self._hardware.detection_warnings) if self._hardware else []}

    # ---- request optimization ---------------------------------------------------
    def _chars_per_token(self, model_id: str) -> float:
        stats = self.monitor.stats(model_id=model_id, limit=30)
        if stats.chars_per_token and stats.count >= 3:
            return max(2.0, min(6.0, stats.chars_per_token))
        return 4.0

    def optimize_request(self, request: LLMRequest, model_id: str, shrink: float = 1.0) -> tuple[dict, list[str], int]:
        """Build the Gemini payload for this model within its verified budget."""
        config = self.get_runtime_configuration()
        budget = config.budgets[model_id]
        cpt = self._chars_per_token(model_id)
        history = request.history[-config.history_turns:] if request.history else []
        while history and (history[0].get("role") != "user" or
                           any("functionResponse" in part for part in history[0].get("parts", []))):
            history = history[1:]  # a window must start on a plain user turn (no orphaned tool replies)
        fixed = estimate_tokens(request.system, cpt) + sum(
            estimate_tokens(str(t.get("parts", "")), cpt) for t in history)
        available = int((budget.input_budget_tokens - fixed) * shrink)
        section_budget = max(512, min(available, int((config.memory_budget_tokens +
                                                       estimate_tokens(request.instruction, cpt)) * shrink)))
        trimmed: list[str] = []
        contents = list(history)
        prompt_chars = len(request.system) + sum(len(str(t.get("parts", ""))) for t in history)
        if request.instruction or request.sections:
            fit = fit_sections(request.instruction, request.sections, section_budget, cpt)
            trimmed = fit.trimmed + [f"dropped:{d}" for d in fit.dropped]
            contents.append({"role": "user", "parts": [{"text": fit.text}]})
            prompt_chars += len(fit.text)
        if not contents:
            raise LLMError("empty request", kind="invalid")
        max_out = min(request.max_output_tokens or budget.output_budget_tokens, budget.output_budget_tokens)
        profile = self._profiles.get(model_id)
        tools = request.tools if (request.tools and (profile is None or profile.supports_tools is not False)) else []
        payload = build_payload(request.system, contents, request.json_schema, tools, max_out, request.temperature)
        return payload, trimmed, prompt_chars

    # ---- generation --------------------------------------------------------------
    def generate(self, request: LLMRequest) -> LLMResponse:
        if self._shutdown:
            raise LLMError("engine is shut down", kind="unavailable")
        config = self.get_runtime_configuration()
        route = list(config.routes.get(request.role, ()))
        if not route:
            raise NoModelAvailable(request.role, ["no model configured for this role"])
        if not request.allow_fallback or not self.policy.auto_fallback:
            route = route[:1]
        reasons: list[str] = []
        for model_id in route:
            blocked = self.limiter.blocked_reason(model_id)
            if blocked:
                reasons.append(blocked)
                continue
            try:
                return self._generate_on(model_id, request, config)
            except LLMError as exc:
                if exc.kind in ("rate", "quota", "unavailable", "overload", "network", "context"):
                    reasons.append(f"{model_id}: {exc.kind}")
                    continue
                raise
        raise NoModelAvailable(request.role, reasons)

    def _generate_on(self, model_id: str, request: LLMRequest, config: RuntimeConfig) -> LLMResponse:
        shrink, attempts, repair_note = 1.0, 0, ""
        last_error: Optional[LLMError] = None
        while attempts <= config.max_retries:
            attempts += 1
            req = request
            if repair_note:
                req = replace(request, instruction=request.instruction + "\n\n" + repair_note)
            payload, trimmed, prompt_chars = self.optimize_request(req, model_id, shrink)
            if not self.limiter.acquire(model_id, max_wait_s=65):
                raise LLMError(f"{model_id} is busy (per-minute limit)", kind="rate")
            start = time.perf_counter()
            try:
                body = self.client.generate(model_id, payload)
                parsed = parse_response(body)
            except LLMError as exc:
                latency = time.perf_counter() - start
                self._record(model_id, request.role, False, latency, prompt_chars, None, exc.kind, None, trimmed, config)
                last_error = exc
                if exc.kind in ("rate", "quota", "unavailable"):
                    self.limiter.on_quota_error(model_id, exc.retry_after_s, exc.quota_id, exc.quota_value,
                                                unavailable=exc.kind == "unavailable")
                    if exc.kind == "unavailable":
                        self._mark_unavailable(model_id, str(exc))
                    raise
                if exc.kind == "context" and shrink > 0.3:
                    shrink *= 0.5
                    continue
                if exc.retryable and attempts <= config.max_retries:
                    delay = exc.retry_after_s or min(self.policy.max_backoff_s, (2 ** attempts) + random.random())
                    self.sleep(delay)
                    continue
                raise
            latency = time.perf_counter() - start
            data, json_valid = None, None
            if request.json_schema and not parsed["function_calls"]:
                try:
                    data = parse_json(parsed["text"])
                    errors = validate(data, request.json_schema)
                except ValueError as exc:
                    errors = [str(exc)]
                json_valid = not errors
                if errors:
                    self._record(model_id, request.role, True, latency, prompt_chars, parsed, None, False, trimmed, config)
                    last_error = InvalidStructuredOutput("; ".join(errors)[:300])
                    if attempts <= config.max_retries:
                        repair_note = ("Your previous answer did not match the required JSON format (" +
                                       "; ".join(errors)[:300] + "). Return only valid JSON matching the schema.")
                        continue
                    raise last_error
            self._record(model_id, request.role, True, latency, prompt_chars, parsed, None, json_valid, trimmed, config)
            return LLMResponse(text=parsed["text"], model=model_id, data=data, function_calls=parsed["function_calls"],
                               content=parsed["content"], finish_reason=parsed["finish_reason"],
                               prompt_tokens=parsed["prompt_tokens"], output_tokens=parsed["output_tokens"],
                               latency_s=latency, trimmed=trimmed, attempts=attempts)
        raise last_error or LLMError("generation failed", kind="unknown")

    def _mark_unavailable(self, model_id: str, why: str) -> None:
        with self._lock:
            p = self._profiles.get(model_id)
            if p and p.available is not False:
                p.available = False
                p.limitations.append(why[:120])
                self.store.save_model_profile(p)
                self._activate(f"{model_id} unavailable on this key", overrides=self._tuned_values(self._config))

    def _record(self, model_id: str, role: str, ok: bool, latency: float, prompt_chars: int, parsed: Optional[dict],
                error_kind: Optional[str], json_valid: Optional[bool], trimmed: list[str], config: RuntimeConfig) -> None:
        self.record_result(model_id=model_id, role=role, ok=int(ok), error_kind=error_kind, latency_s=latency,
                           prompt_tokens=parsed["prompt_tokens"] if parsed else None,
                           output_tokens=parsed["output_tokens"] if parsed else None, prompt_chars=prompt_chars,
                           finish_reason=parsed["finish_reason"] if parsed else None,
                           json_valid=None if json_valid is None else int(json_valid), trimmed=len(trimmed),
                           config_version=config.version)

    def record_result(self, **metrics: Any) -> None:
        self.store.record_metric(**metrics)
        with self._lock:
            self._since_eval += 1
            due = self._since_eval >= self.evaluate_every
            if due:
                self._since_eval = 0
        if due:
            try:
                proposal = self.evaluate_adaptation()
                if proposal:
                    self.apply_adaptation(proposal)
            except Exception:
                log.exception("adaptation evaluation failed")

    # ---- adaptation ------------------------------------------------------------------
    def evaluate_adaptation(self) -> Optional[Proposal]:
        config = self.get_runtime_configuration()
        if self._pending and self._pending[0] == config.version:
            verdict = self.controller.verify(config, self._pending[2])
            if verdict is None:
                return None
            if verdict is False:
                self.rollback(self._pending[1], reason=f"v{config.version} made results worse")
            else:
                self.store.event("adaptation_kept", {"version": config.version})
            self._pending = None
            return None
        return self.controller.evaluate(config)

    def apply_adaptation(self, proposal: Proposal) -> RuntimeConfig:
        with self._lock:
            config = self.get_runtime_configuration()
            values = {**self._tuned_values(config), **proposal.changes}
            candidate = replace(config, **values)
            validate_config(candidate, self.policy)  # never apply an unvalidated configuration
            parent = config.version
            new = self._activate(f"adaptation: {proposal.kind} ({proposal.reason})", overrides=values, parent=parent)
            self._pending = (new.version, parent, proposal.baseline)
            self.store.event("adaptation_applied", {"kind": proposal.kind, "changes": proposal.changes,
                                                    "reason": proposal.reason, "version": new.version})
            return new

    def rollback(self, version: Optional[int] = None, reason: str = "manual rollback") -> RuntimeConfig:
        with self._lock:
            current = self.get_runtime_configuration()
            if version is None:
                history = [h for h in self.store.config_history(50) if h["version"] < current.version]
                if not history:
                    raise LLMError("no earlier configuration to roll back to", kind="invalid")
                version = history[0]["version"]
            target = self.store.get_config(version)
            if target is None:
                raise LLMError(f"configuration v{version} not found", kind="invalid")
            self.store.mark_config(current.version, "rolled_back")
            new = self._activate(f"rollback to v{version}: {reason}", overrides=self._tuned_values(target),
                                 parent=current.version)
            self.store.event("rollback", {"from": current.version, "to": version, "new": new.version, "reason": reason})
            self._pending = None
            return new

    def set_adaptive(self, enabled: bool) -> RuntimeConfig:
        """Turn automatic adaptation on/off without disabling the LLM features."""
        self.policy.adaptive = enabled
        return self._activate(f"adaptation {'enabled' if enabled else 'disabled'} by user",
                              overrides=self._tuned_values(self._config))

    # ---- embeddings and speech ---------------------------------------------------------
    def embed(self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> tuple[str, list[list[float]]]:
        config = self.get_runtime_configuration()
        route = [m for m in config.routes.get("embed", ()) if not self.limiter.blocked_reason(m)]
        if not route:
            raise NoModelAvailable("embed", ["no embedding model available"])
        model = route[0]
        vectors: list[list[float]] = []
        for i in range(0, len(texts), config.embed_batch):
            if not self.limiter.acquire(model, max_wait_s=65):
                raise LLMError("embedding model busy", kind="rate")
            start = time.perf_counter()
            try:
                vectors.extend(self.client.embed(model, texts[i:i + config.embed_batch], task_type))
            except LLMError as exc:
                self.record_result(model_id=model, role="embed", ok=0, error_kind=exc.kind,
                                   latency_s=time.perf_counter() - start, config_version=config.version)
                if exc.kind in ("rate", "quota", "unavailable"):
                    self.limiter.on_quota_error(model, exc.retry_after_s, exc.quota_id, exc.quota_value,
                                                unavailable=exc.kind == "unavailable")
                raise
            self.record_result(model_id=model, role="embed", ok=1, latency_s=time.perf_counter() - start,
                               config_version=config.version)
        return model, vectors

    def tts(self, text: str, voice: str, style: str = "") -> bytes:
        config = self.get_runtime_configuration()
        reasons = []
        for model in config.routes.get("tts", ()):
            if self.limiter.blocked_reason(model):
                reasons.append(self.limiter.blocked_reason(model))
                continue
            if not self.limiter.acquire(model, max_wait_s=65):
                reasons.append(f"{model} busy")
                continue
            start = time.perf_counter()
            try:
                audio = self.client.tts(model, text, voice, style)
            except LLMError as exc:
                self.record_result(model_id=model, role="tts", ok=0, error_kind=exc.kind,
                                   latency_s=time.perf_counter() - start, config_version=config.version)
                if exc.kind in ("rate", "quota", "unavailable"):
                    self.limiter.on_quota_error(model, exc.retry_after_s, exc.quota_id, exc.quota_value,
                                                unavailable=exc.kind == "unavailable")
                    reasons.append(f"{model}: {exc.kind}")
                    continue
                raise
            self.record_result(model_id=model, role="tts", ok=1, latency_s=time.perf_counter() - start,
                               config_version=config.version)
            return audio
        raise NoModelAvailable("tts", reasons or ["no speech model available"])

    def live_model(self) -> Optional[str]:
        config = self.get_runtime_configuration()
        live = [m for m in config.routes.get("live", ()) if "translate" not in m and "transcribe" not in m]
        ranked = sorted(live, key=lambda m: ("preview" in m, "native-audio" in m, "thinking" in m,
                                             -discovery._gen_from(m)))
        return ranked[0] if ranked else None
