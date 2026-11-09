"""Compile verified profiles and measurements into a validated runtime configuration."""
from __future__ import annotations

import math
from typing import Optional

from jarvis_books.errors import ValidationError
from jarvis_books.llm.discovery import best_of_kind, text_candidates
from jarvis_books.llm.models import HardwareProfile, ModelBudget, ModelProfile, RuntimeConfig
from jarvis_books.llm.policy import ROLE_TIERS, EnginePolicy

CONSERVATIVE_CONTEXT = 32_000
CONSERVATIVE_OUTPUT = 8_192


def model_budget(profile: ModelProfile, policy: EnginePolicy, limits: tuple[int, Optional[int], Optional[int]]) -> ModelBudget:
    rpm, rpd, tpm = limits
    verified = profile.context_window is not None
    context = profile.context_window or CONSERVATIVE_CONTEXT
    output = min(profile.max_output_tokens or CONSERVATIVE_OUTPUT, int(context * 0.5))
    output = int(output * policy.output_safety_margin)
    input_budget = int(context * (1 - policy.context_safety_margin)) - output
    if tpm:  # one request cannot use more input than the per-minute token quota
        input_budget = min(input_budget, int(tpm * 0.8))
    return ModelBudget(profile.model_id, max(1024, input_budget), max(256, output), verified, rpm, rpd, tpm)


def order_role(role: str, profiles: list[ModelProfile], policy: EnginePolicy) -> tuple[str, ...]:
    tiers = ROLE_TIERS[role]
    usable = [p for p in profiles if p.available is not False and p.tier in tiers]
    if role == "chat":
        usable = [p for p in usable if p.supports_tools is not False]
    if role in ("planner", "editor", "summarizer"):
        usable = [p for p in usable if p.supports_json_mode is not False]

    def key(p: ModelProfile):
        verified = int(bool(p.supports_json_mode)) + int(bool(p.supports_tools))
        return (tiers.index(p.tier), -verified, -(p.generation or 0), p.preview, p.model_id)

    ordered = [p.model_id for p in sorted(usable, key=key)]
    pinned = policy.pinned_models.get(role)
    if pinned and pinned in ordered:
        ordered.remove(pinned)
        ordered.insert(0, pinned)
    return tuple(ordered)


def compile_config(profiles: list[ModelProfile], hardware: Optional[HardwareProfile], policy: EnginePolicy,
                   limits_for, version: int = 0, reason: str = "initial sizing",
                   overrides: Optional[dict] = None) -> RuntimeConfig:
    text_profiles = text_candidates(profiles)
    routes = {role: order_role(role, text_profiles, policy) for role in ROLE_TIERS}
    for route, kind in (("embed", "embedding"), ("tts", "tts"), ("live", "live")):
        routes[route] = tuple(p.model_id for p in best_of_kind(profiles, kind) if p.available is not False)
    budgets = {p.model_id: model_budget(p, policy, limits_for(p.model_id)) for p in text_profiles}
    writer = routes["writer"][0] if routes["writer"] else None
    if writer:
        b = budgets[writer]
        words_per_call = int(b.output_budget_tokens / policy.tokens_per_word * 0.6)
        memory = int(b.input_budget_tokens * policy.memory_fraction)
    else:
        words_per_call, memory = policy.min_words_per_call, policy.min_memory_tokens
    words_per_call = max(policy.min_words_per_call, min(policy.words_per_call_cap, words_per_call))
    memory = max(policy.min_memory_tokens, min(policy.max_memory_tokens, memory))
    cpu = (hardware.cpu_count if hardware and hardware.cpu_count else 2)
    # Hosted generation: concurrency is bounded by request quotas, not local cores.
    writer_rpm = budgets[writer].rpm if writer else 1
    concurrency = max(1, min(policy.max_concurrency, writer_rpm // 4 or 1))
    tts_workers = max(1, min(4, cpu))
    embed_batch = 32 if (hardware and (hardware.available_ram_bytes or 0) > 2 * 1024 ** 3) else 16
    values = dict(memory_budget_tokens=memory, history_turns=policy.history_turns, words_per_call=words_per_call,
                  scene_words=policy.scene_words_default, concurrency=concurrency, tts_workers=tts_workers,
                  embed_batch=embed_batch)
    values.update(overrides or {})
    config = RuntimeConfig(version=version, routes=routes, budgets=budgets, max_retries=policy.max_retries,
                           adaptive=policy.adaptive, reason=reason, **values)
    validate_config(config, policy)
    return config


def validate_config(config: RuntimeConfig, policy: EnginePolicy) -> None:
    problems = []
    if not config.routes.get("writer"):
        problems.append("no usable writing model")
    for role, models in config.routes.items():
        for m in models:
            if role in ROLE_TIERS and m not in config.budgets:
                problems.append(f"{role} routes to {m} without a budget")
    if not (policy.min_words_per_call <= config.words_per_call <= max(policy.min_words_per_call, policy.words_per_call_cap)):
        problems.append(f"words_per_call {config.words_per_call} out of bounds")
    if not (policy.min_memory_tokens <= config.memory_budget_tokens <= policy.max_memory_tokens):
        problems.append(f"memory budget {config.memory_budget_tokens} out of bounds")
    if not (1 <= config.concurrency <= policy.max_concurrency):
        problems.append(f"concurrency {config.concurrency} out of bounds")
    if config.max_retries > policy.max_retries or config.max_retries < 0:
        problems.append("retry limit exceeds the safety limit")
    if config.history_turns < 2:
        problems.append("history_turns must be at least 2")
    for b in config.budgets.values():
        if b.input_budget_tokens + b.output_budget_tokens <= 0:
            problems.append(f"invalid budget for {b.model_id}")
    if problems:
        raise ValidationError("invalid runtime configuration: " + "; ".join(problems))


def estimate_tokens(text: str, chars_per_token: float = 4.0) -> int:
    return int(math.ceil(len(text or "") / max(1.0, chars_per_token)))
