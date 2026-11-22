"""Starting, restarting and leaving the app process."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

INSTALL_LOCK = "installing.lock"
# An installer that died without removing its lock must not keep Jarvis closed forever.
LOCK_STALE_S = 2 * 3600


def app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def install_lock(root: Optional[Path] = None) -> Path:
    """The installer creates this beside the app folder while it replaces the app's files."""
    return (root or app_root()).parent / INSTALL_LOCK


def install_running(lock: Optional[Path] = None, now: Optional[float] = None) -> bool:
    lock = lock or install_lock()
    try:
        age = (time.time() if now is None else now) - lock.stat().st_mtime
    except OSError:
        return False
    return age < LOCK_STALE_S


def relaunch() -> subprocess.Popen:
    """Start a fresh copy of the app that outlives this one."""
    flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    return subprocess.Popen([sys.executable, "-m", "jarvis_books"], cwd=str(app_root()), creationflags=flags,
                            close_fds=True)


def exit_now(code: int) -> None:
    """Leave without waiting for background threads. Jobs save as they go and resume next start, so a build
    still waiting on a brain must not keep a closed app's process (and its frozen window) alive."""
    logging.shutdown()
    sys.stdout.flush() if sys.stdout else None
    sys.stderr.flush() if sys.stderr else None
    os._exit(code)
