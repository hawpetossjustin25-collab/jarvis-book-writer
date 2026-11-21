"""Jarvis's brain: Gemini and LFM3 (on this computer) behind one generate(), with an automatic switch.

The author picks which one comes first. When it fails (offline, quota, overload, not running...) and the
automatic switch is on, the same request goes to the other one, and the failed one is skipped for a cooldown.
After the cooldown it is tried first again, so the brain switches back by itself once it works.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from typing import Callable, Optional

from jarvis_books.llm.errors import LLMError
from jarvis_books.llm.local import LABEL
from jarvis_books.llm.models import LLMRequest, LLMResponse, RuntimeConfig

log = logging.getLogger(__name__)

GEMINI, LOCAL = "gemini", "local"
NAMES = {GEMINI: "Gemini", LOCAL: LABEL}
# Our own bad request or a blocked answer is not the brain's fault; everything else may switch.
NO_SWITCH = ("invalid",)


class Brain:
    def __init__(self, gemini, local, settings: Callable[[], dict], clock: Callable[[], float] = time.time,
                 cooldown_s: float = 120.0):
        self.gemini = gemini  # AdaptiveLLMEngine or None (no key)
        self.local = local  # LocalEngine
        self.settings = settings
        self.clock = clock
        self.cooldown_s = cooldown_s
        self.current: Optional[str] = None  # who answered last
        self._down: dict[str, tuple[float, str]] = {}  # brain -> (retry after, why)
        self._listeners: list[Callable[[str, str, str], None]] = []
        self._lock = threading.Lock()

    # ---- choice ----------------------------------------------------------------------
    def primary(self) -> str:
        return LOCAL if self.settings().get("brain") == LOCAL else GEMINI

    def automatic(self) -> bool:
        return self.settings().get("brain_auto", "1") == "1"

    def subscribe(self, fn: Callable[[str, str, str], None]) -> None:
        """fn(new brain, old brain, reason) whenever a different brain starts answering."""
        self._listeners.append(fn)

    def order(self) -> list[str]:
        first = self.primary()
        other = LOCAL if first == GEMINI else GEMINI
        if not self.automatic():
            return [first]
        now = self.clock()
        down = [b for b in (first, other) if self._down.get(b, (0, ""))[0] > now]
        return [b for b in (first, other) if b not in down] + down  # resting brains are the last resort

    def _engine(self, which: str):
        engine = self.gemini if which == GEMINI else self.local
        if engine is None:
            raise LLMError("no Gemini key is set", kind="unavailable")
        return engine

    def ready(self) -> bool:
        gemini_ok = self.gemini is not None and getattr(self.gemini, "_config", None) is not None
        local_ok = self.local is not None and self.local.available
        return gemini_ok or local_ok if self.automatic() else (gemini_ok if self.primary() == GEMINI else local_ok)

    # ---- the engine API -------------------------------------------------------------------
    def generate(self, request: LLMRequest) -> LLMResponse:
        reasons = []
        for which in self.order():
            try:
                resp = self._engine(which).generate(self._for(which, request))
            except LLMError as exc:
                if exc.kind in NO_SWITCH:
                    raise
                reasons.append(f"{NAMES[which]}: {exc}")
                with self._lock:
                    self._down[which] = (self.clock() + self.cooldown_s, str(exc))
                log.warning("%s failed for %s (%s)", NAMES[which], request.role, exc)
                continue
            with self._lock:
                self._down.pop(which, None)
                old, self.current = self.current, which
            if old is None and which != self.primary():
                old = self.primary()  # the very first answer already came from the backup
            if old is not None and old != which:
                why = reasons[-1] if reasons else f"{NAMES[which]} is working again"
                for fn in list(self._listeners):
                    try:
                        fn(which, old, why)
                    except Exception:
                        log.exception("brain switch listener failed")
            return resp
        raise LLMError("Neither brain could answer. " + " | ".join(reasons), kind="unavailable")

    @staticmethod
    def _for(which: str, request: LLMRequest) -> LLMRequest:
        """LFM3 reads a small context, so it gets the shorter tool list when the caller gave one."""
        if which == LOCAL and request.local_tools is not None:
            return replace(request, tools=request.local_tools)
        return request

    def get_runtime_configuration(self) -> RuntimeConfig:
        last: Optional[Exception] = None
        for which in self.order():
            try:
                return self._engine(which).get_runtime_configuration()
            except LLMError as exc:
                last = exc
        raise last or LLMError("no brain is ready", kind="unavailable")

    def status(self) -> dict:
        now = self.clock()
        return {"first_choice": NAMES[self.primary()], "automatic_switch": self.automatic(),
                "answering_now": NAMES[self.current] if self.current else None,
                "gemini": "no key" if self.gemini is None else (
                    "resting: " + self._down[GEMINI][1] if self._down.get(GEMINI, (0, ""))[0] > now else "ready"),
                "lfm3": self.local.status() if self.local is not None else None}

    def __getattr__(self, name: str):
        # embed, tts, live_model, profile... are Gemini-only and go straight to it.
        gemini = self.__dict__.get("gemini")
        if gemini is None:
            raise LLMError(f"{name} needs Gemini (no key is set)", kind="unavailable")
        return getattr(gemini, name)
