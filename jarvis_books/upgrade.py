"""Upgrading the app's own code by voice, safely.

Gemini edits a separate copy of the app, never the running one, and may only touch the app's Python code
(no books, keys or protected safety code). The full test suite runs on that copy with a throwaway data folder
and no secrets in its environment. Only a copy that passes is offered; installing it backs up every file it
replaces, so the upgrade can be undone.
"""
from __future__ import annotations

import difflib
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from jarvis_books.errors import NotFound, ValidationError
from jarvis_books.llm.models import LLMRequest, PromptSection

log = logging.getLogger(__name__)

COPY_ITEMS = ("jarvis_books", "tests", "pyproject.toml", "README.md")
EDITABLE_DIRS = ("jarvis_books/", "tests/")
# The safety code itself: the yes/no gate, the approval card, this upgrader, key storage and their tests.
PROTECTED = ("jarvis_books/assistant/permissions.py", "jarvis_books/ui/approval.py", "jarvis_books/upgrade.py",
             "jarvis_books/keystore.py", "jarvis_books/publish/oauth.py", "tests/test_permissions.py",
             "tests/test_upgrade.py", "tests/conftest.py")
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "GEMINI", "GOOGLE", "CREDENTIAL")
MAX_FILES = 8
MAX_ATTEMPTS = 3
TEST_TIMEOUT_S = 900

STR = {"type": "STRING"}
EDIT_SCHEMA = {"type": "OBJECT", "properties": {
    "summary": {"type": "STRING", "description": "One or two plain sentences for the author: what changes"},
    "edits": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "path": STR, "search": STR, "replace": STR}, "required": ["path", "search", "replace"]}}},
    "required": ["summary", "edits"]}

ENGINEER = (
    "You are a senior Python engineer upgrading the Jarvis Book Writer desktop app (Python 3.10+, PyQt6, SQLite, "
    "Gemini). Make the smallest correct, production-quality change that does what the author asked, in the "
    "code's existing style, and add or update tests under tests/ that prove it works. Rules: only edit .py files "
    "under jarvis_books/ or tests/; never edit these protected files: " + ", ".join(PROTECTED) + ". Never weaken "
    "the yes/no permission gate, never read or send the author's books or keys anywhere, and never add network "
    "calls the author did not ask for. Each edit replaces one exact snippet ('search', copied character for "
    "character from the current file and unique in it) with 'replace'. To create a new file give an empty "
    "'search' and the whole file as 'replace'. At most " + str(MAX_FILES) + " files.")


def app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def check_path(path: str) -> str:
    """The relative path of a file Gemini may edit, or a ValidationError."""
    p = (path or "").strip()
    if not p or "\\" in p or ":" in p or p.startswith("/"):
        raise ValidationError(f"not allowed: {path!r} (use a relative path like jarvis_books/x.py)")
    parts = PurePosixPath(p).parts
    if ".." in parts or "__pycache__" in parts or any(x.startswith(".") for x in parts):
        raise ValidationError(f"not allowed: {path!r}")
    p = str(PurePosixPath(*parts))
    if not p.startswith(EDITABLE_DIRS) or not p.endswith(".py"):
        raise ValidationError(f"not allowed: {p} (only .py files in jarvis_books/ or tests/)")
    if p in PROTECTED:
        raise ValidationError(f"not allowed: {p} is protected safety code")
    return p


