"""Upgrading the app's own code by voice, safely.

Gemini edits a separate copy of the app, never the running one, and may only touch the app's Python code
(no books, keys or protected safety code). The full test suite runs on that copy with a throwaway data folder
and no secrets in its environment. Only a copy that passes is offered; installing it backs up every file it
replaces, so the upgrade can be undone.

Building never gives up on its own: when the tests fail, Gemini gets the failures and fixes them, again and
again, until every test passes or the author stops it. Every few failed tries it starts over from a clean copy
with notes on what went wrong, and when Gemini can't be reached it waits and tries again. The one thing that
ends a build early is a change that would need the protected safety code.
"""
from __future__ import annotations

import difflib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
TEST_TIMEOUT_S = 900
FRESH_START_EVERY = 6  # failed tries in a row before starting over from a clean copy
PROTECTED_STRIKES = 3  # tries at the safety code before the build is stopped
WAIT_STEPS = (30, 60, 120, 300, 600)  # seconds to wait when Gemini can't be reached
FALLBACK_AFTER_S = 2 * 3600  # how long the main brain may be out before the free model on this computer takes over
EXCERPT_OVER = 12000  # characters: a changed file bigger than this reaches the free model as the parts around changes
EXCERPT_CONTEXT = 60  # lines kept on each side of a change in such an excerpt

STR = {"type": "STRING"}
EDIT_SCHEMA = {"type": "OBJECT", "properties": {
    "summary": {"type": "STRING", "description": "One or two plain sentences for the author: what changes"},
    "edits": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "path": STR, "search": STR, "replace": STR}, "required": ["path", "search", "replace"]}},
    "need_files": {"type": "ARRAY", "items": STR, "description": "Paths of app files you must read in full "
                   "before you can edit; give no edits when you ask, and they are sent with the next request"}},
    "required": ["summary", "edits"]}
MAX_ASKS = 3  # requests in a row for files to read before an answer must make the change

ENGINEER = (
    "You are a senior Python engineer upgrading the Jarvis Book Writer desktop app (Python 3.10+, PyQt6, SQLite, "
    "Gemini). Make the smallest correct, production-quality change that does what the author asked, in the "
    "code's existing style, and add or update tests under tests/ that prove it works. Rules: only edit .py files "
    "under jarvis_books/ or tests/; never edit these protected files: " + ", ".join(PROTECTED) + ". Never weaken "
    "the yes/no permission gate, never read or send the author's books or keys anywhere, and never add network "
    "calls the author did not ask for. Each edit replaces one exact snippet ('search', copied character for "
    "character from the current file and unique in it) with 'replace'. To create a new file give an empty "
    "'search' and the whole file as 'replace'. At most " + str(MAX_FILES) + " files. Large apps don't fit "
    "whole: if a file you need isn't shown in full below (or ends in [...omitted...]), list it in "
    "'need_files' with no edits and it comes in full next time. Never write tests or code whose job is to "
    "print, raise or otherwise show you the app's source. A new Jarvis tool needs its permission level ('read', "
    "'safe', 'change', 'outside' or 'system') in ADDED_TOOL_LEVELS in jarvis_books/assistant/upgrade_levels.py; "
    "built-in tools keep theirs.")


def excerpt(old: str, new: str, context: int = EXCERPT_CONTEXT, head: int = 30) -> str:
    """The new text cut down to its first lines (imports) and the lines around each change from old; every cut
    is marked so the model knows the file goes on."""
    old_lines, new_lines = old.splitlines(True), new.splitlines(True)
    keep = set(range(min(head, len(new_lines))))
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag != "equal":
            keep.update(range(max(0, j1 - context), min(len(new_lines), max(j2, j1 + 1) + context)))
    out, gap = [], 0
    for n, line in enumerate(new_lines):
        if n in keep:
            if gap:
                out.append(f"[...{gap} unchanged lines omitted...]\n")
                gap = 0
            out.append(line)
        else:
            gap += 1
    if gap:
        out.append(f"[...{gap} unchanged lines omitted...]\n")
    return "".join(out)


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


