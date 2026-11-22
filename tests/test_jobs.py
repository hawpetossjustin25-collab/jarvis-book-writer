import sqlite3
import threading

import pytest

from jarvis_books.jobs import JobManager
from jarvis_books.storage.db import Database
from jarvis_books.storage.library import Library


def test_a_job_still_reporting_after_the_app_closed_its_database_ends_quietly(tmp_path):
    db = Database(tmp_path / "library.db")
    jobs = JobManager(Library(db), workers=1)
    started, closed = threading.Event(), threading.Event()
    seen = []
    jobs.subscribe(lambda j: seen.append(j.message))

    def slow(progress, cancel):
        started.set()
        closed.wait(5)
        progress(0.5, "still waiting for a brain")
        return "done"

    job = jobs.submit("upgrade", "build", slow)
    assert started.wait(5)
    jobs.shutdown(wait=False)
    db.close()
    closed.set()
    jobs.wait(job.id, timeout=5)
    assert job.status == "done" and "still waiting for a brain" not in seen


def test_a_closed_database_while_the_app_is_running_is_still_an_error(tmp_path):
    db = Database(tmp_path / "library.db")
    jobs = JobManager(Library(db), workers=1)
    db.close()
    with pytest.raises(sqlite3.ProgrammingError):
        jobs.submit("upgrade", "build", lambda progress, cancel: None)
