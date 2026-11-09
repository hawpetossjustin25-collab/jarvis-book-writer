"""Engine policy: which settings are automatic, user-controlled, or safety-protected.

Free-tier request limits are not exposed by the API. The defaults below come from Google's
published free-tier tables and are labeled "configured" - they are replaced by what the API
actually reports in 429 quota errors (learned limits are stored and preferred).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# (pattern, rpm, rpd, input tpm)
CONFIGURED_LIMITS: tuple[tuple[str, int, Optional[int], Optional[int]], ...] = (
    (r"flash-lite-tts|flash-tts|-tts", 3, 50, 10_000),
    (r"embedding", 50, 1000, 30_000),
    (r"flash-lite", 10, 500, 250_000),
    (r"pro", 2, 0, 125_000),  # paid-only on the free tier: 0 means "expect unavailable"
    (r"flash", 5, 20, 250_000),
)
DEFAULT_LIMITS = (5, 20, 250_000)

TIER_ORDER = {"pro": 3, "flash": 2, "flash-lite": 1}


@dataclass
class EnginePolicy:
    # user-controlled
    adaptive: bool = True
    auto_fallback: bool = True
    pinned_models: dict[str, str] = field(default_factory=dict)  # role -> model id
    words_per_call_cap: int = 4000
    # automatic defaults (the controller tunes the runtime values within these bounds)
    min_words_per_call: int = 600
    scene_words_default: int = 1200
    memory_fraction: float = 0.35
    min_memory_tokens: int = 2000
    max_memory_tokens: int = 60_000
    history_turns: int = 16
    max_concurrency: int = 3
    output_safety_margin: float = 0.85
    context_safety_margin: float = 0.05
    tokens_per_word: float = 1.35
    # safety-protected (not user-editable at runtime)
    max_retries: int = 3
    max_backoff_s: float = 60.0
    probe_model_limit: int = 4
    min_observations: int = 5

    def limits_for(self, model_id: str) -> tuple[int, Optional[int], Optional[int]]:
        for pattern, rpm, rpd, tpm in CONFIGURED_LIMITS:
            if re.search(pattern, model_id):
                return rpm, rpd, tpm
        return DEFAULT_LIMITS

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, path: Path) -> "EnginePolicy":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        user_keys = {"adaptive", "auto_fallback", "pinned_models", "words_per_call_cap"}
        return cls(**{k: v for k, v in data.items() if k in user_keys})

    def save(self, path: Path) -> None:
        user = {k: getattr(self, k) for k in ("adaptive", "auto_fallback", "pinned_models", "words_per_call_cap")}
        path.write_text(json.dumps(user, indent=2), encoding="utf-8")


# Which kinds of model serve each role, best tier first. The engine orders concrete models by these
# preferences, then by generation (newest first), then demotes models that failed probes or ran out of quota.
ROLE_TIERS: dict[str, tuple[str, ...]] = {
    "planner": ("flash", "flash-lite", "pro"),
    "writer": ("flash-lite", "flash", "pro"),
    "editor": ("flash-lite", "flash", "pro"),
    "summarizer": ("flash-lite", "flash"),
    "chat": ("flash-lite", "flash"),
    "probe": ("flash-lite", "flash"),
}
TEXT_ROLES = tuple(ROLE_TIERS)