# New code that rebinds or mutates the yes/no gate's tables or functions from outside permissions.py.
GATE_TAMPERING = re.compile(
    r"\b(TOOL_LEVELS|ALWAYS_ASK|NEEDS_YES|LEVELS|level_of|PermissionGate)\b\s*(\[[^\]]*\]\s*)?"
    r"(=(?!=)|[|+]=|\.(update|setdefault|pop|popitem|clear|append|extend|insert|remove)\s*\()"
    r"|\b(setattr|delattr)\s*\([^)]*\b(permissions|TOOL_LEVELS|level_of|PermissionGate)\b"
    r"|\b(TOOL_LEVELS|level_of)\b[^\n]*\b(__dict__|_mapping)\b")


def apply_edits(root: Path, edits: list[dict]) -> list[str]:
    """Apply search/replace edits to the copy at root. All are checked before any file is written."""
    staged: dict[str, str] = {}
    for e in edits:
        path = check_path(e.get("path", ""))
        target = root / path
        current = staged.get(path, target.read_text(encoding="utf-8") if target.exists() else None)
        search, replace = e.get("search", ""), e.get("replace", "")
        if GATE_TAMPERING.search(replace or ""):
            raise ValidationError(f"not allowed in {path}: changing the protected yes/no permission levels; give a "
                                  "new tool its level in jarvis_books/assistant/upgrade_levels.py instead")
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


def _pytest(python: str, root: Path, env: dict, args: list[str], timeout_s: int) -> tuple[int, str]:
    try:
        done = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=str(root),
                              env=env, capture_output=True, text=True, timeout=timeout_s,
                              encoding="utf-8", errors="replace",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return -1, f"took longer than {timeout_s} seconds"
    return done.returncode, done.stdout + done.stderr


def _one_by_one(python: str, root: Path, env: dict, rel: str, timeout_s: int) -> list[tuple[str, str]]:
    """Run each test of a file in its own process (a native crash in one, as Qt windows can do on Windows,
    can't hide the others). A failing test gets one retry so a timing flake isn't counted as a bug."""
    code, out = _pytest(python, root, env, ["--collect-only", rel], timeout_s)
    ids = [line.strip() for line in out.splitlines() if "::" in line]
    if not ids:
        return [(rel, out)]
    bad = []
    for test_id in ids:
        code, out = _pytest(python, root, env, [test_id], timeout_s)
        if code != 0:
            code, out = _pytest(python, root, env, [test_id], timeout_s)
            if code != 0:
                bad.append((test_id, out))
    return bad


