"""The laptop's side of Jarvis's hands on an Android phone: a command queue the phone helper long-polls.

The helper (jarvis_books/phone_agent/jarvis_agent.py, run in Termux) pairs like the phone app but as an "agent"
device, then asks /api/agent/next for work and posts /api/agent/result. Jarvis's tools call `DeviceLink.call`,
which queues one command and waits for its result. The yes/no gate runs before a tool gets here."""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from typing import Callable, Optional

from jarvis_books.errors import NotFound

ONLINE_S = 75.0  # the helper asks again at least every ~40 s while it's connected
NEXT_WAIT_S = 25.0
MAX_QUEUE = 20


class PhoneOffline(NotFound):
    pass


class PhoneFailed(NotFound):
    pass


class DeviceLink:
    def __init__(self, clock: Callable[[], float] = time.monotonic, next_wait_s: float = NEXT_WAIT_S):
        self.clock, self.next_wait_s = clock, next_wait_s
        self.info: dict = {}
        self.last_seen: Optional[float] = None
        self._ids = itertools.count(1)
        self._queue: deque = deque()
        self._results: dict[int, dict] = {}
        self._waiting: set[int] = set()
        self._cond = threading.Condition()

    @property
    def online(self) -> bool:
        return self.last_seen is not None and self.clock() - self.last_seen < ONLINE_S

    def status(self) -> dict:
        return {"connected": self.online, "phone": self.info.get("model") or None,
                "android": self.info.get("android") or None, "texts_and_calls": bool(self.info.get("termux_api")),
                "screen_control": bool(self.info.get("screen_control")),
                "seconds_since_seen": None if self.last_seen is None else round(self.clock() - self.last_seen)}

    # ---- the helper's side
    def hello(self, info: dict) -> None:
        with self._cond:
            self.info = {k: info.get(k) for k in ("agent", "model", "android", "termux_api", "battery",
                                                  "screen_control")}
            self.last_seen = self.clock()

    def next(self, wait_s: Optional[float] = None) -> Optional[dict]:
        deadline = time.monotonic() + max(0.0, min(self.next_wait_s if wait_s is None else wait_s,
                                                   self.next_wait_s))
        with self._cond:
            self.last_seen = self.clock()
            while not self._queue:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)
            self.last_seen = self.clock()
            return self._queue.popleft()

    def result(self, cid, ok: bool, result=None, error: str = "") -> bool:
        with self._cond:
            if not isinstance(cid, int) or cid not in self._waiting:
                return False
            self._waiting.discard(cid)
            self._results[cid] = {"ok": bool(ok), "result": result, "error": str(error or "")[:2000]}
            self.last_seen = self.clock()
            if isinstance(result, dict) and "screen_control" in result:
                self.info["screen_control"] = bool(result["screen_control"])
            self._cond.notify_all()
            return True

    # ---- Jarvis's side
    def call(self, action: str, args: Optional[dict] = None, timeout: float = 60.0):
        """Run one action on the phone and return its result; raises PhoneOffline or PhoneFailed."""
        with self._cond:
            if not self.online:
                raise PhoneOffline("the phone helper isn't connected; open Termux on the phone, or ask me for "
                                   "the phone setup command")
            if len(self._waiting) >= MAX_QUEUE:
                raise PhoneFailed("the phone is still busy with earlier commands")
            cid = next(self._ids)
            self._waiting.add(cid)
            self._queue.append({"id": cid, "action": action, "args": args or {}})
            self._cond.notify_all()
            end = time.monotonic() + timeout
            while cid not in self._results:
                left = end - time.monotonic()
                if left <= 0:
                    self._waiting.discard(cid)
                    self._queue = deque(c for c in self._queue if c["id"] != cid)
                    raise PhoneFailed(f"the phone didn't answer within {timeout:.0f} s")
                self._cond.wait(left)
            got = self._results.pop(cid)
        if not got["ok"]:
            raise PhoneFailed(got["error"] or "the phone couldn't do that")
        return got["result"]
