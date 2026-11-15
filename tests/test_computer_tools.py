"""Jarvis's computer tools through the ToolBox: every change, command and download waits for the author's yes."""
from __future__ import annotations

import json
import sys
import time

import pytest

from jarvis_books.assistant.permissions import SYSTEM, PermissionGate, level_of
from jarvis_books.assistant.tools import COMPUTER_PINS, COMPUTER_TOOLS, ToolBox
from jarvis_books.computer.files import Computer
from jarvis_books.computer.terminal import Terminal
from jarvis_books.computer.web import Web
from jarvis_books.jobs import JobManager

from tests.test_computer import Resp, Session


@pytest.fixture
def box(library, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    gate = PermissionGate(library)
    tools = ToolBox(library, writer=None, index=None, jobs=JobManager(library, workers=1), gate=gate)
    tools.computer = Computer(tmp_path / "work", home=home, platform="linux")
    tools.terminal = Terminal(tmp_path / "work", home=home, platform=sys.platform)
    return tools, gate, home


def wait_job(tools, job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        out = tools.call("command_output", {"job_id": job_id})
        if not out.get("running"):
            return out
        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_every_change_is_system_level_and_never_allowed_for_the_session():
    for name in COMPUTER_PINS:
        assert level_of(name) == SYSTEM, name
    for name in ("list_folder", "read_file", "web_search", "hf_model_info", "github_read", "command_output"):
        assert level_of(name) != SYSTEM


def test_reading_is_free_but_writing_waits_for_yes_and_shows_the_diff(box):
    tools, gate, home = box
    (home / "story.txt").write_text("The cat sat.\n")
    assert "The cat sat." in tools.call("read_file", {"path": "story.txt"})["text"]
    ask = tools.call("edit_file", {"path": "story.txt", "find": "cat", "replace": "dog"})
    assert ask["needs_approval"] and str(home / "story.txt") in ask["asking"]
    assert "-The cat sat." in ask["asking"] and "+The dog sat." in ask["asking"]
    assert (home / "story.txt").read_text() == "The cat sat.\n"  # nothing yet
    gate.answer(True, allow_session=True)
    assert (home / "story.txt").read_text() == "The dog sat.\n"
    assert tools.call("write_file", {"path": "story.txt", "content": "x"})["needs_approval"]  # never pre-approved
    gate.answer(False)
    assert (home / "story.txt").read_text() == "The dog sat.\n"


def test_undo_brings_back_edits_deletes_moves_and_new_folders(box):
    tools, gate, home = box
    (home / "a.txt").write_text("one")
    tools.run("write_file", {"path": "a.txt", "content": "two"})
    tools.run("make_folder", {"path": "new"})
    tools.run("move_path", {"source": "a.txt", "destination": "new"})
    tools.run("delete_path", {"path": "new"})
    assert not (home / "new").exists()
    for _ in range(3):
        tools.run("undo_last_change")
    assert (home / "a.txt").read_text() == "two" and not (home / "new").exists()
    tools.run("undo_last_change")
    assert (home / "a.txt").read_text() == "one"


def test_paths_are_pinned_when_asked_and_bad_ones_never_reach_the_question(box):
    tools, gate, home = box
    ask = tools.call("make_folder", {"path": "Projects/Jarvis"})
    assert gate.pending.args["path"] == str(home / "Projects" / "Jarvis") and "Projects" in ask["asking"]
    gate.answer(False)
    for args in ({"path": "/etc/hosts", "content": "x"}, {"path": "/", "content": "x"},
                 {"path": ".ssh/config", "content": "x"}):
        out = tools.call("write_file", args)
        assert "error" in out and gate.pending is None
    assert "isn't in the file" in tools.call("edit_file", {"path": "nope.txt", "find": "a", "replace": "b"})["error"] \
        or "no file" in tools.call("edit_file", {"path": "nope.txt", "find": "a", "replace": "b"})["error"]


def test_dangerous_commands_are_refused_before_asking_and_warnings_are_shown(box):
    tools, gate, home = box
    out = tools.call("run_command", {"command": "Format-Volume -DriveLetter C"})
    assert "won't run" in out["error"] and gate.pending is None
    ask = tools.call("run_command", {"command": "winget install Git.Git"})
    assert "WARNING: this installs or removes software" in ask["asking"] and tools.terminal.shell in ask["asking"]
    gate.answer(False)


@pytest.mark.skipif(sys.platform == "win32", reason="bash commands")
def test_a_command_runs_after_yes_and_long_ones_report_later(box):
    tools, gate, home = box
    tools.call("run_command", {"command": "echo hi from $(pwd)"})
    result = gate.answer(True)["result"]
    assert result["ok"] and result["output"].strip() == f"hi from {home}" and result["exit_code"] == 0
    tools.COMMAND_WAIT_S = 0.2
    tools.call("run_command", {"command": "sleep 1; echo late", "timeout_seconds": 10})
    started = gate.answer(True)["result"]
    assert started["running"]
    done = wait_job(tools, started["job_id"])
    assert done["output"].strip() == "late"
    assert json.loads((tools.terminal.log_path).read_text().splitlines()[-1])["command"] == "sleep 1; echo late"
    assert [c["command"] for c in tools.call("computer_log")["commands"]][-1] == "sleep 1; echo late"


def test_internet_tools_read_freely_and_downloads_wait_for_yes(box, tmp_path):
    tools, gate, home = box
    info = json.dumps({"id": "LiquidAI/LFM3-1.7B", "siblings": [{"rfilename": "model.gguf", "size": 3}]})
    tools.web = Web(Session({"/api/models/": Resp(body=info.encode()), "README": Resp(body=b"card"),
                             "resolve/main": lambda: Resp(body=b"abc", headers={"Content-Length": "3"})}),
                    check_address=lambda u: u)
    assert tools.call("hf_model_info", {"repo": "LiquidAI/LFM3-1.7B"})["files"][0]["name"] == "model.gguf"
    ask = tools.call("download_file", {"url": "hf://LiquidAI/LFM3-1.7B/model.gguf"})
    assert ask["needs_approval"] and "resolve/main/model.gguf" in ask["asking"] and str(home / "Downloads") in ask["asking"]
    started = gate.answer(True)["result"]
    end = time.time() + 10
    while time.time() < end and tools.jobs.get(started["job_id"]).status != "done":
        time.sleep(0.05)
    assert (home / "Downloads" / "model.gguf").read_bytes() == b"abc"


def test_the_war_room_has_the_internet_but_not_the_computer(box):
    tools, gate, home = box
    tools.war_session_id = 1
    names = {d["name"] for d in tools.declarations()}
    assert not names & set(COMPUTER_TOOLS) and "web_search" in names


def test_requests_pick_the_right_tools(box):
    tools, _, _ = box
    pick = lambda text: {d["name"] for d in tools.select(text)}
    assert {"run_command", "list_folder"} <= pick("run the tests in my project folder in the terminal")
    assert {"hf_search", "download_file"} <= pick("download that model from hugging face")
    assert {"github_search", "github_read"} <= pick("find a github repo for epub tools")
    assert "run_command" not in pick("write chapter 3")


def test_the_studio_wires_the_computer(tmp_path):
    from tests.test_brain import studio as make_studio
    studio = make_studio(tmp_path)
    assert studio.computer.backups == tmp_path / "home" / "computer" / "backups"
    assert studio.tools.computer is studio.computer and studio.tools.terminal is studio.terminal
    assert studio.war_tools.computer is None and studio.war_tools.web is studio.web
    studio.close()
