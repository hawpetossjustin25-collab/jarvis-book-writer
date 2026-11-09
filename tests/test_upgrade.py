"""App upgrades: Gemini may only change a tested copy; installing needs a yes and can always be undone."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis_books.assistant.permissions import PermissionGate, level_of
from jarvis_books.assistant.tools import ToolBox
from jarvis_books.errors import NotFound, ValidationError
from jarvis_books.jobs import JobManager
from jarvis_books.upgrade import Upgrader, apply_edits, check_path, clean_env, run_tests

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
        return SimpleNamespace(text="", data=self.answers.pop(0))


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


def make(library, app, tmp_path, engine, runner=lambda root: (True, "1 passed")):
    return Upgrader(library, engine, tmp_path / "work", root=app, runner=runner)


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
    up = make(library, app, tmp_path, engine, runner=lambda root: (False, "FAILED test_greet"))
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
