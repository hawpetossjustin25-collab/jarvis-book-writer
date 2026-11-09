"""Adaptation controller: detect sustained problems, propose bounded changes, verify, roll back.

Decisions need at least policy.min_observations requests (no reaction to one noisy result), and a
change is only kept if the next window of requests is not worse than the one that triggered it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from jarvis_books.llm.models import RuntimeConfig
from jarvis_books.llm.monitor import RuntimeMonitor, WindowStats
from jarvis_books.llm.policy import EnginePolicy

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Proposal:
    kind: str
    changes: dict
    reason: str
    baseline: dict = field(default_factory=dict)


def _score(stats: WindowStats) -> float:
    """Lower is better: failures, truncations and invalid JSON all cost a retry or a bad chapter."""
    return stats.error_rate * 3 + stats.truncation_rate * 2 + stats.json_invalid_rate * 2 + \
        (min(stats.p50_latency_s or 0, 300) / 300)


class AdaptationController:
    def __init__(self, monitor: RuntimeMonitor, policy: EnginePolicy):
        self.monitor = monitor
        self.policy = policy

    def evaluate(self, config: RuntimeConfig) -> Optional[Proposal]:
        if not config.adaptive:
            return None
        stats = self.monitor.stats(config_version=config.version)
        writer_stats = self.monitor.stats(role="writer", config_version=config.version)
        n_min = self.policy.min_observations
        baseline = {"score": _score(stats), "count": stats.count}
        if stats.count >= n_min and stats.context_errors >= 2:
            new = max(self.policy.min_memory_tokens, int(config.memory_budget_tokens * 0.7))
            if new < config.memory_budget_tokens:
                return Proposal("reduce_memory", {"memory_budget_tokens": new},
                                f"{stats.context_errors} context-limit errors", baseline)
        if writer_stats.count >= n_min and writer_stats.truncation_rate >= 0.3:
            new = max(self.policy.min_words_per_call, int(config.words_per_call * 0.75))
            if new < config.words_per_call:
                return Proposal("reduce_words_per_call", {"words_per_call": new},
                                f"{writer_stats.truncation_rate:.0%} of drafts were cut off", baseline)
        if stats.count >= n_min and stats.quota_errors >= 3 and config.concurrency > 1:
            return Proposal("reduce_concurrency", {"concurrency": config.concurrency - 1},
                            f"{stats.quota_errors} rate-limit errors", baseline)
        if stats.count >= n_min and (stats.p50_latency_s or 0) > 120 and \
                config.memory_budget_tokens > self.policy.min_memory_tokens:
            new = max(self.policy.min_memory_tokens, int(config.memory_budget_tokens * 0.8))
            return Proposal("reduce_memory_latency", {"memory_budget_tokens": new},
                            f"median response {stats.p50_latency_s:.0f}s", baseline)
        # Recover headroom slowly once things are healthy.
        if writer_stats.count >= n_min * 2 and writer_stats.truncation_rate == 0 and writer_stats.error_rate == 0 \
                and config.words_per_call < self.policy.words_per_call_cap:
            new = min(self.policy.words_per_call_cap, int(config.words_per_call * 1.15))
            if new > config.words_per_call:
                return Proposal("raise_words_per_call", {"words_per_call": new},
                                "drafts finished cleanly", baseline)
        return None

    def verify(self, config: RuntimeConfig, baseline: dict) -> Optional[bool]:
        """True keep, False roll back, None not enough data yet."""
        stats = self.monitor.stats(config_version=config.version)
        if stats.count < self.policy.min_observations:
            return None
        return _score(stats) <= float(baseline.get("score", 0)) + 0.05
