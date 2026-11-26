"""Jarvis on the phone: pairing, token checks, limits, streamed turns, Yes/No answers, the app files, the tunnel."""
import json
import re
import sys
import threading
import time
from pathlib import Path

import pytest
import requests

from conftest import hardware
from fakes import FakeGemini, FakeOllama, body
from jarvis_books.assistant.permissions import PermissionGate
from jarvis_books.assistant.phone_tools import PHONE_TOOLS
from jarvis_books.phone import (APP_DIR, CSP, DEVICES, MAX_BODY, MAX_DEVICES, MAX_TEXT, PAIR_TRIES, STATIC, Busy,
                                PhoneServer, Tunnel, cloudflared_asset, phone_line, phone_link)
from jarvis_books.studio import Studio

HOST = "calm-river-blue-sky.trycloudflare.com"
ORIGIN = "https://hawpetossjustin25-collab.github.io"


class FakeAssistant:
    """Streams `partials`, can block until released, and can ask the gate a question mid-turn."""

    def __init__(self, reply="Hi Justin.", partials=(), hold=None, ask=None):
        self.reply, self.partials, self.hold, self.ask = reply, list(partials), hold, ask
        self.calls: list[tuple] = []

    def send(self, text, on_tool=None, on_text=None, decided=None):
        self.calls.append((text, decided))
        for p in self.partials:
            on_text(p)
        if self.hold is not None:
            self.hold.wait(5)
        if self.ask is not None and decided is None:
            self.ask()
        return self.reply


@pytest.fixture
def make(library):
    servers = []

    def build(assistant=None, gate=None, **kw):
        gate = gate or PermissionGate(library)
        srv = PhoneServer(library, assistant or FakeAssistant(), gate, port=0, **kw)
        assert srv.start()
        servers.append(srv)
        return srv, gate

    yield build
    for srv in servers:
        srv.stop()


def url(srv, path):
    return f"http://127.0.0.1:{srv.port}{path}"


def paired(srv):
    token = srv.pair(srv.pair_code(), "test phone")
    assert token
    return {"Authorization": f"Bearer {token}"}


def finish(srv, auth, turn, timeout=5.0):
    deadline, after = time.time() + timeout, -1
    while time.time() < deadline:
        view = requests.get(url(srv, f"/api/turn?id={turn}&after={after}&wait=2"), headers=auth, timeout=10).json()
        if view["done"]:
            return view
        after = view["version"]
    raise AssertionError("the turn never finished")


# ---- pairing ------------------------------------------------------------------------------------------------
def test_pairing_hands_out_a_token_once_and_stores_only_its_hash(make, library):
    srv, _ = make()
    code = srv.pair_code()
    assert re.fullmatch(r"\d{8}", code) and srv.pair_code() == code
    r = requests.post(url(srv, "/api/pair"), json={"code": code, "name": "Pixel"}, timeout=5)
    token = r.json()["token"]
    assert r.status_code == 200 and len(token) >= 40
    assert token not in library.get_meta(DEVICES) and srv.devices()[0]["name"] == "Pixel"
    assert requests.post(url(srv, "/api/pair"), json={"code": code}, timeout=5).status_code == 403  # used up
    assert srv.pair_code() != code
    hello = requests.get(url(srv, "/api/hello"), headers={"Authorization": f"Bearer {token}"}, timeout=5).json()
    assert hello["paired"] and hello["app"] == "jarvis"


def test_wrong_codes_burn_the_code(make):
    srv, _ = make()
    code = srv.pair_code()
    wrong = f"{(int(code) + 1) % 10 ** 8:08d}"
    for _ in range(PAIR_TRIES - 1):
        assert srv.pair(wrong) is None
        assert srv.pair_code() == code
    assert srv.pair("abc") is None
    assert srv.pair_code() != code and srv.pair(code) is None  # a guesser can't keep hammering the same code


def test_only_the_newest_devices_are_kept_and_unpair_works(make):
    srv, _ = make()
    tokens = [srv.pair(srv.pair_code(), f"p{i}") for i in range(MAX_DEVICES + 2)]
    assert len(srv.devices()) == MAX_DEVICES
    assert srv.device_for(tokens[0]) is None and srv.device_for(tokens[-1]) is not None
    r = requests.post(url(srv, "/api/unpair"), json={}, headers={"Authorization": f"Bearer {tokens[-1]}"}, timeout=5)
    assert r.json() == {"unpaired": True} and srv.device_for(tokens[-1]) is None
    srv.forget_devices()
    assert srv.devices() == []


