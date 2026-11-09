"""Rolling runtime statistics computed from recorded request metrics."""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from typing import Optional

from jarvis_books.llm.store import EngineStore


@dataclass(frozen=True)
class WindowStats:
    count: int
    error_rate: float
    context_errors: int
    quota_errors: int
    truncation_rate: float
    json_invalid_rate: float
    p50_latency_s: Optional[float]
    output_tps: Optional[float]
    chars_per_token: Optional[float]

    def describe(self) -> str:
        lat = f"{self.p50_latency_s:.1f}s" if self.p50_latency_s is not None else "n/a"
        tps = f"{self.output_tps:.0f} tok/s" if self.output_tps else "n/a"
        return (f"{self.count} requests, median {lat}, {tps}, errors {self.error_rate:.0%}, "
                f"cut off {self.truncation_rate:.0%}, bad JSON {self.json_invalid_rate:.0%}")


def window_stats(rows: list[dict]) -> WindowStats:
    n = len(rows)
    if n == 0:
        return WindowStats(0, 0.0, 0, 0, 0.0, 0.0, None, None, None)
    ok = [r for r in rows if r["ok"]]
    errors = n - len(ok)
    json_rows = [r for r in rows if r.get("json_valid") is not None]
    latencies = [r["latency_s"] for r in ok]
    tps = [r["output_tokens"] / r["latency_s"] for r in ok if r.get("output_tokens") and r["latency_s"] > 0]
    cpt = [r["prompt_chars"] / r["prompt_tokens"] for r in ok if r.get("prompt_tokens") and r.get("prompt_chars")]
    return WindowStats(
        count=n,
        error_rate=errors / n,
        context_errors=sum(1 for r in rows if r.get("error_kind") == "context"),
        quota_errors=sum(1 for r in rows if r.get("error_kind") in ("quota", "rate")),
        truncation_rate=sum(1 for r in ok if r.get("finish_reason") == "MAX_TOKENS") / max(1, len(ok)),
        json_invalid_rate=sum(1 for r in json_rows if not r["json_valid"]) / max(1, len(json_rows)) if json_rows else 0.0,
        p50_latency_s=statistics.median(latencies) if latencies else None,
        output_tps=statistics.median(tps) if tps else None,
        chars_per_token=statistics.median(cpt) if cpt else None,
    )


class RuntimeMonitor:
    def __init__(self, store: EngineStore, window_s: float = 6 * 3600):
        self.store = store
        self.window_s = window_s

    def stats(self, role: Optional[str] = None, model_id: Optional[str] = None,
              config_version: Optional[int] = None, limit: int = 50) -> WindowStats:
        rows = self.store.recent_metrics(time.time() - self.window_s, model_id=model_id, role=role, limit=limit)
        if config_version is not None:
            rows = [r for r in rows if r.get("config_version") == config_version]
        return window_stats(rows)
