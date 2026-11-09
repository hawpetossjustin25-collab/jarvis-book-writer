"""Per-model request pacing (requests/minute) and daily quota tracking.

Gemini free-tier daily quotas reset at midnight Pacific time, so days are counted in that zone.
Limits learned from the API's own 429 quota errors replace the configured defaults.
"""
from __future__ import annotations

import collections
import datetime as dt
import threading
import time
from typing import Callable, Optional

from jarvis_books.llm.policy import EnginePolicy
from jarvis_books.llm.store import EngineStore

try:
    from zoneinfo import ZoneInfo
    _PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # Windows without tzdata: fixed PST offset is within an hour of correct
    _PACIFIC = dt.timezone(dt.timedelta(hours=-8))


def quota_day(ts: Optional[float] = None) -> str:
    return dt.datetime.fromtimestamp(ts if ts is not None else time.time(), _PACIFIC).strftime("%Y-%m-%d")


def next_reset(ts: Optional[float] = None) -> float:
    now = dt.datetime.fromtimestamp(ts if ts is not None else time.time(), _PACIFIC)
    tomorrow = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return tomorrow.timestamp()


class RateLimiter:
    def __init__(self, store: EngineStore, policy: EnginePolicy, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.store = store
        self.policy = policy
        self.clock = clock
        self.sleep = sleep
        self._windows: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def limits(self, model_id: str) -> tuple[int, Optional[int], Optional[int]]:
        rpm, rpd, tpm = self.policy.limits_for(model_id)
        learned_rpm = self.store.learned_limit(model_id, "rpm")
        learned_rpd = self.store.learned_limit(model_id, "rpd")
        return learned_rpm or rpm, learned_rpd if learned_rpd is not None else rpd, tpm

    def used_today(self, model_id: str) -> int:
        return self.store.requests_on(model_id, quota_day(self.clock()))

    def remaining_today(self, model_id: str) -> Optional[int]:
        _, rpd, _ = self.limits(model_id)
        if rpd is None:
            return None
        return max(0, rpd - self.used_today(model_id))

    def blocked_reason(self, model_id: str) -> Optional[str]:
        until = self.store.exhausted_until(model_id)
        if until:
            return f"{model_id} out of quota until {time.strftime('%H:%M', time.localtime(until))}"
        remaining = self.remaining_today(model_id)
        if remaining == 0:
            return f"{model_id} daily limit reached"
        return None

    def wait_time(self, model_id: str) -> float:
        rpm, _, _ = self.limits(model_id)
        now = self.clock()
        with self._lock:
            window = self._windows[model_id]
            while window and now - window[0] >= 60.0:
                window.popleft()
            if len(window) < max(1, rpm):
                return 0.0
            return max(0.0, 60.0 - (now - window[0]) + 0.05)

    def acquire(self, model_id: str, max_wait_s: float = 65.0) -> bool:
        """Reserve one request slot, waiting for the per-minute window. False if it would wait too long."""
        deadline = self.clock() + max_wait_s
        while True:
            wait = self.wait_time(model_id)
            if wait <= 0:
                with self._lock:
                    self._windows[model_id].append(self.clock())
                self.store.add_request(model_id, quota_day(self.clock()))
                return True
            if self.clock() + wait > deadline:
                return False
            self.sleep(min(wait, 5.0))

    def on_quota_error(self, model_id: str, retry_after_s: Optional[float], quota_id: Optional[str],
                       quota_value: Optional[int], unavailable: bool = False) -> None:
        now = self.clock()
        if unavailable:
            self.store.set_exhausted(model_id, now + 24 * 3600, "model not available on this API tier")
            return
        if quota_id and "PerDay" in quota_id:
            if quota_value:
                self.store.set_learned_limit(model_id, "rpd", quota_value, f"429 {quota_id}")
            self.store.set_exhausted(model_id, next_reset(now), f"daily quota {quota_id}")
        elif quota_id and "PerMinute" in quota_id:
            if quota_value and "Request" in quota_id:
                self.store.set_learned_limit(model_id, "rpm", quota_value, f"429 {quota_id}")
            self.store.set_exhausted(model_id, now + (retry_after_s or 30.0), f"minute quota {quota_id}")
        else:
            self.store.set_exhausted(model_id, now + (retry_after_s or 30.0), "rate limited")