def test_api_refuses_without_a_good_token(make):
    srv, _ = make()
    paired(srv)
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}):
        assert requests.post(url(srv, "/api/send"), json={"text": "hi"}, headers=headers, timeout=5).status_code == 401
        assert requests.get(url(srv, "/api/turn?id=x"), headers=headers, timeout=5).status_code == 401
        assert requests.post(url(srv, "/api/answer"), json={"request": 1, "yes": True}, headers=headers,
                             timeout=5).status_code == 401
    assert not requests.get(url(srv, "/api/hello"), timeout=5).json()["paired"]


# ---- limits and bad input -----------------------------------------------------------------------------------
def test_limits_and_bad_input_get_clear_errors(make):
    srv, _ = make()
    auth = paired(srv)
    send = lambda headers=auth, **kw: requests.post(url(srv, "/api/send"), headers=headers, timeout=5, **kw)  # noqa: E731
    assert send(json={"text": "   "}).status_code == 400
    assert send(json={"text": "x" * (MAX_TEXT + 1)}).status_code == 400
    assert send(data=b"{nope", headers={**auth, "Content-Type": "application/json"}).status_code == 400
    assert send(data=b"[1]", headers={**auth, "Content-Type": "application/json"}).status_code == 400
    assert send(data=b"x" * (MAX_BODY + 1), headers={**auth, "Content-Type": "application/json"}).status_code == 413
    assert requests.post(url(srv, "/api/answer"), json={"request": "x"}, headers=auth, timeout=5).status_code == 400
    assert requests.get(url(srv, "/api/turn?id=x&after=y"), headers=auth, timeout=5).status_code == 400
    assert requests.get(url(srv, "/api/turn?id=missing"), headers=auth, timeout=5).status_code == 404
    assert requests.post(url(srv, "/api/nothing"), json={}, headers=auth, timeout=5).status_code == 404


# ---- turns --------------------------------------------------------------------------------------------------
def test_a_reply_streams_and_finishes(make):
    hold = threading.Event()
    assistant = FakeAssistant("Hello, the whole reply.", partials=["Hello", "Hello, the"], hold=hold)
    srv, _ = make(assistant)
    auth = paired(srv)
    turn = requests.post(url(srv, "/api/send"), json={"text": "hi jarvis"}, headers=auth, timeout=5).json()["turn"]
    first = requests.get(url(srv, f"/api/turn?id={turn}&after=-1&wait=2"), headers=auth, timeout=5).json()
    assert first["text"] in ("Hello", "Hello, the") and not first["done"]
    assert srv.busy
    assert requests.post(url(srv, "/api/send"), json={"text": "again"}, headers=auth, timeout=5).status_code == 409
    started = time.time()
    view = requests.get(url(srv, f"/api/turn?id={turn}&after=2&wait=0.3"), headers=auth, timeout=5).json()
    assert not view["done"] and 0.2 < time.time() - started < 2  # nothing new: the long poll waits, then returns
    hold.set()
    done = finish(srv, auth, turn)
    assert done["text"] == "Hello, the whole reply." and done["ask"] is None
    assert assistant.calls == [("hi jarvis", None)] and not srv.busy


def test_a_crash_is_told_not_hidden(make):
    class Broken:
        def send(self, *a, **k):
            raise RuntimeError("boom")

    srv, _ = make(Broken())
    auth = paired(srv)
    turn = srv.send("hi").id
    assert "went wrong" in finish(srv, auth, turn)["text"]


