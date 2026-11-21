"""App upgrades: Gemini may only change a tested copy; installing needs a yes and can always be undone."""
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis_books.assistant.permissions import PermissionGate, level_of
from jarvis_books.assistant.tools import ToolBox
from jarvis_books.errors import NotFound, ValidationError
from jarvis_books.jobs import JobManager
from jarvis_books.upgrade import (FRESH_START_EVERY, Stopped, Upgrader, apply_edits, check_path, clean_env,
                                  excerpt, run_tests)

FEATURE = "def greet():\n    return 'hi'\n"
FEATURE_TEST = "from jarvis_books.feature import greet\n\n\ndef test_greet():\n    assert greet()\n"
GATE = "LEVELS = 'protected'\n"


class ScriptedEngine:
    """Answers each upgrade request with the next scripted set of edits."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(text="", data=answer)


def edit(path, search, replace, summary="Jarvis says hello"):
    return {"summary": summary, "edits": [{"path": path, "search": search, "replace": replace}]}


GOOD = edit("jarvis_books/feature.py", "return 'hi'", "return 'hello'")


@pytest.fixture
def app(tmp_path):
    root = tmp_path / "live"
    for rel, text in {"jarvis_books/__init__.py": "", "jarvis_books/feature.py": FEATURE,
                      "jarvis_books/assistant/__init__.py": "", "jarvis_books/assistant/permissions.py": GATE,
                      "tests/test_feature.py": FEATURE_TEST, "pyproject.toml": "[project]\nname='x'\n"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def make(library, app, tmp_path, engine, runner=lambda root: (True, "1 passed"), **kw):
    kw.setdefault("wait", lambda seconds, cancel: None)
    return Upgrader(library, engine, tmp_path / "work", root=app, runner=runner, **kw)


def numbered(n):
    """n answers that each change the greeting to a new word, so every try is a real change."""
    words = ["hi"] + [f"w{i}" for i in range(n)]
    return [edit("jarvis_books/feature.py", f"'{a}'", f"'{b}'") for a, b in zip(words, words[1:])]


@pytest.mark.parametrize("bad", ["../x.py", "/etc/x.py", "C:/x.py", "jarvis_books\\x.py", "install.ps1",
                                 "jarvis_books/notes.txt", "jarvis_books/__pycache__/a.py", ".git/hooks/x.py",
                                 "jarvis_books/assistant/permissions.py", "jarvis_books/upgrade.py",
                                 "jarvis_books/keystore.py", "tests/test_permissions.py", "tests/test_upgrade.py",
                                 "jarvis_books/../jarvis_books/upgrade.py", ""])
def test_gemini_can_only_touch_app_code_outside_the_safety_code(bad):
    with pytest.raises(ValidationError):
        check_path(bad)


def test_allowed_paths():
    assert check_path("jarvis_books/ui/goals.py") == "jarvis_books/ui/goals.py"
    assert check_path("tests/test_goals.py") == "tests/test_goals.py"


def test_edits_are_all_or_nothing(app):
    good = {"path": "jarvis_books/feature.py", "search": "'hi'", "replace": "'yo'"}
    for bad in ({"path": "jarvis_books/feature.py", "search": "missing", "replace": "x"},
                {"path": "jarvis_books/feature.py", "search": "", "replace": "x"},
                {"path": "jarvis_books/new.py", "search": "", "replace": "def broken(:\n"},
                {"path": "jarvis_books/assistant/permissions.py", "search": "protected", "replace": "open"}):
        with pytest.raises((ValidationError, SyntaxError)):
            apply_edits(app, [good, bad])
        assert (app / "jarvis_books/feature.py").read_text() == FEATURE
    assert not (app / "jarvis_books/new.py").exists()
    (app / "jarvis_books/twice.py").write_text("a = 1\na = 1\n")
    with pytest.raises(ValidationError, match="2 times"):
        apply_edits(app, [{"path": "jarvis_books/twice.py", "search": "a = 1", "replace": "a = 2"}])


def test_build_changes_only_the_copy_and_records_the_diff(library, app, tmp_path):
    seen = []
    up = make(library, app, tmp_path, ScriptedEngine(GOOD), runner=lambda root: seen.append(root) or (True, "ok"))
    u = up.build(up.start("make Jarvis say hello")["id"])
    assert u["status"] == "ready" and u["files"] == ["jarvis_books/feature.py"]
    assert "-    return 'hi'" in u["diff"] and "+    return 'hello'" in u["diff"]
    assert (app / "jarvis_books/feature.py").read_text() == FEATURE  # the running app is untouched
    assert seen[0] != app and "hello" in (seen[0] / "jarvis_books/feature.py").read_text()
    assert u["summary"] == "Jarvis says hello"


def test_a_bad_answer_is_sent_back_to_gemini_and_retried(library, app, tmp_path):
    engine = ScriptedEngine(edit("jarvis_books/feature.py", "nope", "x"), GOOD)
    up = make(library, app, tmp_path, engine)
    assert up.build(up.start("hello")["id"])["status"] == "ready"
    assert "found 0 times" in engine.requests[1].instruction


def test_failing_tests_are_fed_back_and_a_failed_upgrade_can_never_be_installed(library, app, tmp_path):
    engine = ScriptedEngine(GOOD, edit("jarvis_books/feature.py", "'hello'", "'hey'"),
                            edit("jarvis_books/feature.py", "'hey'", "'hiya'"))
    up = make(library, app, tmp_path, engine, runner=lambda root: (False, "FAILED test_greet"), max_tries=3)
    u = up.build(up.start("hello")["id"])
    assert u["status"] == "failed" and len(engine.requests) == 3
    assert "FAILED test_greet" in engine.requests[1].instruction
    with pytest.raises(NotFound, match="not ready"):
        up.install(u["id"])
    assert (app / "jarvis_books/feature.py").read_text() == FEATURE


def test_trying_to_edit_the_permission_gate_fails_the_upgrade(library, app, tmp_path):
    attack = edit("jarvis_books/assistant/permissions.py", "protected", "open")
    up = make(library, app, tmp_path, ScriptedEngine(attack, attack, attack))
    u = up.build(up.start("let Jarvis skip the yes")["id"])
    assert u["status"] == "failed" and "protected" in u["test_output"]
    assert (tmp_path / "work" / str(u["id"]) / "app/jarvis_books/assistant/permissions.py").read_text() == GATE


def test_install_backs_up_and_undo_restores_including_new_files(library, app, tmp_path):
    answer = {"summary": "goals", "edits": [
        {"path": "jarvis_books/feature.py", "search": "return 'hi'", "replace": "return 'hello'"},
        {"path": "jarvis_books/goals.py", "search": "", "replace": "GOAL = 1000\n"}]}
    up = make(library, app, tmp_path, ScriptedEngine(answer))
    u = up.build(up.start("word goals")["id"])
    assert up.install(u["id"])["status"] == "installed"
    assert "hello" in (app / "jarvis_books/feature.py").read_text() and (app / "jarvis_books/goals.py").exists()
    with pytest.raises(NotFound):
        up.install(u["id"])  # only once
    assert up.undo()["status"] == "undone"
    assert (app / "jarvis_books/feature.py").read_text() == FEATURE and not (app / "jarvis_books/goals.py").exists()
    with pytest.raises(NotFound):
        up.undo()


def test_install_refuses_if_the_app_changed_after_testing(library, app, tmp_path):
    up = make(library, app, tmp_path, ScriptedEngine(GOOD))
    u = up.build(up.start("hello")["id"])
    (app / "jarvis_books/feature.py").write_text(FEATURE + "# changed\n")
    with pytest.raises(NotFound, match="build it again"):
        up.install(u["id"])


def test_undo_goes_newest_first(library, app, tmp_path):
    up = make(library, app, tmp_path, ScriptedEngine(GOOD, edit("jarvis_books/feature.py", "'hello'", "'hey'")))
    first = up.build(up.start("hello")["id"])
    up.install(first["id"])
    second = up.build(up.start("hey")["id"])
    up.install(second["id"])
    with pytest.raises(NotFound, match="first"):
        up.undo(first["id"])
    up.undo()
    up.undo()
    assert (app / "jarvis_books/feature.py").read_text() == FEATURE


def test_the_copy_runs_its_tests_without_secrets_or_real_books(app, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "not-for-tests")
    monkeypatch.setenv("JARVIS_BOOKS_HOME", "/real/books")
    env = clean_env(Path("/scratch"))
    assert "GEMINI_API_KEY" not in env and env["JARVIS_BOOKS_HOME"] != "/real/books"
    (app / "tests/test_env.py").write_text(
        "import os\n\ndef test_env():\n    assert 'GEMINI_API_KEY' not in os.environ\n"
        "    assert os.environ['JARVIS_BOOKS_HOME'] != '/real/books'\n")
    ok, out = run_tests(app, python=sys.executable, timeout_s=120)
    assert ok, out
    (app / "jarvis_books/feature.py").write_text("def greet():\n    return ''\n")
    ok, out = run_tests(app, python=sys.executable, timeout_s=120)
    assert not ok and "test_greet" in out


def test_upgrade_tools_always_ask_and_cannot_be_preapproved(library, app, tmp_path):
    for tool in ("build_upgrade", "install_upgrade", "undo_upgrade"):
        assert level_of(tool) == "system"
    assert level_of("list_upgrades") == "read"
    gate = PermissionGate(library)
    jobs = JobManager(library, workers=1)
    tools = ToolBox(library, writer=None, index=None, jobs=jobs, gate=gate)
    tools.upgrader = make(library, app, tmp_path, ScriptedEngine(GOOD))
    restarts = []
    tools.app_handler = lambda action, args: restarts.append(action) or {"ok": True}

    ask = tools.call("build_upgrade", {"request": "make Jarvis say hello"})
    assert ask["needs_approval"] and "separate test copy" in ask["asking"]
    started = gate.answer(True, allow_session=True)["result"]
    jobs.wait(started["job_id"], timeout=30)
    assert tools.call("list_upgrades")["upgrades"][0]["status"] == "ready"
    assert "error" in tools.call("install_upgrade", {"upgrade_id": 99}) and gate.pending is None

    ask = tools.call("install_upgrade", {"upgrade_id": started["upgrade_id"]})
    assert ask["needs_approval"] and "Jarvis says hello" in ask["asking"]  # a system yes is never remembered
    assert (app / "jarvis_books/feature.py").read_text() == FEATURE
    assert gate.answer(True)["result"]["restarting"] and restarts == ["restart"]
    assert "hello" in (app / "jarvis_books/feature.py").read_text()

    ask = tools.call("undo_upgrade")
    assert ask["needs_approval"] and f"#{started['upgrade_id']}" in ask["asking"]
    gate.answer(True)
    assert (app / "jarvis_books/feature.py").read_text() == FEATURE
    assert "error" in tools.call("undo_upgrade")
    jobs.shutdown()


def test_the_studio_upgrades_the_installed_app_folder(tmp_path):
    from jarvis_books.studio import Studio
    studio = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=None)
    try:
        assert studio.upgrader.root == Path(__file__).resolve().parent.parent
        assert studio.tools.upgrader is studio.war_tools.upgrader is studio.upgrader
        assert os.path.isdir(studio.upgrader.root / "tests")
    finally:
        studio.close()


# ---- builds keep going until the tests pass -----------------------------------------------------------------
def test_a_build_keeps_fixing_failing_tests_past_any_fixed_number_of_tries(library, app, tmp_path):
    results = [(False, f"1 test files passed, 1 failed: tests/test_feature.py try {i}") for i in range(4)]
    results.append((True, "2 test files passed, 0 failed"))
    engine = ScriptedEngine(*numbered(5))
    up = make(library, app, tmp_path, engine, runner=lambda root: results.pop(0))
    progress = []
    u = up.build(up.start("hello")["id"], progress=lambda p, m: progress.append(m))
    assert u["status"] == "ready" and len(engine.requests) == 5
    assert u["test_output"].startswith("passed on try 5")
    assert "try 3" in engine.requests[4].instruction  # the latest failure is handed back to Gemini
    assert any("Try 4" in m and "fixing it" in m for m in progress)


def test_while_still_fixing_the_latest_failure_is_visible(library, app, tmp_path):
    seen = []
    up = make(library, app, tmp_path, ScriptedEngine(*numbered(2)))

    def runner(root):
        if not seen:
            seen.append(library.get_upgrade(1)["status"])
            return False, "1 test files passed, 1 failed: tests/test_feature.py"
        seen.append(library.get_upgrade(1)["test_output"])
        return True, "ok"
    up.runner = runner
    assert up.build(up.start("hello")["id"])["status"] == "ready"
    assert seen[0] == "building" and seen[1].startswith("try 1 failed, still fixing")


def test_after_repeated_failures_it_starts_over_from_a_clean_copy(library, app, tmp_path):
    copy = tmp_path / "work" / "1" / "app" / "jarvis_books" / "feature.py"
    contents = []

    def runner(root):
        contents.append(copy.read_text())
        return (len(contents) > FRESH_START_EVERY), "1 failed"
    answers = numbered(FRESH_START_EVERY) + [GOOD]
    engine = ScriptedEngine(*answers)
    up = make(library, app, tmp_path, engine, runner=runner)
    u = up.build(up.start("hello")["id"])
    assert u["status"] == "ready" and len(engine.requests) == FRESH_START_EVERY + 1
    last = engine.requests[-1].instruction
    assert "clean copy" in last and f"try {FRESH_START_EVERY}: tests failed" in last
    assert "return 'hello'" in contents[-1]  # GOOD applied to the untouched original, not to the earlier tries


def test_when_gemini_is_unreachable_it_waits_and_tries_again(library, app, tmp_path):
    waits = []
    engine = ScriptedEngine(ConnectionError("no internet"), RuntimeError("429 quota"), GOOD)
    up = make(library, app, tmp_path, engine, wait=lambda seconds, cancel: waits.append(seconds))
    u = up.build(up.start("hello")["id"])
    assert u["status"] == "ready" and waits == [30, 60]
    assert u["test_output"].startswith("passed on try 1")


def test_the_author_can_stop_a_build(library, app, tmp_path):
    cancel = threading.Event()
    up = make(library, app, tmp_path, ScriptedEngine(*numbered(3)))
    up.runner = lambda root: (cancel.set(), (False, "1 failed"))[1]
    u = up.build(up.start("hello")["id"], cancel=cancel)
    assert u["status"] == "stopped" and "stopped by the author after 1 tries" in u["test_output"]
    with pytest.raises(NotFound, match="not ready"):
        up.install(u["id"])


def test_a_build_interrupted_by_the_app_closing_carries_on_next_start(library, app, tmp_path):
    cancel = threading.Event()
    up = make(library, app, tmp_path, ScriptedEngine(*numbered(1)))
    up.closing = lambda: True
    up.runner = lambda root: (cancel.set(), (False, "1 failed"))[1]
    uid = up.start("hello")["id"]
    with pytest.raises(Stopped):
        up.build(uid, cancel=cancel)
    assert library.get_upgrade(uid)["status"] == "building" and [u["id"] for u in up.unfinished()] == [uid]

    jobs = JobManager(library, workers=1)
    tools = ToolBox(library, writer=None, index=None, jobs=jobs, gate=PermissionGate(library))
    tools.upgrader = make(library, app, tmp_path, ScriptedEngine(GOOD))
    started = tools.resume_upgrades()
    assert len(started) == 1
    jobs.wait(started[0], timeout=30)
    assert library.get_upgrade(uid)["status"] == "ready"
    assert tools.resume_upgrades() == []
    jobs.shutdown()


def test_a_build_needing_the_safety_code_is_the_one_thing_that_stops_early(library, app, tmp_path):
    attack = edit("jarvis_books/assistant/permissions.py", "protected", "open")
    engine = ScriptedEngine(attack, GOOD, attack, attack, GOOD)
    up = make(library, app, tmp_path, engine, runner=lambda root: (False, "1 failed"))
    u = up.build(up.start("let Jarvis skip the yes")["id"])
    assert u["status"] == "failed" and len(engine.requests) == 4 and "may never change" in u["test_output"]


# ---- the test runner: each file on its own, the way the Windows installer does it ------------------------------
def write_tests(app, files):
    for name, text in files.items():
        (app / "tests" / name).write_text(text)


def test_run_tests_runs_each_file_alone_and_reports_which_failed(app):
    write_tests(app, {"test_ok.py": "def test_ok():\n    assert True\n",
                      "test_bad.py": "def test_bad():\n    assert 1 == 2, 'broken on purpose'\n"})
    ok, out = run_tests(app, timeout_s=120)
    assert not ok and out.startswith("2 test files passed, 1 failed: tests/test_bad.py")
    assert "broken on purpose" in out


def test_a_flaky_file_passes_on_its_retry(app, tmp_path):
    marker = tmp_path / "ran-once"
    write_tests(app, {"test_flaky.py": f"from pathlib import Path\n\ndef test_flaky():\n    m = Path({str(marker)!r})\n"
                                       "    first = not m.exists()\n    m.write_text('x')\n    assert not first\n"})
    ok, out = run_tests(app, timeout_s=120)
    assert ok and "tests/test_flaky.py (on retry)" in out


def test_a_crashing_file_is_run_one_test_at_a_time(app):
    write_tests(app, {"test_crash.py": "import os\n\ndef test_fine():\n    assert True\n\n"
                                       "def test_crash():\n    os._exit(3)\n\ndef test_also_fine():\n    assert True\n"})
    ok, out = run_tests(app, timeout_s=120)
    assert not ok and "tests/test_crash.py::test_crash" in out
    assert "test_fine" not in out.split("FAILED", 1)[1].split("\n")[0]


def test_run_tests_needs_tests(tmp_path):
    (tmp_path / "jarvis_books").mkdir()
    assert run_tests(tmp_path) == (False, "no tests found under tests/")


# ---- test_app: Jarvis tests himself and repairs what fails --------------------------------------------------------
def test_test_app_reports_all_passing(library, app, tmp_path):
    jobs = JobManager(library, workers=2)
    gate = PermissionGate(library)
    tools = ToolBox(library, writer=None, index=None, jobs=jobs, gate=gate)
    tools.upgrader = make(library, app, tmp_path, ScriptedEngine())
    ask = tools.call("test_app")
    assert ask["needs_approval"] and "keeps fixing it" in ask["asking"]
    started = gate.answer(True)["result"]
    assert jobs.wait(started["job_id"], timeout=30).result == "every test passes"
    assert library.list_upgrades() == []
    jobs.shutdown()


def test_test_app_starts_a_repair_that_keeps_going_until_the_tests_pass(library, app, tmp_path):
    jobs = JobManager(library, workers=2)
    gate = PermissionGate(library)
    gate.full_control = lambda: True
    tools = ToolBox(library, writer=None, index=None, jobs=jobs, gate=gate)
    results = [(False, "FAILED tests/test_feature.py::test_greet"), (False, "still failing"), (True, "all passed")]
    tools.upgrader = make(library, app, tmp_path, ScriptedEngine(*numbered(2)), runner=lambda root: results.pop(0))
    started = tools.call("test_app")
    assert "keeps going" in jobs.wait(started["job_id"], timeout=30).result
    repair = library.list_upgrades()[0]
    assert "FAILED tests/test_feature.py::test_greet" in repair["request"]
    build = next(j for j in jobs.recent() if j.kind == "upgrade")
    jobs.wait(build.id, timeout=30)
    assert library.get_upgrade(repair["id"])["status"] == "ready"
    assert (app / "jarvis_books/feature.py").read_text() == FEATURE  # still needs the author's yes to install
    jobs.shutdown()


def test_the_studio_resumes_unfinished_upgrades_and_stops_them_cleanly_on_close(tmp_path):
    from jarvis_books.studio import Studio
    studio = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=None)
    try:
        assert studio.upgrader.closing() is False
        studio.jobs.closing = True
        assert studio.upgrader.closing() is True
        studio.jobs.closing = False
    finally:
        studio.close()


def test_gemini_can_ask_to_read_files_in_full_before_editing(library, app, tmp_path):
    ask = {"summary": "", "edits": [], "need_files": ["jarvis_books/feature.py", "jarvis_books/nope.py", "../x.py"]}
    engine = ScriptedEngine(ask, GOOD)
    up = make(library, app, tmp_path, engine)
    u = up.build(up.start("make Jarvis say hello")["id"])
    assert u["status"] == "ready" and len(engine.requests) == 2
    first = {s.name: s for s in engine.requests[0].sections}
    sent = {s.name: s for s in engine.requests[1].sections}
    assert not first["jarvis_books/feature.py"].required
    assert sent["jarvis_books/feature.py"].required and sent["jarvis_books/feature.py"].text == FEATURE
    assert "now included in full: jarvis_books/feature.py." in engine.requests[1].instruction


def test_asking_to_read_forever_is_treated_as_changing_nothing(library, app, tmp_path):
    from jarvis_books.upgrade import MAX_ASKS
    ask = {"summary": "", "edits": [], "need_files": ["jarvis_books/feature.py"]}
    engine = ScriptedEngine(*([ask] * (MAX_ASKS + 1)), GOOD)
    up = make(library, app, tmp_path, engine)
    assert up.build(up.start("hello")["id"])["status"] == "ready"
    assert "changed nothing" in engine.requests[MAX_ASKS + 1].instruction


def test_files_the_request_names_and_changed_files_fit_first(library, app, tmp_path):
    (app / "jarvis_books/tools.py").write_text("TOOLS = []\n")
    up = make(library, app, tmp_path, ScriptedEngine())
    up._fresh_copy(tmp_path / "copy")
    secs = {s.name: s for s in up._sections(tmp_path / "copy", "add a tool called total_words",
                                            ["jarvis_books/feature.py"])}
    assert secs["jarvis_books/tools.py"].priority > secs["tests/test_feature.py"].priority
    assert secs["jarvis_books/tools.py"].priority > 60 and not secs["jarvis_books/tools.py"].required
    assert secs["jarvis_books/feature.py"].required and secs["Files in the app"].required


def test_gemini_is_told_to_ask_for_files_instead_of_writing_probe_tests():
    from jarvis_books.upgrade import EDIT_SCHEMA, ENGINEER
    assert "need_files" in EDIT_SCHEMA["properties"] and "need_files" in ENGINEER
    assert "Never write tests or code whose job is to print" in ENGINEER


class Fallback(ScriptedEngine):
    def __init__(self, *answers, ready=True):
        super().__init__(*answers)
        self.available, self.ready = False, ready

    def initialize(self):
        self.available = self.ready
        return self.ready


def clocked(library, app, tmp_path, engine, fallback, after=100):
    now, waits, said = [0.0], [], []
    up = make(library, app, tmp_path, engine, fallback=fallback, fallback_after_s=after, clock=lambda: now[0],
              wait=lambda seconds, cancel: (waits.append(seconds), now.__setitem__(0, now[0] + seconds)))
    return up, waits, said


def test_upgrades_wait_for_gemini_before_using_the_slower_free_model(library, app, tmp_path):
    fallback = Fallback(GOOD)
    up, waits, said = clocked(library, app, tmp_path, ScriptedEngine(RuntimeError("429"), RuntimeError("429"), GOOD),
                              fallback)
    u = up.build(up.start("hello")["id"], progress=lambda p, m: said.append(m))
    assert u["status"] == "ready" and waits == [30, 60] and fallback.requests == []
    assert not any("free model" in m for m in said)


def test_after_a_long_outage_the_free_model_writes_the_change(library, app, tmp_path):
    out = [RuntimeError("429 quota")] * 5
    fallback = Fallback(GOOD)
    up, waits, said = clocked(library, app, tmp_path, ScriptedEngine(*out), fallback)
    up.fallback_name = "Qwen3-4B on this computer"
    u = up.build(up.start("hello")["id"], progress=lambda p, m: said.append(m))
    assert u["status"] == "ready" and u["test_output"].startswith("passed on try 1")
    assert waits == [30, 60, 120] and len(fallback.requests) == 1  # out 90 s: too soon; out 210 s: Qwen writes it
    assert fallback.requests[0].instruction.startswith("The author asked: hello")
    assert any("Qwen3-4B on this computer is writing the change" in m for m in said)
    assert "return 'hello'" in (up.work_root / str(u["id"]) / "app/jarvis_books/feature.py").read_text()


def test_gemini_takes_over_again_as_soon_as_it_answers(library, app, tmp_path):
    words = numbered(2)
    engine = ScriptedEngine(RuntimeError("down"), RuntimeError("down"), RuntimeError("down"), words[1])
    fallback = Fallback(words[0])
    runs = []
    up, waits, said = clocked(library, app, tmp_path, engine, fallback, after=50)
    up.runner = lambda root: (runs.append(1), (len(runs) > 1, "ok" if len(runs) > 1 else "1 failed"))[1]
    u = up.build(up.start("hello")["id"])
    assert u["status"] == "ready" and u["test_output"].startswith("passed on try 2")
    assert len(fallback.requests) == 1 and len(engine.requests) == 4


def test_a_free_model_that_is_not_downloaded_just_keeps_the_wait_going(library, app, tmp_path):
    fallback = Fallback(GOOD, ready=False)
    engine = ScriptedEngine(*([RuntimeError("down")] * 4), GOOD)
    up, waits, said = clocked(library, app, tmp_path, engine, fallback)
    u = up.build(up.start("hello")["id"])
    assert u["status"] == "ready" and waits == [30, 60, 120, 300] and fallback.requests == []


def test_a_failing_free_model_does_not_stop_the_build(library, app, tmp_path):
    fallback = Fallback(ConnectionError("ollama stopped"), ConnectionError("ollama stopped"))
    engine = ScriptedEngine(*([RuntimeError("down")] * 4), GOOD)
    up, waits, said = clocked(library, app, tmp_path, engine, fallback, after=50)
    u = up.build(up.start("hello")["id"])
    assert u["status"] == "ready" and waits == [30, 60, 120, 300] and len(fallback.requests) == 2


def test_the_free_model_gets_a_lean_request_and_says_why_it_could_not_answer(library, app, tmp_path):
    ask = {"summary": "", "edits": [], "need_files": ["jarvis_books/feature.py"]}
    engine = ScriptedEngine(ask, *([RuntimeError("429 quota")] * 5))
    fallback = Fallback(RuntimeError("Qwen3-4B has room for about 900 tokens"), GOOD)
    up, waits, said = clocked(library, app, tmp_path, engine, fallback, after=50)
    up.fallback_name = "Qwen3-4B on this computer"
    u = up.build(up.start("hello")["id"], progress=lambda p, m: said.append(m))
    assert u["status"] == "ready" and waits == [30, 60, 120] and len(fallback.requests) == 2
    assert {s.name: s for s in engine.requests[1].sections}["jarvis_books/feature.py"].required
    lean = {s.name: s for s in fallback.requests[1].sections}
    assert not lean["jarvis_books/feature.py"].required and lean["jarvis_books/feature.py"].priority == 88
    assert lean["Files in the app"].required
    assert fallback.requests[1].json_schema == engine.requests[1].json_schema
    assert any("Qwen3-4B on this computer couldn't either: Qwen3-4B has room for about 900" in m for m in said)


def test_the_studio_gives_upgrades_a_qwen_sized_to_each_request(tmp_path):
    from jarvis_books.studio import Studio
    st = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=None)
    try:
        assert st.upgrade_local.fit_request and st.upgrade_local.label == "Qwen3-4B"
        assert st.research_local.label == "Qwen3-4B" and not st.research_local.fit_request
    finally:
        st.ready.set()  # never started, so close needn't wait for startup probes
        st.close()


def test_upgrade_tests_run_without_opening_a_console_window(monkeypatch, tmp_path):
    import subprocess

    from jarvis_books import upgrade
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return SimpleNamespace(returncode=0, stdout="1 passed", stderr="")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    assert upgrade._pytest(sys.executable, tmp_path, {}, [], 5) == (0, "1 passed")
    assert seen["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_an_excerpt_keeps_the_head_and_the_lines_around_each_change_and_marks_the_cuts():
    old = "".join(f"line {i}\n" for i in range(1000))
    new = old.replace("line 500\n", "line 500 changed\n")
    cut = excerpt(old, new, context=5, head=3)
    assert cut.startswith("line 0\nline 1\nline 2\n[...492 unchanged lines omitted...]\nline 495\n")
    assert "line 500 changed\nline 501" in cut and cut.endswith("line 505\n[...494 unchanged lines omitted...]\n")
    assert excerpt("", "a\nb\n") == "a\nb\n"  # a new file is all change


def test_the_free_model_sees_only_the_changed_parts_of_a_big_file(library, app, tmp_path):
    big = "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(2000))
    (app / "jarvis_books/big.py").write_text(big)
    up = make(library, app, tmp_path, ScriptedEngine())
    copy = tmp_path / "copy"
    up._fresh_copy(copy)
    (copy / "jarvis_books/big.py").write_text(big.replace("return 1000\n", "return 'one thousand'\n"))
    whole = {s.name: s for s in up._sections(copy, "x", ["jarvis_books/big.py"])}["jarvis_books/big.py"]
    lean = {s.name: s for s in up._sections(copy, "x", ["jarvis_books/big.py"], cut_big=True)}
    part = lean["jarvis_books/big.py"]
    assert part.required and "return 'one thousand'" in part.text and "unchanged lines omitted" in part.text
    assert len(part.text) < len(whole.text) // 5 and "return 1999" not in part.text
    small = {s.name: s for s in up._sections(copy, "x", ["jarvis_books/feature.py"], cut_big=True)}
    assert small["jarvis_books/feature.py"].text == FEATURE
