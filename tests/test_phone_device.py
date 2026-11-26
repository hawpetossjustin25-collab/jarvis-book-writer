"""Jarvis's hands on an Android phone: the laptop's command queue, the Termux helper and the tools, end to end."""
import importlib.util
import json
import threading
import time
import urllib.request

import pytest
import requests

from jarvis_books.assistant.permissions import CHANGE, OUTSIDE, READ, SAFE, SYSTEM, PermissionGate, level_of
from jarvis_books.assistant.phone_device_tools import PHONE_DEVICE_TOOLS, PhoneDeviceTools
from jarvis_books.phone import AGENT_DIR, PhoneServer
from jarvis_books.phone_device import DeviceLink, PhoneFailed, PhoneOffline

spec = importlib.util.spec_from_file_location("jarvis_agent", AGENT_DIR / "jarvis_agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
HOST = "calm-river-test.trycloudflare.com"
UI = ("<?xml version='1.0' encoding='UTF-8'?><hierarchy rotation='0'>"
      "<node text='' resource-id='' clickable='false' bounds='[0,0][1080,2340]'>"
      "<node text='Send' resource-id='com.app:id/send' content-desc='' clickable='true' bounds='[900,2000][1060,2100]'/>"
      "<node text='Hello there' resource-id='' content-desc='' clickable='false' bounds='[40,300][700,380]'/>"
      "<node text='' resource-id='com.app:id/menu' content-desc='More options' clickable='true' bounds='[980,80][1060,160]'/>"
      "<node text='' resource-id='' content-desc='' clickable='false' bounds='[0,0][0,0]'/>"
      "</node></hierarchy>")


class FakeRunner:
    """Answers the Termux and adb commands a real phone would, and records them."""

    def __init__(self, adb=True):
        self.adb, self.calls = adb, []

    def __call__(self, cmd, timeout=30.0, stdin=None):
        self.calls.append((list(cmd), stdin))
        c = cmd[0]
        if c == "getprop":
            return 0, {"ro.product.model": "Pixel 8", "ro.build.version.release": "15"}.get(cmd[1], ""), b""
        if c == "termux-battery-status":
            return 0, '{"percentage": 81}', b""
        if c == "termux-sms-send":
            return 0, "", b""
        if c == "termux-sms-list":
            return 0, json.dumps([{"number": "+14045550134", "body": "Lunch?", "received": "2026-11-25 12:00",
                                   "type": "inbox", "read": False, "threadid": 3}]), b""
        if c == "termux-contact-list":
            return 0, json.dumps([{"name": "Mom", "number": "+14045550101"},
                                  {"name": "Mike Jones", "number": "+16785550102"},
                                  {"name": "Mike Smith", "number": "+16785550103"}]), b""
        if c == "termux-telephony-call":
            return 0, "", b""
        if c == "termux-clipboard-get":
            return 0, "copied words", b""
        if c in ("termux-clipboard-set", "termux-open-url"):
            return 0, "", b""
        if c == "bash":
            return 0, "Linux localhost", b"Linux localhost"
        if c == "adb":
            return self._adb(cmd[1:])
        return 127, f"{c} isn't installed", b""

    def _adb(self, args):
        if args[0] == "devices":
            return 0, "List of devices attached\n" + ("127.0.0.1:37001\tdevice" if self.adb else ""), b""
        if args[0] == "pair":
            return 0, "Successfully paired to 127.0.0.1:41000 [guid=adb-X]", b""
        if args[0] == "connect":
            self.adb = True
            return 0, f"connected to {args[1]}", b""
        if args[0] == "mdns":
            return 0, "adb-X\t_adb-tls-connect._tcp\t192.168.1.20:37001", b""
        if args[0] == "exec-out":
            png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
            return 0, "", png
        if not self.adb:
            return 1, "error: no devices/emulators found", b""
        rest = args[1:]
        if rest[:2] == ["wm", "size"]:
            return 0, "Physical size: 1080x2340", b""
        if rest[:1] == ["cat"]:
            return 0, UI, UI.encode()
        if rest[:3] == ["pm", "list", "packages"]:
            return 0, "package:com.google.android.youtube\npackage:com.facebook.orca\npackage:com.android.chrome", b""
        return 0, "", b""

    def ran(self, name):
        return [c for c, _ in self.calls if c[0] == name or (c[0] == "adb" and name in c)]


# ---- the helper's actions -----------------------------------------------------------------------------------
def test_texts_calls_contacts_and_terminal_use_termux_api():
    run = FakeRunner()
    phone = agent.Phone(run)
    assert agent.execute(phone, "send_text", {"to": "+14045550134", "message": "On my way"}) == \
        {"ok": True, "result": {"sent": True, "to": "+14045550134"}}
    assert run.ran("termux-sms-send")[-1] == ["termux-sms-send", "-n", "+14045550134", "On my way"]
    texts = agent.execute(phone, "read_texts", {"limit": 500})["result"]["texts"]
    assert texts[0]["body"] == "Lunch?" and "threadid" not in texts[0]
    assert run.ran("termux-sms-list")[-1][2] == "50"  # capped
    assert agent.execute(phone, "call", {"number": "(404) 555-0134"})["ok"]
    assert run.ran("termux-telephony-call")[-1] == ["termux-telephony-call", "4045550134"]
    assert agent.execute(phone, "call", {"number": "rm -rf /"}) == \
        {"ok": False, "error": "that doesn't look like a phone number"}
    assert agent.execute(phone, "contacts", {"query": "mike"})["result"]["total"] == 2
    assert agent.execute(phone, "shell", {"command": "uname -a"})["result"] == \
        {"exit_code": 0, "output": "Linux localhost"}
    assert agent.execute(phone, "clipboard", {"text": "hi"})["result"] == {"copied": 2}
    assert run.calls[-1] == (["termux-clipboard-set"], "hi")
    assert agent.execute(phone, "open_url", {"url": "javascript:alert(1)"})["ok"] is False


def test_unknown_actions_and_bad_arguments_are_refused():
    phone = agent.Phone(FakeRunner())
    assert agent.execute(phone, "_adb", {}) == {"ok": False, "error": "this phone doesn't do '_adb'"}
    assert agent.execute(phone, "__init__", {})["ok"] is False
    assert agent.execute(phone, "send_text", {"nope": 1})["error"].startswith("bad request")


def test_screen_control_reads_the_screen_and_taps_by_words():
    run = FakeRunner()
    phone = agent.Phone(run)
    seen = agent.execute(phone, "screen", {"picture": True})["result"]
    assert [e["text"] or e["desc"] for e in seen["elements"]] == ["Send", "Hello there", "More options"]
    assert seen["elements"][0] == {"text": "Send", "desc": "", "id": "send", "clickable": True, "x": 980, "y": 2050}
    assert seen["png"]
    assert agent.execute(phone, "tap", {"text": "send"})["result"] == {"tapped": [980, 2050]}
    assert agent.execute(phone, "tap", {"text": "more"})["result"] == {"tapped": [1020, 120]}
    assert agent.execute(phone, "tap", {"text": "Delete everything"})["ok"] is False
    assert agent.execute(phone, "type", {"text": "hi there\nbye"})["ok"]
    typed = [c for c in run.ran("input") if "text" in c or "keyevent" in c]
    assert typed[-3:] == [["adb", "shell", "input", "text", "hi%sthere"], ["adb", "shell", "input", "keyevent", "66"],
                          ["adb", "shell", "input", "text", "bye"]]
    assert agent.execute(phone, "swipe", {"direction": "up"})["result"] == {"swiped": "up"}
    assert run.ran("swipe")[-1][-5:] == ["540", "1755", "540", "585", "300"]
    assert agent.execute(phone, "key", {"key": "Home"})["ok"] and run.ran("keyevent")[-1][-1] == "3"
    assert agent.execute(phone, "open_app", {"app": "YouTube"})["result"] == {"opened": "com.google.android.youtube"}
    assert agent.execute(phone, "open_app", {"app": "x; reboot"})["ok"] is False


def test_screen_control_explains_itself_until_debugging_is_paired():
    run = FakeRunner(adb=False)
    phone = agent.Phone(run)
    assert "wireless debugging" in agent.execute(phone, "tap", {"x": 1, "y": 2})["error"]
    assert agent.execute(phone, "pair_debugging", {"pair_port": 41000, "code": "12"})["ok"] is False
    done = agent.execute(phone, "pair_debugging", {"pair_port": 41000, "code": "482913"})
    assert done == {"ok": True, "result": {"screen_control": True, "port": 37001}}
    assert ["adb", "pair", "127.0.0.1:41000", "482913"] in run.ran("adb")
    assert phone.info()["screen_control"] is True


def test_ui_elements_ignore_junk():
    assert agent.ui_elements("not xml") == [] and agent.ui_elements("") == []
    assert len(agent.ui_elements(UI, limit=1)) == 1


# ---- the laptop's queue ---------------------------------------------------------------------------------------
def test_device_link_round_trip_timeout_and_offline():
    link = DeviceLink(next_wait_s=0.05)
    with pytest.raises(PhoneOffline):
        link.call("info")
    link.hello({"model": "Pixel 8", "android": "15", "termux_api": True, "screen_control": False, "junk": "x"})
    assert link.status()["phone"] == "Pixel 8" and "junk" not in link.info
    assert link.next(0.01) is None

    def helper():
        cmd = link.next(2)
        assert link.result(cmd["id"], True, {"echo": cmd["args"]})
        assert not link.result(cmd["id"], True, {})  # only once
    threading.Thread(target=helper).start()
    assert link.call("send_text", {"to": "1"}, timeout=5) == {"echo": {"to": "1"}}

    threading.Thread(target=lambda: link.result(link.next(2)["id"], False, None, "no signal")).start()
    with pytest.raises(PhoneFailed, match="no signal"):
        link.call("call", {}, timeout=5)
    with pytest.raises(PhoneFailed, match="didn't answer"):
        link.call("info", timeout=0.05)
    assert link.next(0.01) is None  # the timed-out command isn't left in the queue
    assert not link.result("1", True)


# ---- end to end: helper <-> PhoneServer <-> tools ------------------------------------------------------------
@pytest.fixture
def live(library):
    gate = PermissionGate(library)
    server = PhoneServer(library, assistant=None, gate=gate, port=0)
    server.device_link.next_wait_s = 0.5
    assert server.start()
    base = f"http://127.0.0.1:{server.port}"

    def opener(req, timeout=None):
        local = urllib.request.Request(req.full_url.replace(f"https://{HOST}", base), data=req.data,
                                       headers=dict(req.header_items()), method=req.get_method())
        return urllib.request.urlopen(local, timeout=timeout)
    yield server, base, opener
    server.stop()


def test_the_helper_pairs_as_an_agent_and_runs_jarvis_commands(live, tmp_path, monkeypatch):
    server, base, opener = live
    monkeypatch.setattr(agent, "HOME", tmp_path / ".jarvis")
    monkeypatch.setattr(agent, "CONFIG", tmp_path / ".jarvis" / "agent.json")
    cfg = agent.pair(HOST, server.pair_code(), "Pixel 8", opener=opener)
    assert json.loads((tmp_path / ".jarvis" / "agent.json").read_text()) == cfg
    assert server.devices()[-1]["kind"] == "agent" and server.devices()[-1]["name"] == "Pixel 8"
    with pytest.raises(RuntimeError):
        agent.pair(HOST, "00000000", opener=opener)
    auth = {"Authorization": "Bearer " + cfg["token"]}
    assert requests.post(base + "/api/send", json={"text": "hi"}, headers=auth, timeout=5).status_code == 401
    app = server.pair(server.pair_code())
    assert requests.get(base + "/api/agent/next", headers={"Authorization": "Bearer " + app},
                        timeout=5).status_code == 401  # the chat app can't take phone commands

    stop = threading.Event()
    runner = FakeRunner()
    t = threading.Thread(target=agent.serve, args=(agent.Link(HOST, cfg["token"], opener=opener),
                                                   agent.Phone(runner), stop.is_set), kwargs={"wait_s": 1})
    t.start()
    try:
        deadline = time.time() + 5
        while not server.device_link.online and time.time() < deadline:
            time.sleep(0.02)
        tools = PhoneDeviceTools()
        tools.phone_device, tools.vision = server.device_link, lambda system, parts: "A chat with a Send button."
        assert tools._t_phone_status()["phone"] == "Pixel 8"
        assert tools._t_phone_send_text("Mom", "Home soon")["to"] == "+14045550101"
        with pytest.raises(Exception, match="more than one contact"):
            tools._t_phone_call("mike")
        assert tools._t_phone_call("Mike Smith") == {"calling": "+16785550103"}
        assert tools._t_phone_read_texts()["texts"][0]["body"] == "Lunch?"
        looked = tools._t_phone_look("what's here?")
        assert looked["answer"] == "A chat with a Send button." and looked["elements"][0]["text"] == "Send"
        assert tools._t_phone_tap(text="Send") == {"tapped": [980, 2050]}
        assert tools._t_phone_press("swipe_down") == {"swiped": "down"}
        assert tools._t_phone_open("https://example.com") == {"opened": "https://example.com"}
        assert tools._t_phone_open("messenger") == {"opened": "com.facebook.orca"}
        assert tools._t_phone_terminal("uname -a")["output"] == "Linux localhost"
        with pytest.raises(PhoneFailed, match="phone number"):
            tools._t_phone_call("12")
    finally:
        stop.set()
        t.join(10)
    assert not t.is_alive()


def test_agent_files_are_served_and_setup_is_a_safe_script(live):
    server, base, _ = live
    script = requests.get(base + "/agent/setup.sh", timeout=5)
    assert script.status_code == 200 and script.text.startswith("#!/data/data/com.termux/files/usr/bin/bash")
    assert "set -euo pipefail" in script.text and "*.trycloudflare.com)" in script.text
    assert requests.get(base + "/agent/jarvis_agent.py", timeout=5).text == (AGENT_DIR / "jarvis_agent.py").read_text()
    assert requests.get(base + "/agent/../phone.py", timeout=5).status_code == 404
    assert requests.get(base + "/agent/agent.json", timeout=5).status_code == 404


def test_setup_tool_gives_the_command_with_a_one_time_code():
    tools = PhoneDeviceTools()
    tools.phone_info = lambda: {"host": HOST, "code": "12345678"}
    steps = tools._t_phone_setup()["steps"]
    assert f"curl -fsSL https://{HOST}/agent/setup.sh | bash -s {HOST} 12345678" in steps
    assert "f-droid.org/packages/com.termux.api" in steps[0]
    tools.phone_info = lambda: {"host": None, "code": "", "problem": "the secure link is still starting"}
    with pytest.raises(Exception, match="still starting"):
        tools._t_phone_setup()


def test_risky_phone_tools_wait_for_a_yes():
    levels = {t: level_of(t) for t in PHONE_DEVICE_TOOLS}
    assert levels["phone_send_text"] == levels["phone_call"] == OUTSIDE
    assert levels["phone_terminal"] == SYSTEM
    assert {levels[t] for t in ("phone_tap", "phone_type", "phone_press", "phone_pair_debugging")} == {CHANGE}
    assert levels["phone_open"] == SAFE and levels["phone_read_texts"] == levels["phone_look"] == READ


def test_the_real_runner_reports_failures_instead_of_raising():
    code, out, raw = agent.run(["python3", "-c", "print('hi')"], 10)
    assert (code, out, raw.strip()) == (0, "hi", b"hi")
    assert agent.run(["no-such-program-jarvis"], 5)[0] == 127
    assert agent.run(["python3", "-c", "import time; time.sleep(5)"], 0.2)[0] == 124


class FlakyLink:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def request(self, path, body=None, timeout=40.0):
        self.sent.append((path.split("?")[0], body))
        r = self.replies.pop(0) if self.replies else {"command": None}
        if isinstance(r, Exception):
            raise r
        return r


def test_the_helper_backs_off_when_the_link_is_down_and_stops_when_unpaired():
    import urllib.error
    naps = []
    link = FlakyLink([urllib.error.URLError("down"), urllib.error.URLError("down"), {"ok": True},
                      {"command": {"id": 7, "action": "clipboard", "args": {}}}, {"ok": True},
                      urllib.error.HTTPError("u", 401, "no", {}, None)])
    with pytest.raises(SystemExit, match="setup command again"):
        agent.serve(link, agent.Phone(FakeRunner()), sleep=naps.append, wait_s=1)
    assert naps == [2.0, 4.0]
    assert ("/api/agent/result", {"id": 7, "ok": True, "result": {"clipboard": "copied words"}}) in link.sent
    assert [p for p, _ in link.sent].count("/api/agent/hello") == 3  # said hello again after each outage


def test_link_only_talks_to_jarvis_links():
    with pytest.raises(ValueError):
        agent.Link("evil.example.com")
    with pytest.raises(ValueError):
        agent.Link("x.trycloudflare.com.evil.com")