def test_gate_questions_come_back_with_yes_no_and_yes_runs_the_action(make, library):
    gate = PermissionGate(library)
    ran = []
    assistant = FakeAssistant("Shall I run dir?",
                              ask=lambda: gate.request("run_command", {"command": "dir"}, "run dir",
                                                       lambda: ran.append(1) or {"ok": True}, source="phone"))
    srv, _ = make(assistant, gate)
    auth = paired(srv)
    turn = requests.post(url(srv, "/api/send"), json={"text": "run dir"}, headers=auth, timeout=5).json()["turn"]
    ask = finish(srv, auth, turn)["ask"]
    assert ask["tool"] == "run_command" and ask["summary"] == "run dir"
    r = requests.post(url(srv, "/api/answer"), json={"request": ask["id"], "yes": True}, headers=auth, timeout=5)
    finish(srv, auth, r.json()["turn"])
    assert ran == [1]
    text, decided = assistant.calls[-1]
    assert text == "yes" and decided is not None
    again = requests.post(url(srv, "/api/answer"), json={"request": ask["id"], "yes": True}, headers=auth, timeout=5)
    assert again.status_code == 409 and ran == [1]  # an old question can't be answered twice


def test_no_declines_and_questions_asked_elsewhere_are_not_shown(make, library):
    gate = PermissionGate(library)
    ran = []
    srv, _ = make(FakeAssistant("Okay."), gate)
    auth = paired(srv)
    req = gate.request("run_command", {"command": "dir"}, "run dir", lambda: ran.append(1) or {}, source="chat")
    turn = srv.send("hello").id
    assert finish(srv, auth, turn)["ask"] is None  # the desktop's question isn't the phone's
    r = requests.post(url(srv, "/api/answer"), json={"request": req.id, "yes": False}, headers=auth, timeout=5)
    finish(srv, auth, r.json()["turn"])
    assert ran == [] and gate.pending is None


def test_old_turns_are_forgotten(make):
    srv, _ = make()
    auth = paired(srv)
    ids = []
    for i in range(25):
        ids.append(srv.send(f"m{i}").id)
        finish(srv, auth, ids[-1])
    assert srv.wait(ids[0], wait_s=0) is None and srv.wait(ids[-1], wait_s=0)["done"]


def test_busy_answers_are_refused(make, library):
    hold = threading.Event()
    srv, gate = make(FakeAssistant(hold=hold))
    srv.send("slow")
    with pytest.raises(Busy):
        srv.answer(1, True)
    hold.set()


# ---- the app's files and headers ----------------------------------------------------------------------------
def test_the_app_files_are_served_with_safe_headers(make):
    srv, _ = make()
    page = requests.get(url(srv, "/"), timeout=5)
    assert page.status_code == 200 and "<title>Jarvis</title>" in page.text
    assert page.headers["Content-Security-Policy"] == CSP and page.headers["X-Frame-Options"] == "DENY"
    assert page.headers["X-Content-Type-Options"] == "nosniff" and page.headers["Referrer-Policy"] == "no-referrer"
    for name, ctype in STATIC.items():
        r = requests.get(url(srv, "/" + name), timeout=5)
        assert r.status_code == 200 and r.headers["Content-Type"] == ctype and r.content == (APP_DIR / name).read_bytes()
    for bad in ("/../phone.py", "/%2e%2e/phone.py", "/phone.py", "/app.js/../../studio.py", "/nothing.js"):
        assert requests.get(url(srv, bad), timeout=5).status_code == 404
    assert requests.head(url(srv, "/app.js"), timeout=5).status_code == 200


def test_cross_site_calls_only_from_the_app_origin(make):
    srv, _ = make()
    ok = requests.options(url(srv, "/api/send"), headers={"Origin": ORIGIN}, timeout=5)
    assert ok.status_code == 204 and ok.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "Authorization" in ok.headers["Access-Control-Allow-Headers"]
    evil = requests.options(url(srv, "/api/send"), headers={"Origin": "https://evil.example"}, timeout=5)
    assert "Access-Control-Allow-Origin" not in evil.headers
    hello = requests.get(url(srv, "/api/hello"), headers={"Origin": "https://evil.example"}, timeout=5)
    assert "Access-Control-Allow-Origin" not in hello.headers


def test_the_app_page_and_script_agree():
    html = (APP_DIR / "index.html").read_text()
    js = (APP_DIR / "app.js").read_text()
    used = set(re.findall(r"\$\('([A-Za-z]+)'\)", js))
    ids = set(re.findall(r'id="([A-Za-z]+)"', html))
    assert used and used <= ids, used - ids
    assert "innerHTML" not in js and "eval(" not in js  # Jarvis's words are shown as text only
    for path in ("/api/pair", "/api/send", "/api/turn", "/api/answer", "/api/hello"):
        assert path in js
    manifest = json.loads((APP_DIR / "manifest.webmanifest").read_text())
    assert manifest["display"] == "standalone"
    for icon in manifest["icons"]:
        data = (APP_DIR / icon["src"]).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        assert f"{w}x{h}" == icon["sizes"]
    sw = (APP_DIR / "sw.js").read_text()
    assert "/api/" in sw and all(name in sw for name in ("index.html", "app.js", "style.css"))


