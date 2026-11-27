import os
import subprocess
import sys
import time
from pathlib import Path

from jarvis_books import lifecycle

ROOT = Path(__file__).resolve().parent.parent


def test_exit_now_leaves_even_while_a_job_thread_is_still_waiting():
    script = ("import threading, time\n"
              "from jarvis_books.lifecycle import exit_now\n"
              "threading.Thread(target=time.sleep, args=(60,)).start()  # not a daemon, like the job pool\n"
              "exit_now(5)\n")
    start = time.time()
    done = subprocess.run([sys.executable, "-c", script], cwd=str(ROOT), timeout=30)
    assert done.returncode == 5 and time.time() - start < 20


def test_the_install_lock_keeps_jarvis_closed_only_while_it_is_fresh(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    lock = lifecycle.install_lock(app)
    assert lock == tmp_path / "installing.lock"
    assert not lifecycle.install_running(lock)
    lock.write_text("x")
    assert lifecycle.install_running(lock)
    old = time.time() - lifecycle.LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    assert not lifecycle.install_running(lock)


def test_the_installed_app_looks_for_the_lock_beside_its_folder():
    assert lifecycle.install_lock() == ROOT.parent / "installing.lock"


def test_the_installer_holds_the_lock_while_it_replaces_files():
    text = (ROOT / "scripts" / "install_windows.ps1").read_text()
    assert "installing.lock" in text
    assert text.index("installing.lock") < text.index("Remove-Item -Recurse -Force $app")
    assert "finally" in text


def test_the_installer_records_the_commit_before_the_tests_read_it():
    # The Release Center test reads app\version.json while the suite runs, so it must exist first.
    text = (ROOT / "scripts" / "install_windows.ps1").read_text()
    first_write = text.index("'version.json'")
    assert first_write < text.index("-m pytest")
    assert "tests running" in text[:text.index("-m pytest")]
