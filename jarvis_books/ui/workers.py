"""Run blocking work off the UI thread and deliver results back on it."""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

log = logging.getLogger(__name__)


class _Signals(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        # Python owns the task: letting Qt auto-delete a Python-held runnable can double-free on exit.
        self.setAutoDelete(False)
        self.fn = fn
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # surfaced in the UI, never swallowed
            log.exception("background task failed")
            try:
                self.signals.failed.emit(str(exc) or type(exc).__name__)
            except RuntimeError:  # the window closed while this was running
                pass
            return
        try:
            self.signals.done.emit(result)
        except RuntimeError:
            pass


_alive: set = set()


def run_async(fn: Callable[[], Any], on_done: Optional[Callable[[Any], None]] = None,
              on_error: Optional[Callable[[str], None]] = None) -> None:
    task = _Task(fn)
    _alive.add(task)
    if on_done:
        task.signals.done.connect(on_done)
    if on_error:
        task.signals.failed.connect(on_error)
    task.signals.done.connect(lambda *_: _alive.discard(task))
    task.signals.failed.connect(lambda *_: _alive.discard(task))
    QThreadPool.globalInstance().start(task)


def wait_for_tasks(timeout_ms: int = 10000) -> bool:
    """Block until background tasks finish, so nothing touches storage after it is closed."""
    return QThreadPool.globalInstance().waitForDone(timeout_ms)


class Bridge(QObject):
    """Thread-safe fan-in for library changes and job updates."""
    library_changed = pyqtSignal(str, str)  # event, book_id
    job_updated = pyqtSignal(object)