# ---- link and settings line ---------------------------------------------------------------------------------
def test_phone_link_and_settings_line():
    assert phone_link(None) is None and phone_link("evil.example.com", "12345678") is None
    assert phone_link(HOST) == f"https://{HOST}/"
    assert phone_link(HOST, "12345678") == f"https://{HOST}/#c=12345678"
    link = phone_link(HOST, "12345678")
    assert link in phone_line({"running": True, "devices": 2}, {"host": HOST}, link)
    assert "2 phones paired" in phone_line({"running": True, "devices": 2}, {}, link)
    assert phone_line({"running": False}, {}, None) == "The phone app is turned off."
    assert "retrying" in phone_line({"running": True}, {"error": "no internet"}, None)
    assert "starting" in phone_line({"running": True}, {}, None)


# ---- tunnel -------------------------------------------------------------------------------------------------
def wait_until(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def fake_cloudflared(tmp_path, host, live_s=30.0):
    script = tmp_path / "cf.py"
    script.write_text("import sys, time\n"
                      "print('INF Requesting new quick Tunnel on trycloudflare.com...', flush=True)\n"
                      f"print('INF |  https://{host}  |', flush=True)\n"
                      f"time.sleep({live_s})\n")
    return [sys.executable, str(script)]


def test_tunnel_reports_its_address_and_restarts(tmp_path):
    seen = []
    tunnel = Tunnel(tmp_path, lambda: 8765, on_url=seen.append, command=fake_cloudflared(tmp_path, HOST, 0.3),
                    restart_s=0.05)
    tunnel.start()
    try:
        assert wait_until(lambda: seen.count(HOST) >= 2)  # it came back after the process ended
        assert None in seen
    finally:
        tunnel.stop()
    assert tunnel.host is None and not tunnel.running


def test_tunnel_waits_for_the_server_and_says_why(tmp_path):
    tunnel = Tunnel(tmp_path, lambda: None, command=["unused"], restart_s=10)
    tunnel.start()
    try:
        assert wait_until(lambda: tunnel.error is not None)
        assert "isn't running" in tunnel.error and tunnel.host is None
    finally:
        tunnel.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell script as the fake cloudflared")
def test_a_leftover_cloudflared_from_a_killed_jarvis_is_stopped(tmp_path):
    import subprocess
    fake = tmp_path / "cloudflared"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    old = subprocess.Popen([str(fake)])
    other = subprocess.Popen(["sleep", "30"])
    try:
        (tmp_path / "cloudflared.pid").write_text(str(other.pid))
        Tunnel(tmp_path, lambda: None, command=["unused"])._end_leftover()
        assert other.poll() is None  # not a cloudflared: left alone
        (tmp_path / "cloudflared.pid").write_text(str(old.pid))
        tunnel = Tunnel(tmp_path, lambda: None, command=["unused"], restart_s=10)
        tunnel.start()
        tunnel.stop()
        assert old.wait(5) is not None and not (tmp_path / "cloudflared.pid").exists()
    finally:
        for proc in (old, other):
            proc.kill()


def test_leftover_cleanup_on_windows_uses_taskkill_only_for_cloudflared(tmp_path, monkeypatch):
    import jarvis_books.phone as phone
    calls = []
    monkeypatch.setattr(phone, "WINDOWS", True)
    monkeypatch.setattr(phone.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or
                        type("R", (), {"stdout": '"cloudflared.exe","4242","Console"' if "tasklist" in cmd
                                       and "PID eq 4242" in cmd else 'INFO: No tasks'})())
    tunnel = Tunnel(tmp_path, lambda: None, command=["unused"])
    (tmp_path / "cloudflared.pid").write_text("4343")
    tunnel._end_leftover()
    assert all("taskkill" not in c for c in calls)
    (tmp_path / "cloudflared.pid").write_text("4242")
    tunnel._end_leftover()
    assert calls[-1] == ["taskkill", "/PID", "4242", "/F"] and not (tmp_path / "cloudflared.pid").exists()


class FakeDownload:
    def __init__(self, size):
        self.size, self.urls = size, []

    def get(self, u, stream=True, timeout=60):
        self.urls.append(u)
        size = self.size

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, n):
                left = size
                while left > 0:
                    yield b"\0" * min(n, left)
                    left -= n

        return Resp()


