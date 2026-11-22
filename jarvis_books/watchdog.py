"""Notices when the window stops responding, records what was stuck, and restarts the app."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


def thread_stacks() -> str:
    """Where every thread is right now, for the log."""
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():
        parts.append(f"--- thread {names.get(ident, ident)}\n" + "".join(traceback.format_stack(frame)))
    return "\n".join(parts)


class FreezeWatchdog:
    """beat() is called from the window's own thread every second. If the beats stop for dump_after_s, the
    stacks of every thread go to the log; at restart_after_s, on_stuck() runs once (it restarts the app).
    Restarts are capped (max_restarts within window_s, remembered in history) so a freeze that comes back
    on every start can't turn into a restart loop."""

    def __init__(self, on_stuck: Callable[[], None], history: Optional[Path] = None, dump_after_s: float = 20.0,
                 restart_after_s: float = 90.0, check_s: float = 2.0, max_restarts: int = 3,
                 window_s: float = 1800.0, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.on_stuck, self.history = on_stuck, history
        self.dump_after_s, self.restart_after_s, self.check_s = dump_after_s, restart_after_s, check_s
        self.max_restarts, self.window_s = max_restarts, window_s
        self.clock, self.wall = clock, wall
        self._last = self._last_check = clock()
        self._dumped = self._fired = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def beat(self) -> None:
        self._last = self.clock()

    def start(self) -> None:
        self.beat()
        self._last_check = self.clock()
        self._thread = threading.Thread(target=self._run, name="freeze-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.check_s):
            try:
                self.check()
            except Exception:
                log.exception("freeze watchdog check failed")

    def check(self) -> Optional[str]:
        """One look: None (fine), "dump" (stacks logged), "restart" (on_stuck ran) or "gave_up"."""
        if self._stop.is_set():
            return None
        now = self.clock()
        slept = now - self._last_check > max(30.0, self.check_s * 10)
        self._last_check = now
        if slept:  # this thread didn't run either: the computer was asleep, not the window stuck
            self._last = now
            return None
        stuck = now - self._last
        if stuck < self.dump_after_s:
            self._dumped = False
            return None
        result = None
        if not self._dumped:
            self._dumped = True
            log.error("the window has not responded for %.0f s; threads:\n%s", stuck, thread_stacks())
            result = "dump"
        if stuck >= self.restart_after_s and not self._fired:
            self._fired = True
            if not self._allowed():
                log.error("the window is stuck again, but Jarvis already restarted %d times in %.0f min; "
                          "not restarting", self.max_restarts, self.window_s / 60)
                return "gave_up"
            log.error("the window has not responded for %.0f s; restarting Jarvis", stuck)
            self.on_stuck()
            return "restart"
        return result

    def _allowed(self) -> bool:
        now = self.wall()
        times: list[float] = []
        if self.history is not None:
            try:
                times = [float(t) for t in json.loads(self.history.read_text(encoding="utf-8"))]
            except (OSError, ValueError, TypeError):
                times = []
        times = [t for t in times if now - t < self.window_s]
        if len(times) >= self.max_restarts:
            return False
        if self.history is not None:
            try:
                self.history.write_text(json.dumps(times + [now]), encoding="utf-8")
            except OSError:
                log.exception("could not record the restart")
        return True
