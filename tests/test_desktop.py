"""Jarvis's desktop control, Full control, and the fix for 'I can only do things inside the app'."""
from __future__ import annotations

import base64
import json
import sys

import pytest

from fakes import FakeGemini, body
from jarvis_books.assistant import Assistant
from jarvis_books.assistant.desktop_tools import DESKTOP_TOOLS
from jarvis_books.assistant.permissions import ALWAYS_ASK, NEEDS_YES, PermissionGate, level_of
from jarvis_books.assistant.tools import COMPUTER_CHANGES, ToolBox
from jarvis_books.computer.desktop import SCRIPTS, Desktop, key_codes, sendkeys_escape
from jarvis_books.computer.files import Computer
from jarvis_books.computer.terminal import Terminal
from jarvis_books.jobs import JobManager
from jarvis_books.llm.brain import GEMINI, LOCAL, Brain
from jarvis_books.llm.models import LLMRequest, LLMResponse
from jarvis_books.writer import prompts

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg==")


class Runner:
    """Stands in for PowerShell: records each script and its inputs, answers per script."""

    def __init__(self, answers=None):
        self.calls: list[tuple[str, dict]] = []
        self.answers = answers or {}

    def __call__(self, script, env):
        name = next(k for k, v in SCRIPTS.items() if v == script)
        self.calls.append((name, env))
        answer = self.answers.get(name, (0, "ok", ""))
        if callable(answer):
            answer = answer(env)
        return answer

    def names(self):
        return [n for n, _ in self.calls]


@pytest.fixture
def desk(tmp_path):
    runner, opened = Runner(), []
    return Desktop(tmp_path / "work", platform="win32", runner=runner, opener=opened.append), runner, opened


# ---- the desktop itself -------------------------------------------------------------------
def test_websites_links_files_and_folders_open_with_their_usual_program(desk, tmp_path):
    d, runner, opened = desk
    assert d.open("youtube.com") == {"opened": "https://youtube.com", "kind": "website"}
    assert d.open("https://example.org/a?b=1")["kind"] == "website"
    assert d.open("mailto:mom@example.com")["kind"] == "link"
    note = tmp_path / "notes.txt"
    note.write_text("hi")
    assert d.open(str(note)) == {"opened": str(note), "kind": "file"}
    assert d.open(str(tmp_path))["kind"] == "folder"
    assert opened == ["https://youtube.com", "https://example.org/a?b=1", "mailto:mom@example.com",
                      str(note), str(tmp_path)]
    assert runner.calls == []


def test_programs_are_found_by_name_and_the_name_never_becomes_part_of_the_script(desk):
    d, runner, _ = desk
    runner.answers["open_app"] = (0, "started Spotify\n", "")
    assert d.open("spotify") == {"opened": "Spotify", "kind": "program"}
    evil = "x'; Remove-Item -Recurse -Force ~; '"
    d.open(evil)
    name, env = runner.calls[-1]
    assert name == "open_app" and env == {"JB_A": evil} and evil not in SCRIPTS["open_app"]
    assert d.open("notepad.exe")["kind"] == "program"  # not mistaken for a website


def test_unknown_programs_and_scripts_are_explained(desk, tmp_path):
    d, runner, _ = desk
    runner.answers["open_app"] = (3, "not found", "")
    with pytest.raises(LookupError, match="couldn't find a program called 'zzz'"):
        d.open("zzz")
    bat = tmp_path / "go.bat"
    bat.write_text("echo hi")
    with pytest.raises(ValueError, match="run_command"):
        d.open(str(bat))
    with pytest.raises(ValueError):
        d.open("  ")
    with pytest.raises(LookupError, match="nothing at"):
        d.open(str(tmp_path / "missing" / "file.txt"))


def test_closing_asks_politely_unless_forced_and_protects_windows_itself(desk):
    d, runner, _ = desk
    runner.answers["close_program"] = (0, "Discord", "")
    out = d.close("discord")
    assert out["closed"] == "Discord" and not out["forced"] and "save" in out["note"]
    assert runner.calls[-1] == ("close_program", {"JB_A": "discord", "JB_B": "0"})
    assert d.close("discord", force=True)["forced"] and runner.calls[-1][1]["JB_B"] == "1"
    for bad in ("explorer", "*", "", "lsass"):
        with pytest.raises(ValueError):
            d.close(bad)
    runner.answers["close_program"] = (3, "not found", "")
    with pytest.raises(LookupError, match="list_programs"):
        d.close("steam")