def test_cloudflared_is_downloaded_once_and_short_downloads_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis_books.phone.shutil.which", lambda name: None)
    short = FakeDownload(1000)
    with pytest.raises(RuntimeError):
        Tunnel(tmp_path / "bin", lambda: 1, session=short).binary()
    assert not list((tmp_path / "bin").glob("cloudflared*"))
    good = FakeDownload(6_000_000)
    tunnel = Tunnel(tmp_path / "bin", lambda: 1, session=good)
    path = tunnel.binary()
    assert path.stat().st_size == 6_000_000 and good.urls[0].endswith(cloudflared_asset())
    assert tunnel.binary() == path and len(good.urls) == 1


# ---- inside Jarvis ------------------------------------------------------------------------------------------
def test_studio_answers_on_the_phone_with_the_same_jarvis(tmp_path, monkeypatch):
    gemini = FakeGemini(lambda model, payload: body("Hello from Jarvis."))
    s = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=gemini,
               engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None}, local_client=FakeOllama())
    s.phone.wanted_port = 0
    s.tunnel.command = fake_cloudflared(tmp_path, HOST)
    told = []
    monkeypatch.setattr(s.telegram, "notify", lambda text: told.append(text) or True)
    s.start(probe=False)
    try:
        assert s.phone_info()["link"] is None and not s.phone.running
        s.set_setting("phone", "1")
        assert s.phone.running and wait_until(lambda: s.tunnel.host == HOST)
        assert told and f"https://{HOST}/#c=" in told[0]  # the new link is sent on Telegram
        s._on_phone_url(HOST)  # the same link again (a restarted Jarvis kept it): no repeat message
        assert len(told) == 1
        assert s.tools.phone_device is s.phone.device_link
        s.phone.pair(s.phone.pair_code(), "Pixel 8", kind="agent")
        s._on_phone_url("new-link-here.trycloudflare.com")  # laptop rebooted: the Android helper is told how to follow
        assert told[-1].endswith("paste this into Termux: jarvis-link new-link-here.trycloudflare.com")
        s._on_phone_url(HOST)
        told.pop()
        told.pop()
        info = s.phone_info()
        assert info["link"] == f"https://{HOST}/#c={info['code']}" and s.library.get_meta("phone.url") == HOST
        out = s.tools.call("phone_link", {})
        assert out["link"].startswith(f"https://{HOST}/#c=") and "phone_link" in PHONE_TOOLS
        auth = paired(s.phone)
        turn = requests.post(url(s.phone, "/api/send"), json={"text": "hi jarvis"}, headers=auth, timeout=5).json()
        assert finish(s.phone, auth, turn["turn"], timeout=15)["text"] == "Hello from Jarvis."
        said = [(m.channel, m.role, m.text) for m in s.library.recent_messages(("phone",))]
        assert said == [("phone", "user", "hi jarvis"), ("phone", "assistant", "Hello from Jarvis.")]
        assert "phone" in s.assistant.history_channels and "phone" in s.telegram_assistant.history_channels
        s.set_setting("phone", "0")
        assert not s.phone.running and s.tunnel.host is None
    finally:
        s.close()


def test_telegram_notify_needs_a_paired_running_bridge(library, monkeypatch):
    from test_telegram import KEY, FakeTelegram, msg
    from jarvis_books.telegram import TelegramBridge
    api = FakeTelegram()
    bridge = TelegramBridge(library, FakeAssistant(), PermissionGate(library), lambda: KEY, api_factory=lambda k: api)
    bridge.handle(api, msg(bridge.pair_code(), date=time.time()))
    api.sent.clear()
    assert not bridge.notify("hello")  # not running
    monkeypatch.setattr(TelegramBridge, "running", property(lambda self: True))
    assert bridge.notify("hello") and api.sent[-1]["text"] == "hello"


