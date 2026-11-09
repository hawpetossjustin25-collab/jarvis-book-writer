"""Background jobs (writing chapters, building audiobooks) with progress, cancellation and persistence."""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Optional

from jarvis_books.storage.library import Library

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    kind: str
    book_id: Optional[str]
    title: str
    status: str = "queued"  # queued, running, done, failed, cancelled
    progress: float = 0.0
    message: str = ""
    error: Optional[str] = None
    result: Any = None
    created_at: float = 0.0


JobListener = Callable[[Job], None]


class JobManager:
    def __init__(self, library: Library, workers: int = 2):
        self.library = library
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="jarvis-job")
        self._jobs: dict[str, Job] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._futures: dict[str, Future] = {}
        self._listeners: list[JobListener] = []
        self._lock = threading.Lock()
        # Jobs that were running when the app closed are marked interrupted (their saved work is kept).
        library.db.execute("UPDATE jobs SET status='interrupted', updated_at=? WHERE status IN ('queued','running')",
                           (time.time(),))

    def subscribe(self, listener: JobListener) -> None:
        self._listeners.append(listener)

    def _update(self, job: Job, **changes: Any) -> None:
        for k, v in changes.items():
            setattr(job, k, v)
        self.library.db.execute(
            "INSERT INTO jobs (id, book_id, kind, status, progress, message, error, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, progress=excluded.progress, "
            "message=excluded.message, error=excluded.error, updated_at=excluded.updated_at",
            (job.id, job.book_id, job.kind, job.status, job.progress, job.message[:500], job.error, job.created_at, time.time()))
        for listener in list(self._listeners):
            try:
                listener(job)
            except Exception:
                log.exception("job listener failed")

    def submit(self, kind: str, title: str, fn: Callable[[Callable[[float, str], None], threading.Event], Any],
               book_id: Optional[str] = None) -> Job:
        job = Job(uuid.uuid4().hex[:12], kind, book_id, title, created_at=time.time())
        cancel = threading.Event()
        with self._lock:
            self._jobs[job.id] = job
            self._cancel[job.id] = cancel
        self._update(job, message="waiting to start")

        def run() -> None:
            if cancel.is_set():
                self._update(job, status="cancelled", message="cancelled")
                return
            self._update(job, status="running", message="starting")
            try:
                result = fn(lambda p, m: self._update(job, progress=max(0.0, min(1.0, p)), message=m), cancel)
            except Exception as exc:
                from jarvis_books.writer.pipeline import Cancelled
                if isinstance(exc, Cancelled):
                    self._update(job, status="cancelled", message="stopped (finished parts were saved)")
                else:
                    log.error("job %s failed: %s", job.id, traceback.format_exc())
                    self._update(job, status="failed", error=str(exc)[:500], message=f"failed: {exc}"[:300])
                return
            job.result = result
            self._update(job, status="done", progress=1.0, message=job.message or "done")

        self._futures[job.id] = self._pool.submit(run)
        return job

    def cancel(self, job_id: str) -> bool:
        event = self._cancel.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def active(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.status in ("queued", "running")]

    def recent(self, limit: int = 10) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: -j.created_at)[:limit]

    def wait(self, job_id: str, timeout: Optional[float] = None) -> Job:
        future = self._futures.get(job_id)
        if future:
            future.result(timeout=timeout)
        return self._jobs[job_id]

    def shutdown(self, wait: bool = False) -> None:
        for event in self._cancel.values():
            event.set()
        self._pool.shutdown(wait=wait, cancel_futures=True)