def test_system_facts_and_open_programs_come_back_as_data(desk):
    d, runner, _ = desk
    facts = {"battery_percent": 81, "plugged_in": True, "disks": [{"drive": "C:", "free_gb": 120.5}]}
    runner.answers["system_info"] = (0, json.dumps(facts), "")
    runner.answers["list_programs"] = (0, json.dumps({"name": "chrome", "id": 4, "window": "YouTube"}), "")
    assert d.info() == facts
    assert d.programs() == {"open_programs": [{"name": "chrome", "id": 4, "window": "YouTube"}]}
    runner.answers["system_info"] = (1, "", "Get-CimInstance : access denied\n")
    with pytest.raises(RuntimeError, match="access denied"):
        d.info()


def test_clipboard_typing_keys_mouse_and_media(desk):
    d, runner, _ = desk
    runner.answers["get_clipboard"] = (0, "copied words", "")
    assert d.get_clipboard() == {"clipboard": "copied words"}
    assert d.set_clipboard("hello") == {"copied": 5} and runner.calls[-1][1] == {"JB_A": "hello"}
    d.type_text("50% off (today)\nbye")
    assert runner.calls[-1][1]["JB_A"] == "50{%} off {(}today{)}{ENTER}bye"
    assert d.press("ctrl+s") == {"pressed": "ctrl+s"} and runner.calls[-1][1]["JB_A"] == "17,83"
    d.click(100, 200, "right", 2)
    assert runner.calls[-1] == ("click", {"JB_A": "100", "JB_B": "200", "JB_C": "right", "JB_D": "2"})
    assert d.media("volume up", 5) == {"done": "volume_up", "times": 5}
    assert runner.names()[-5:] == ["press_keys"] * 5 and runner.calls[-1][1]["JB_A"] == "175"
    with pytest.raises(ValueError):
        d.media("explode")
    with pytest.raises(ValueError):
        d.type_text("")
    with pytest.raises(ValueError):
        d.click(1, 1, "middle")


def test_key_names():
    assert key_codes("Ctrl + Shift + Esc") == [0x11, 0x10, 0x1B]
    assert key_codes("alt+f4") == [0x12, 0x73] and key_codes("win+d") == [0x5B, ord("D")]
    with pytest.raises(ValueError, match="unknown key 'banana'"):
        key_codes("ctrl+banana")
    with pytest.raises(ValueError):
        key_codes("")
    assert sendkeys_escape("a+b^c~{x}[y]") == "a{+}b{^}c{~}{{}x{}}{[}y{]}"


def test_screenshots_are_saved_and_only_the_last_twenty_kept(desk):
    d, runner, _ = desk

    def shoot(env):
        open(env["JB_A"], "wb").write(PNG)
        return 0, "1920x1080", ""
    runner.answers["screenshot"] = shoot
    d.shots.mkdir(parents=True)
    for i in range(25):
        (d.shots / f"screen-2000010{i:02d}.png").write_bytes(b"old")
    shot = d.screenshot()
    assert shot["width"] == 1920 and shot["height"] == 1080
    assert len(list(d.shots.glob("*.png"))) == 20
    part = d.image_part(shot["path"])["inlineData"]
    assert part["mimeType"] == "image/png" and base64.b64decode(part["data"]) == PNG


def test_other_systems_open_things_but_windows_only_parts_say_so(tmp_path):
    opened = []
    d = Desktop(tmp_path, platform="linux", opener=opened.append, runner=lambda s, e: pytest.fail("ran powershell"))
    assert d.open("example.com")["opened"] == "https://example.com"
    assert "home_disk_free_gb" in d.info()
    with pytest.raises(ValueError, match="only works on Windows"):
        d.get_clipboard()