def test_jarvis_knows_when_to_hand_out_the_phone_link():
    import random
    from jarvis_books.assistant.permissions import READ, level_of
    from jarvis_books.llm.tool_data import load_templates, render
    mine = [t for t in load_templates() if t.tool == "phone_link"]
    assert len(mine) >= 5
    said = {render(t, random.Random(i)).request for i, t in enumerate(mine * 3)}
    assert any("phone link" in s for s in said) and any("on my phone" in s for s in said)
    assert level_of("phone_link") == READ  # it only shows a link; nothing changes, no yes needed


class HelloSession:
    def __init__(self, ok=True):
        self.ok, self.urls = ok, []

    def get(self, url, timeout=None):
        self.urls.append(url)
        if not self.ok:
            raise requests.ConnectionError("no route")
        return type("R", (), {"status_code": 200, "json": lambda self: {"app": "jarvis"}})()


@pytest.fixture
def tracked(monkeypatch):
    """Fake cloudflared processes the tunnel can recognise and stop on any platform."""
    import jarvis_books.phone as phone_mod
    launched = {}
    real_launch = Tunnel._launch

    def launch(self, port):
        proc = real_launch(self, port)
        launched[proc.pid] = proc
        return proc

    def kill(pid):
        proc = launched.get(pid)
        if proc is None or proc.poll() is not None:
            return False
        proc.terminate()
        proc.wait(5)
        return True
    monkeypatch.setattr(Tunnel, "_launch", launch)
    monkeypatch.setattr(phone_mod, "_is_cloudflared", lambda pid: pid in launched and launched[pid].poll() is None)
    monkeypatch.setattr(phone_mod, "_kill_cloudflared", kill)
    yield launched
    for proc in launched.values():
        if proc.poll() is None:
            proc.kill()
            proc.wait(5)


def test_a_restarted_jarvis_keeps_the_same_link(tmp_path, tracked):
    first = Tunnel(tmp_path, lambda: 8765, command=fake_cloudflared(tmp_path, HOST))
    first.start()
    assert wait_until(lambda: first.host == HOST)
    pid = first.pid
    first.stop(keep=True)
    assert tracked[pid].poll() is None and json.loads((tmp_path / "cloudflared.json").read_text())["host"] == HOST
    seen, hello = [], HelloSession()
    second = Tunnel(tmp_path, lambda: 8765, on_url=seen.append, command=["no-such-cloudflared"], session=hello)
    second.start()
    try:
        assert wait_until(lambda: second.host == HOST) and seen == [HOST]
        assert second.pid == pid and len(tracked) == 1 and hello.urls == [f"https://{HOST}/api/hello"]
    finally:
        second.stop()
    assert tracked[pid].poll() is not None and not (tmp_path / "cloudflared.json").exists()
    assert seen == [HOST, None] and second.host is None


@pytest.mark.parametrize("port, reachable", [(9999, True), (8765, False)])
def test_a_kept_link_on_another_port_or_unreachable_is_replaced(tmp_path, tracked, port, reachable):
    first = Tunnel(tmp_path, lambda: 8765, command=fake_cloudflared(tmp_path, HOST))
    first.start()
    assert wait_until(lambda: first.host == HOST)
    old = first.pid
    first.stop(keep=True)
    other = "fresh-link-here.trycloudflare.com"
    second = Tunnel(tmp_path, lambda: port, command=fake_cloudflared(tmp_path, other), session=HelloSession(reachable))
    second.start()
    try:
        assert wait_until(lambda: second.host == other)
        assert tracked[old].poll() is not None and second.pid != old
        assert json.loads((tmp_path / "cloudflared.json").read_text()) == {"pid": second.pid, "port": port,
                                                                           "host": other}
    finally:
        second.stop()


def test_a_dead_kept_tunnel_is_started_fresh(tmp_path, tracked):
    (tmp_path / "cloudflared.json").write_text(json.dumps({"pid": 999999, "port": 8765, "host": HOST}))
    tunnel = Tunnel(tmp_path, lambda: 8765, command=fake_cloudflared(tmp_path, "new-one.trycloudflare.com"),
                    session=HelloSession())
    tunnel.start()
    try:
        assert wait_until(lambda: tunnel.host == "new-one.trycloudflare.com") and len(tracked) == 1
    finally:
        tunnel.stop()
