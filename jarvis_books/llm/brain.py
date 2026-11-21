"""Jarvis's brain: Gemini, Groq's free cloud models and LFM3 (on this computer) behind one generate(), with an
automatic switch.

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

GEMINI, GROQ, LOCAL = "gemini", "groq", "local"
NAMES = {GEMINI: "Gemini", GROQ: "Groq", LOCAL: LABEL}
# Who stands in, in order, when the first choice can't answer.
BACKUPS = {GEMINI: (GROQ, LOCAL), GROQ: (GEMINI, LOCAL), LOCAL: (GEMINI, GROQ)}
# Our own bad request or a blocked answer is not the brain's fault; everything else may switch.
NO_SWITCH = ("invalid",)


class Brain:
    def __init__(self, gemini, local, settings: Callable[[], dict], clock: Callable[[], float] = time.time,
                 cooldown_s: float = 120.0, groq=None):
        self.gemini = gemini  # AdaptiveLLMEngine or None (no key)
        self.local = local  # LocalEngine
        self.groq = groq  # GroqEngine (free cloud backup) or None
        self.settings = settings
        self.clock = clock
        self.cooldown_s = cooldown_s
        self.current: Optional[str] = None  # who answered last
        self._down: dict[str, tuple[float, str]] = {}  # brain -> (retry after, why)
        self._listeners: list[Callable[[str, str, str], None]] = []
        self._lock = threading.Lock()

    # ---- choice ----------------------------------------------------------------------
    def primary(self) -> str:
        choice = self.settings().get("brain")
        return choice if choice in (LOCAL, GROQ) else GEMINI

    def automatic(self) -> bool:
        return self.settings().get("brain_auto", "1") == "1"

    def subscribe(self, fn: Callable[[str, str, str], None]) -> None:
        """fn(new brain, old brain, reason) whenever a different brain starts answering."""
        self._listeners.append(fn)

    def _has_groq(self) -> bool:
        return self.groq is not None and self.groq.available

    def order(self) -> list[str]:
        first = self.primary()
        if not self.automatic():
            return [first]
        chain = [first] + [b for b in BACKUPS[first] if b != GROQ or self._has_groq()]
        now = self.clock()
        down = [b for b in chain if self._down.get(b, (0, ""))[0] > now]
        return [b for b in chain if b not in down] + down  # resting brains are the last resort

    def _engine(self, which: str):
        engine = {GEMINI: self.gemini, GROQ: self.groq, LOCAL: self.local}[which]
        if engine is None:
            raise LLMError("no Gemini key is set" if which == GEMINI else f"{NAMES[which]} is not set up",
                           kind="unavailable")
        return engine

    def ready(self) -> bool:
        ok = {GEMINI: self.gemini is not None and getattr(self.gemini, "_config", None) is not None,
              GROQ: self._has_groq(), LOCAL: self.local is not None and self.local.available}
        return any(ok.values()) if self.automatic() else ok[self.primary()]

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
        """LFM3 reads a small context and Groq's free plan takes small requests, so they get the shorter tool
        list when the caller gave one."""
        if which in (LOCAL, GROQ) and request.local_tools is not None:
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
                "lfm3": self.local.status() if self.local is not None else None,
                "groq": "no key" if not self._has_groq() else (
                    "resting: " + self._down[GROQ][1] if self._down.get(GROQ, (0, ""))[0] > now else "ready")}

    def __getattr__(self, name: str):
        # embed, tts, live_model, profile... are Gemini-only and go straight to it.
        gemini = self.__dict__.get("gemini")
        if gemini is None:
            raise LLMError(f"{name} needs Gemini (no key is set)", kind="unavailable")
        return getattr(gemini, name)