# ---- the tools Jarvis calls -----------------------------------------------------------------
@pytest.fixture
def box(library, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    gate = PermissionGate(library)
    tools = ToolBox(library, writer=None, index=None, jobs=JobManager(library, workers=1), gate=gate)
    runner, opened = Runner(), []
    tools.desktop = Desktop(tmp_path / "work", platform="win32", runner=runner, opener=opened.append)
    tools.computer = Computer(tmp_path / "work", home=home, platform="linux")
    tools.terminal = Terminal(tmp_path / "work", home=home, platform=sys.platform)
    return tools, gate, runner, opened, home


def test_looking_and_opening_need_no_yes_but_typing_clicking_and_closing_do(box):
    tools, gate, runner, opened, _ = box
    runner.answers["system_info"] = (0, '{"battery_percent": 64}', "")
    assert tools.call("system_info", {}) == {"battery_percent": 64}
    assert tools.call("open_item", {"target": "youtube.com"})["kind"] == "website" and opened
    assert tools.call("media_control", {"action": "volume_up", "times": 3})["times"] == 3
    assert gate.pending is None
    ask = tools.call("type_text", {"text": "hello"})
    assert ask["needs_approval"] and ask["asking"] == "Type into the window in front: hello"
    assert "type_text" not in runner.names()
    gate.answer(True)
    assert runner.calls[-1] == ("type_text", {"JB_A": "hello"})
    assert tools.call("click_screen", {"x": 5, "y": 6, "clicks": 2})["asking"] == "Double-click the screen at 5, 6"
    gate.answer(False)
    assert "click" not in runner.names()
    assert "right away" in tools.call("close_program", {"name": "discord", "force": "yes"})["asking"]


def test_every_desktop_tool_has_a_level_and_a_working_declaration(box):
    tools, *_ = box
    names = {d["name"] for d in tools.declarations()}
    for name in DESKTOP_TOOLS:
        assert name in names and level_of(name) in ("read", "safe", "change")
        assert callable(getattr(tools, f"_t_{name}"))


def test_look_at_screen_shows_gemini_the_screenshot(box):
    tools, gate, runner, _, _ = box
    runner.answers["screenshot"] = lambda env: (open(env["JB_A"], "wb").write(PNG) and 0, "800x600", "")
    seen = []
    tools.vision = lambda system, parts: seen.append((system, parts)) or "Notepad is open; the Save button is at 40,12."
    out = tools.call("look_at_screen", {"question": "where is save?"})
    assert out["answer"].startswith("Notepad is open") and out["screen"] == "800x600" and gate.pending is None
    system, parts = seen[0]
    assert "800x600" in system and parts[0]["inlineData"]["mimeType"] == "image/png"
    assert parts[1] == {"text": "where is save?"}
    tools.vision = None
    assert "needs Gemini" in tools.call("look_at_screen", {})["error"]


def test_full_control_runs_changes_straight_away_but_keeps_the_hard_blocks(box, library):
    tools, gate, runner, _, home = box
    on = {"value": False}
    gate.full_control = lambda: on["value"]
    assert tools.call("type_text", {"text": "hi"})["needs_approval"]
    gate.answer(False)
    on["value"] = True
    assert tools.call("type_text", {"text": "hi"}) == {"typed": 2}
    assert tools.call("write_file", {"path": "a.txt", "content": "made"})["written"].endswith("a.txt")
    assert (home / "a.txt").read_text() == "made" and gate.pending is None
    assert "won't run" in tools.call("run_command", {"command": "format c:"})["error"]
    assert "won't run" in tools.call("run_command", {"command": "Set-MpPreference -DisableRealtimeMonitoring $true"})["error"]
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("secret")
    assert "error" in tools.call("read_file", {"path": ".ssh/id_rsa"})
    for name in ALWAYS_ASK:
        assert gate.decide(name) == "ask"


def test_a_broken_full_control_setting_means_asking(box):
    tools, gate, *_ = box
    gate.full_control = lambda: 1 / 0
    assert gate.decide("type_text") == "ask"


def test_full_control_is_a_setting_jarvis_can_change_only_with_a_yes(box):
    tools, gate, *_ = box

    class Store:
        values = {"full_control": "0"}
        DEFAULTS = values

        def settings(self):
            return dict(self.values)

        def set_setting(self, k, v):
            self.values[k] = v
    tools.settings_store = Store()
    ask = tools.call("change_setting", {"key": "full_control", "value": "on"})
    assert ask["needs_approval"] and Store.values["full_control"] == "0"
    gate.answer(True)
    assert Store.values["full_control"] == "1"


def test_run_as_administrator_says_so_and_elevates(box, tmp_path):
    tools, gate, *_ = box
    ask = tools.call("run_command", {"command": "Get-Service", "as_admin": "yes"})
    assert "AS ADMINISTRATOR" in ask["asking"]
    term = Terminal(tmp_path / "w", platform="win32")
    argv = term.admin_argv("Get-Service | Select -First 1", tmp_path / "o'ut.txt")
    script = argv[-1]
    assert argv[0] == "powershell.exe" and "-Verb RunAs" in script and "-EncodedCommand" in script
    encoded = script.split("'-EncodedCommand','")[1].split("'")[0]
    inner = base64.b64decode(encoded).decode("utf-16-le")
    assert "Get-Service | Select -First 1" in inner and "o''ut.txt" in inner
    with pytest.raises(ValueError, match="only for Windows"):
        Terminal(tmp_path / "x", platform="linux").run("ls", as_admin=True)


def test_the_war_room_can_look_but_not_change(box):
    tools, *_ = box
    tools.war_session_id = 1
    names = {d["name"] for d in tools.declarations()}
    assert {"system_info", "look_at_screen", "list_programs", "open_item"} <= names
    assert not names & set(COMPUTER_CHANGES)


# ---- the root cause: typed chat only got the tools its words matched ------------------------------
EVERYDAY = ["open spotify", "open chrome and go to youtube", "check my battery", "turn up the volume",
            "send an email to my mom", "take a screenshot", "play some music", "copy this to my clipboard",
            "type hello in notepad", "close discord", "how much space is on my hard drive", "do it"]


class Recorder:
    def __init__(self):
        self.requests: list[LLMRequest] = []

    def get_runtime_configuration(self):
        return type("C", (), {"history_turns": 4})()

    def generate(self, req):
        self.requests.append(req)
        return LLMResponse(text="On it.", model="fake")


def test_typed_chat_gives_gemini_every_tool_for_everyday_computer_requests(box, library):
    tools, *_ = box
    engine = Recorder()
    chat = Assistant(engine, library, tools)
    every = {d["name"] for d in tools.declarations()}
    for text in EVERYDAY:
        chat.send(text)
        req = engine.requests[-1]
        assert {d["name"] for d in req.tools} == every, text
        assert {"open_item", "run_command", "research_web"} <= {d["name"] for d in req.local_tools}, text


def test_the_local_model_list_matches_the_request_and_follow_ups_keep_it(box, library):
    tools, *_ = box
    engine = Recorder()
    chat = Assistant(engine, library, tools)
    local = lambda: {d["name"] for d in engine.requests[-1].local_tools}
    chat.send("check my battery")
    assert "system_info" in local()
    chat.send("take a screenshot and click the save button")
    assert {"look_at_screen", "click_screen"} <= local()
    chat.send("do it")
    assert {"look_at_screen", "click_screen"} <= local()  # carried over from the last request
    assert len(engine.requests[-1].local_tools) < len(engine.requests[-1].tools)


def test_the_brain_hands_lfm3_the_short_list_and_gemini_the_full_one():
    seen = {}

    class Eng:
        def __init__(self, name):
            self.name = name

        def generate(self, req):
            seen[self.name] = [d["name"] for d in req.tools]
            return LLMResponse(text="ok", model=self.name)
    full, short = [{"name": "a"}, {"name": "b"}], [{"name": "a"}]
    req = LLMRequest(role="chat", tools=full, local_tools=short)
    assert Brain._for(GEMINI, req).tools == full and Brain._for(LOCAL, req).tools == short
    assert Brain._for(LOCAL, LLMRequest(role="chat", tools=full)).tools == full
    assert req.tools == full  # the caller's request is not changed


def test_jarvis_is_told_he_runs_the_whole_computer_not_just_the_app():
    text = prompts.ASSISTANT
    assert "Never say you can only do things inside the app" in text
    assert "say what you actually did" in text and "never steps you skipped" in text
    assert "whole Windows computer" in text and "look_at_screen" in text and "open_item" in text
    assert "Never turn off virus protection" in text
    assert "book-writing studio" not in text
    assert "Never say you don't have access to information" in text and "'use the researcher'" in text


def test_needs_yes_is_unchanged():
    assert set(NEEDS_YES) == {"change", "outside", "system"}


# ---- the Studio -----------------------------------------------------------------------------
def test_the_studio_wires_desktop_vision_and_full_control(tmp_path):
    from tests.test_brain import studio as make_studio
    payloads = []

    def responder(model_id, payload):
        payloads.append(payload)
        return body("I see a desktop.")
    s = make_studio(tmp_path, FakeGemini(responder))
    try:
        assert s.tools.desktop is s.desktop and s.war_tools.desktop is s.desktop
        assert s.settings()["full_control"] == "0" and s.gate.decide("write_file") == "ask"
        s.set_setting("full_control", "1")
        assert s.gate.decide("write_file") == "auto" and s.gate.decide("install_upgrade") == "ask"
        assert s.tools.vision("look", [{"inlineData": {"mimeType": "image/png", "data": "QQ=="}},
                                       {"text": "what is there?"}]) == "I see a desktop."
        sent = payloads[-1]["contents"][-1]["parts"]
        assert sent[0]["inlineData"]["data"] == "QQ==" and sent[1]["text"] == "what is there?"
    finally:
        s.close()