def apply_edits(root: Path, edits: list[dict]) -> list[str]:
    """Apply search/replace edits to the copy at root. All are checked before any file is written."""
    staged: dict[str, str] = {}
    for e in edits:
        path = check_path(e.get("path", ""))
        target = root / path
        current = staged.get(path, target.read_text(encoding="utf-8") if target.exists() else None)
        search, replace = e.get("search", ""), e.get("replace", "")
        if not search:
            if current is not None:
                raise ValidationError(f"{path} already exists; give the exact text to replace")
            staged[path] = replace
            continue
        if current is None:
            raise ValidationError(f"{path} does not exist")
        count = current.count(search)
        if count != 1:
            raise ValidationError(f"in {path} the text to replace was found {count} times (it must be exactly once)"
                                  f": {search[:120]!r}")
        staged[path] = current.replace(search, replace)
    if len(staged) > MAX_FILES:
        raise ValidationError(f"too many files changed ({len(staged)} > {MAX_FILES})")
    for path, text in staged.items():
        compile(text, path, "exec")  # a syntax error is reported back to Gemini instead of failing in the tests
    for path, text in staged.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8", newline="\n")
    return sorted(staged)


def clean_env(scratch: Path) -> dict:
    """The environment the copy's tests run in: no secrets, and a throwaway data folder (never the real books)."""
    env = {k: v for k, v in os.environ.items() if not any(w in k.upper() for w in SECRET_WORDS)}
    env.update({"JARVIS_BOOKS_HOME": str(scratch / "home"), "JARVIS_BOOKS_MIRROR": str(scratch / "mirror"),
                "QT_QPA_PLATFORM": "offscreen", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    env.pop("PYTHONPATH", None)
    return env


def run_tests(root: Path, python: str = sys.executable, timeout_s: int = TEST_TIMEOUT_S) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="jb-upgrade-") as scratch:
        try:
            done = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=str(root),
                                  env=clean_env(Path(scratch)), capture_output=True, text=True, timeout=timeout_s,
                                  encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return False, f"the tests took longer than {timeout_s} seconds"
    output = (done.stdout + done.stderr)[-6000:]
    return done.returncode == 0, output


def _sha(path: Path) -> Optional[str]:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


class Upgrader:
    def __init__(self, library, engine, work_root: Path, root: Optional[Path] = None,
                 runner: Callable[[Path], tuple[bool, str]] = run_tests):
        self.library = library
        self.engine = engine
        self.work_root = Path(work_root)
        self.root = Path(root) if root else app_root()
        self.runner = runner

    # ---- building (touches only the copy) ------------------------------------------------
    def start(self, request: str) -> dict:
        request = (request or "").strip()
        if not request:
            raise ValidationError("say what the upgrade should do")
        return self.library.add_upgrade(request)

    def build(self, upgrade_id: int, progress: Callable[[float, str], None] = lambda p, m: None,
              cancel=None) -> dict:
        up = self.library.get_upgrade(upgrade_id)
        if self.engine is None:
            return self.library.update_upgrade(upgrade_id, status="failed", test_output="no Gemini key")
        copy = self.work_root / str(upgrade_id) / "app"
        if copy.exists():
            shutil.rmtree(copy)
        progress(0.05, "Making a separate copy of the app")
        for item in COPY_ITEMS:
            src = self.root / item
            if src.is_dir():
                shutil.copytree(src, copy / item, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            elif src.exists():
                copy.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, copy / item)
        feedback, changed, summary, output = "", [], "", ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if cancel is not None and cancel.is_set():
                return self.library.update_upgrade(upgrade_id, status="failed", test_output="stopped")
            progress(0.1 + 0.8 * (attempt - 1) / MAX_ATTEMPTS, f"Gemini is writing the change (try {attempt})")
            resp = self.engine.generate(LLMRequest(
                role="planner", system=ENGINEER, json_schema=EDIT_SCHEMA, temperature=0.2,
                purpose="app upgrade", instruction=f"The author asked: {up['request']}" + feedback,
                sections=self._sections(copy)))
            data = resp.data or {}
            try:
                changed = sorted(set(changed) | set(apply_edits(copy, list(data.get("edits") or []))))
            except (ValidationError, SyntaxError) as exc:
                feedback = f"\n\nYour last answer could not be applied: {exc}. Fix it and answer again."
                output = str(exc)
                continue
            summary = (data.get("summary") or "").strip() or summary
            if not changed:
                feedback, output = "\n\nYour last answer changed nothing. Make the change.", "no change"
                continue
            progress(0.15 + 0.8 * (attempt - 1) / MAX_ATTEMPTS, "Running every test on the copy")
            ok, output = self.runner(copy)
            if ok:
                return self.library.update_upgrade(
                    upgrade_id, status="ready", summary=summary, files=changed, diff=self._diff(copy, changed),
                    test_output=output, hashes={p: _sha(self.root / p) for p in changed})
            feedback = ("\n\nThe edits were applied but the tests failed. They are in the current files below. "
                        "Fix the code (not the tests' intent). Test output:\n" + output[-3000:])
        return self.library.update_upgrade(upgrade_id, status="failed", summary=summary, files=changed,
                                           diff=self._diff(copy, changed), test_output=output)

    def _sections(self, copy: Path) -> list[PromptSection]:
        files = sorted(p.relative_to(copy).as_posix() for p in copy.rglob("*.py"))
        sections = [PromptSection("Files in the app", "\n".join(files), priority=95, required=True)]
        for rel in files:
            low = rel.startswith("tests/") and rel not in ("tests/fakes.py", "tests/test_gui.py")
            sections.append(PromptSection(rel, (copy / rel).read_text(encoding="utf-8"),
                                          priority=20 if low else 60))
        return sections

    def _diff(self, copy: Path, changed: list[str]) -> str:
        out = []
        for rel in changed:
            old = (self.root / rel).read_text(encoding="utf-8").splitlines(True) if (self.root / rel).exists() else []
            new = (copy / rel).read_text(encoding="utf-8").splitlines(True)
            out += difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}")
        return "".join(out)

    # ---- installing and undoing (touches the running app; only after a yes) ---------------------
    def install(self, upgrade_id: int) -> dict:
        up = self.library.get_upgrade(upgrade_id)
        if up["status"] != "ready":
            raise NotFound(f"upgrade #{upgrade_id} is {up['status']}, not ready to install")
        for rel, sha in up["hashes"].items():
            if _sha(self.root / rel) != sha:
                raise NotFound(f"{rel} changed since upgrade #{upgrade_id} was tested; build it again")
        copy = self.work_root / str(upgrade_id) / "app"
        backup = self.work_root / str(upgrade_id) / "backup"
        added = []
        for rel in up["files"]:
            check_path(rel)
            live = self.root / rel
            if live.exists():
                (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(live, backup / rel)
            else:
                added.append(rel)
        for rel in up["files"]:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(copy / rel, self.root / rel)
        return self.library.update_upgrade(upgrade_id, status="installed", added=added)

    def last_installed(self) -> dict:
        found = next((u for u in self.library.list_upgrades(200) if u["status"] == "installed"), None)
        if found is None:
            raise NotFound("no installed upgrade to undo")
        return found

    def undo(self, upgrade_id: int = 0) -> dict:
        up = self.library.get_upgrade(upgrade_id) if upgrade_id else self.last_installed()
        if up["status"] != "installed":
            raise NotFound(f"upgrade #{up['id']} is {up['status']}, not installed")
        newer = [u for u in self.library.list_upgrades(200) if u["status"] == "installed" and u["id"] > up["id"]]
        if newer:
            raise NotFound(f"undo upgrade #{newer[0]['id']} first (it is newer)")
        backup = self.work_root / str(up["id"]) / "backup"
        for rel in up["files"]:
            check_path(rel)
            if rel in up["added"]:
                (self.root / rel).unlink(missing_ok=True)
            else:
                shutil.copy2(backup / rel, self.root / rel)
        return self.library.update_upgrade(up["id"], status="undone")


def describe(up: dict) -> str:
    files = ", ".join(up["files"][:4]) + (" ..." if len(up["files"]) > 4 else "")
    return f"#{up['id']} {up['summary'] or up['request']} (changes {files})"


