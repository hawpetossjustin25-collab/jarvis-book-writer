"""Where the app keeps its data.

The canonical database lives in local app data (SQLite and OneDrive sync do not mix well:
OneDrive can lock or partially upload a live database). Every saved chapter is mirrored as
plain Markdown into the OneDrive Documents folder, and the database is backed up there,
so the books are always readable and backed up off the machine.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "JarvisBooks"
MIRROR_DIR_NAME = "Jarvis Books"


def data_dir() -> Path:
    override = os.environ.get("JARVIS_BOOKS_HOME")
    if override:
        root = Path(override)
    elif sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_DIR_NAME
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "jarvis-books"
    root.mkdir(parents=True, exist_ok=True)
    return root


def mirror_dir() -> Path:
    override = os.environ.get("JARVIS_BOOKS_MIRROR")
    if override:
        root = Path(override)
    else:
        onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
        docs = Path(onedrive) / "Documents" if onedrive and Path(onedrive).exists() else Path.home() / "Documents"
        root = docs / MIRROR_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def audio_dir() -> Path:
    path = data_dir() / "audio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    return data_dir() / "library.db"


def engine_db_path() -> Path:
    return data_dir() / "adaptive_llm.db"


def log_path() -> Path:
    return data_dir() / "jarvis-books.log"