def run_tests(root: Path, python: str = sys.executable, timeout_s: int = TEST_TIMEOUT_S) -> tuple[bool, str]:
    """Every test file in its own process, the way the Windows installer runs them, so a crash in one file
    can't sink the rest. A failing file is run once more; a crashed file is run one test at a time."""
    root = Path(root)
    files = sorted((root / "tests").glob("test_*.py"))
    if not files:
        return False, "no tests found under tests/"
    passed, failed = [], []
    with tempfile.TemporaryDirectory(prefix="jb-upgrade-") as scratch:
        env = clean_env(Path(scratch))
        for f in files:
            rel = f.relative_to(root).as_posix()
            code, out = _pytest(python, root, env, [rel], timeout_s)
            if code in (0, 5):  # 5: the file has no tests
                passed.append(rel)
                continue
            if code == 1:
                code, out = _pytest(python, root, env, [rel], timeout_s)
                if code in (0, 5):
                    passed.append(rel + " (on retry)")
                else:
                    failed.append((rel, out))
            elif code == -1:
                failed.append((rel, out))
            else:
                bad = _one_by_one(python, root, env, rel, timeout_s)
                if bad:
                    failed += bad
                else:
                    passed.append(rel + " (one test at a time)")
    summary = f"{len(passed)} test files passed, {len(failed)} failed"
    if not failed:
        return True, summary + "\n" + "\n".join(passed)
    share = max(800, 5000 // len(failed))
    details = "\n\n".join(f"===== FAILED {name}\n{out[-share:]}" for name, out in failed)
    return False, (summary + ": " + ", ".join(name for name, _ in failed) + "\n\n" + details)[-6000:]


def _sha(path: Path) -> Optional[str]:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


class Stopped(Exception):
    """The app is closing mid-build; the build picks up again next time the app starts."""


class Upgrader:
    def __init__(self, library, engine, work_root: Path, root: Optional[Path] = None,
                 runner: Callable[[Path], tuple[bool, str]] = run_tests, max_tries: int = 0,
                 wait: Optional[Callable[[float, object], None]] = None, fallback=None,
                 fallback_after_s: float = FALLBACK_AFTER_S, clock: Callable[[], float] = time.time):
        self.library = library
        self.engine = engine
        self.fallback = fallback  # slower free model, used only once the main brain has been out a long time
        self.fallback_after_s = fallback_after_s
        self.fallback_name = "the free model on this computer"
        self.clock = clock
        self.work_root = Path(work_root)
        self.root = Path(root) if root else app_root()
        self.runner = runner
        self.max_tries = max_tries  # 0: keep going until the tests pass or the author stops it
        self.wait = wait or (lambda seconds, cancel: cancel.wait(seconds) if cancel is not None
                             else time.sleep(seconds))
        self.closing: Callable[[], bool] = lambda: False  # set by the Studio: True while the app shuts down

    # ---- building (touches only the copy) ------------------------------------------------
    def start(self, request: str) -> dict:
        request = (request or "").strip()
        if not request:
            raise ValidationError("say what the upgrade should do")
        return self.library.add_upgrade(request)

    def unfinished(self) -> list[dict]:
        """Upgrades whose build was still going when the app closed."""
        return [u for u in self.library.list_upgrades(200) if u["status"] == "building"]

    def check(self) -> tuple[bool, str]:
        """Run the full test suite on the app as it is installed now."""
        return self.runner(self.root)

    def _fresh_copy(self, copy: Path) -> None:
        if copy.exists():
            shutil.rmtree(copy)
        for item in COPY_ITEMS:
            src = self.root / item
            if src.is_dir():
                shutil.copytree(src, copy / item, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            elif src.exists():
                copy.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, copy / item)

    def _stopped(self, cancel) -> bool:
        if cancel is None or not cancel.is_set():
            return False
        if self.closing():
            raise Stopped("the app is closing; this upgrade carries on next time it starts")
        return True

    def build(self, upgrade_id: int, progress: Callable[[float, str], None] = lambda p, m: None,
              cancel=None) -> dict:
        up = self.library.get_upgrade(upgrade_id)
        if self.engine is None:
            return self.library.update_upgrade(upgrade_id, status="failed", test_output="no Gemini key")
        copy = self.work_root / str(upgrade_id) / "app"
        progress(0.02, "Making a separate copy of the app")
        self._fresh_copy(copy)
        feedback, changed, summary, output = "", [], "", ""
        need: list[str] = []
        asks = 0
        history: list[str] = []
        tries = failed_in_a_row = strikes = unreachable = 0
        out_since: Optional[float] = None
        while not self.max_tries or tries < self.max_tries:
            if self._stopped(cancel):
                return self.library.update_upgrade(upgrade_id, status="stopped", summary=summary, files=changed,
                                                   test_output=f"stopped by the author after {tries} tries\n"
                                                               + output[-3000:])
            if failed_in_a_row >= FRESH_START_EVERY:
                progress(0.05, f"Starting over from a clean copy after {tries} tries")
                self._fresh_copy(copy)
                changed, failed_in_a_row = [], 0
                feedback = ("\n\nEarlier tries did not pass, so this is a clean copy again. Take a different "
                            "approach. What went wrong before:\n" + "\n".join(history[-FRESH_START_EVERY:]))
            busy = min(0.9, 0.1 + 0.04 * tries)
            progress(busy, f"Jarvis is writing the change (try {tries + 1})")
            request = LLMRequest(
                role="planner", system=ENGINEER, json_schema=EDIT_SCHEMA, temperature=0.2,
                purpose="app upgrade", instruction=f"The author asked: {up['request']}" + feedback,
                sections=self._sections(copy, up["request"], need + changed))
            try:
                resp = self.engine.generate(request)
                out_since = None
            except Exception as exc:
                out_since = self.clock() if out_since is None else out_since
                # The fallback has far less room: only the files it already changed must come in full; the
                # ones asked for come first if they fit, and it can ask for any other file by name.
                lean = lambda: LLMRequest(  # noqa: E731
                    role=request.role, system=request.system, json_schema=request.json_schema,
                    temperature=request.temperature, purpose=request.purpose, instruction=request.instruction,
                    sections=self._sections(copy, up["request"], changed, wanted=need, cut_big=True))
                resp, problem = self._fall_back(lean, out_since, busy, tries, progress)
                if resp is None:
                    delay = WAIT_STEPS[min(unreachable, len(WAIT_STEPS) - 1)]
                    unreachable += 1
                    log.warning("upgrade %s: no brain reachable (%s); waiting %ss", upgrade_id, exc, delay)
                    why = f"{self.fallback_name} couldn't either: {problem[:160]}" if problem else str(exc)[:80]
                    progress(busy, f"No brain is answering ({why}); trying again in {delay} s")
                    self.wait(delay, cancel)
                    continue
            unreachable = 0
            tries += 1
            data = resp.data or {}
            wanted = self._readable(copy, data.get("need_files") or [])
            if not data.get("edits") and data.get("need_files") and asks < MAX_ASKS:
                asks += 1
                need = sorted(set(need) | set(wanted))
                feedback = ("\n\nThe files you asked for are now included in full: " + (", ".join(wanted) or
                            "none of them exist; pick from 'Files in the app'") + ". Now make the change.")
                history.append(f"try {tries}: asked to read {', '.join(wanted) or 'files that do not exist'}")
                continue
            asks = 0
            need = sorted(set(need) | set(wanted))
            try:
                changed = sorted(set(changed) | set(apply_edits(copy, list(data.get("edits") or []))))
            except (ValidationError, SyntaxError) as exc:
                output = str(exc)
                if "protected" in output:
                    strikes += 1
                    if strikes >= PROTECTED_STRIKES:
                        return self.library.update_upgrade(
                            upgrade_id, status="failed", summary=summary, files=changed,
                            test_output="This upgrade would need the protected safety code (the yes/no gate, key "
                                        "storage or the upgrader itself), which upgrades may never change. "
                                        + output)
                feedback = f"\n\nYour last answer could not be applied: {exc}. Fix it and answer again."
                history.append(f"try {tries}: the edits could not be applied: {output[:300]}")
                failed_in_a_row += 1
                continue
            summary = (data.get("summary") or "").strip() or summary
            if not changed:
                feedback, output = "\n\nYour last answer changed nothing. Make the change.", "no change"
                history.append(f"try {tries}: changed nothing")
                failed_in_a_row += 1
                continue
            progress(busy + 0.03, f"Running every test on the copy (try {tries})")
            ok, output = self.runner(copy)
            if ok:
                return self.library.update_upgrade(
                    upgrade_id, status="ready", summary=summary, files=changed, diff=self._diff(copy, changed),
                    test_output=f"passed on try {tries}\n" + output, hashes={p: _sha(self.root / p) for p in changed})
            first = output.strip().splitlines()[0] if output.strip() else "tests failed"
            history.append(f"try {tries}: tests failed: {first[:300]}")
            failed_in_a_row += 1
            self.library.update_upgrade(upgrade_id, summary=summary, files=changed,
                                        test_output=f"try {tries} failed, still fixing\n" + output)
            progress(busy, f"Try {tries}: {first[:80]}; fixing it")
            feedback = ("\n\nThe edits were applied but the tests failed. They are in the current files below. "
                        "Fix the code (not the tests' intent). Test output:\n" + output[-3000:])
        return self.library.update_upgrade(upgrade_id, status="failed", summary=summary, files=changed,
                                           diff=self._diff(copy, changed), test_output=output)

    def _fall_back(self, make_request: Callable[[], LLMRequest], out_since: float, busy: float, tries: int,
                   progress: Callable[[float, str], None]) -> tuple[Optional[object], str]:
        """(the fallback's answer, "") once the main brain has been out for fallback_after_s, else (None, why it
        could not answer; "" while it isn't its turn yet). The main brain is still asked first on every try, so
        it takes over again as soon as it answers."""
        fb = self.fallback
        out_for = self.clock() - out_since
        if fb is None or out_for < self.fallback_after_s:
            return None, ""
        try:
            if not getattr(fb, "available", True) and not fb.initialize():
                problem = str(getattr(fb, "problem", "") or "it isn't ready")
                log.info("upgrade fallback not ready: %s", problem)
                return None, problem
            progress(busy, f"The main brain has been out for {out_for / 60:.0f} min, so {self.fallback_name} "
                           f"is writing the change (try {tries + 1}); this is slower")
            return fb.generate(make_request()), ""
        except Exception as exc:
            log.warning("upgrade fallback failed: %s", exc)
            return None, str(exc) or type(exc).__name__

    @staticmethod
    def _readable(copy: Path, paths: list) -> list[str]:
        """The requested paths that are app source files in the copy (protected ones may be read, not edited)."""
        out = []
        for raw in paths:
            p = str(raw or "").strip().replace("\\", "/")
            parts = PurePosixPath(p).parts
            if (p.endswith(".py") and p.startswith(EDITABLE_DIRS) and ".." not in parts
                    and (copy / p).is_file()):
                out.append(str(PurePosixPath(*parts)))
        return sorted(set(out))

    def _sections(self, copy: Path, request: str = "", full: list[str] = (),
                  wanted: list[str] = (), cut_big: bool = False) -> list[PromptSection]:
        """Every source file, ranked so what Gemini asked for, already changed, or the request names fits first.
        Files in full are always sent (whole, or with cut_big only the parts around the changes of a big one);
        wanted ones come next if there is room."""
        files = sorted(p.relative_to(copy).as_posix() for p in copy.rglob("*.py") if "__pycache__" not in p.parts)
        words = {w.rstrip("s") for w in re.findall(r"[a-z]{3,}", request.lower())}
        sections = [PromptSection("Files in the app", "\n".join(files), priority=95, required=True)]
        for rel in files:
            text = (copy / rel).read_text(encoding="utf-8")
            if rel in full:
                if cut_big and len(text) > EXCERPT_OVER:
                    live = self.root / rel
                    old = live.read_text(encoding="utf-8") if live.is_file() else ""
                    text = ("(Only the parts around your earlier changes are shown; edit inside them.)\n"
                            + excerpt(old, text))
                sections.append(PromptSection(rel, text, priority=90, required=True))
                continue
            if rel in wanted:
                sections.append(PromptSection(rel, text, priority=88))
                continue
            low = rel.startswith("tests/") and rel not in ("tests/fakes.py", "tests/test_gui.py")
            named = words & {w.rstrip("s") for w in re.findall(r"[a-z]{3,}", rel.lower())} - {"jarvis", "book", "test"}
            priority = 20 if low else 60
            sections.append(PromptSection(rel, text, priority=priority + (15 if named else 0)))
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


