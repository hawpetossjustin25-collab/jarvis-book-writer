"""Jarvis's hands on an Android phone. Runs in Termux (Python standard library only).

It pairs once with the Jarvis on the laptop, then long-polls the laptop for commands through the same Cloudflare
link the phone app uses, runs each one with Termux:API or with adb (Android's wireless debugging, connected to this
same phone), and posts the result back. Only the actions in ACTIONS can run; the laptop's yes/no gate has already
approved anything that changes something. Settings live in ~/.jarvis/agent.json (token, link)."""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional

VERSION = 1
HOME = Path(os.environ.get("JARVIS_AGENT_HOME", str(Path.home() / ".jarvis")))
CONFIG = HOME / "agent.json"
HOST_RE = re.compile(r"^[a-z0-9-]+\.trycloudflare\.com$")
MAX_OUT = 20_000
MAX_SHOT = 4_000_000
KEYS = {"home": 3, "back": 4, "call": 5, "end_call": 6, "up": 19, "down": 20, "left": 21, "right": 22,
        "volume_up": 24, "volume_down": 25, "power": 26, "camera": 27, "enter": 66, "delete": 67, "menu": 82,
        "search": 84, "play_pause": 85, "next": 87, "previous": 88, "mute": 164, "recents": 187, "wake": 224,
        "sleep": 223}
APP_NAMES = {"messenger": "com.facebook.orca", "facebook": "com.facebook.katana", "gmail": "com.google.android.gm",
             "maps": "com.google.android.apps.maps", "messages": "com.google.android.apps.messaging",
             "phone": "com.google.android.dialer", "photos": "com.google.android.apps.photos",
             "play store": "com.android.vending", "settings": "com.android.settings", "whatsapp": "com.whatsapp",
             "instagram": "com.instagram.android", "tiktok": "com.zhiliaoapp.musically", "termux": "com.termux",
             "camera": "com.android.camera2", "chrome": "com.android.chrome", "youtube": "com.google.android.youtube"}
SWIPES = {"up": (0.5, 0.75, 0.5, 0.25), "down": (0.5, 0.25, 0.5, 0.75), "left": (0.8, 0.5, 0.2, 0.5),
          "right": (0.2, 0.5, 0.8, 0.5)}

Runner = Callable[[list, float, Optional[str]], tuple]


def run(cmd: list, timeout: float = 30.0, stdin: Optional[str] = None) -> tuple[int, str, bytes]:
    """(exit code, text output, raw stdout) of a command, never raising for a failed command."""
    try:
        p = subprocess.run(cmd, input=stdin.encode() if stdin is not None else None, capture_output=True,
                           timeout=timeout)
    except FileNotFoundError:
        return 127, f"{cmd[0]} isn't installed", b""
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f} s", b""
    text = (p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")).strip()
    return p.returncode, text[-MAX_OUT:], p.stdout


class ActionError(Exception):
    pass


class Phone:
    """The actions Jarvis may run here."""

    def __init__(self, runner: Runner = run):
        self.run = runner

    # ---- helpers
    def _ok(self, cmd: list, timeout: float = 30.0, stdin: Optional[str] = None) -> str:
        code, out, _ = self.run(cmd, timeout, stdin)
        if code != 0:
            raise ActionError(out or f"{cmd[0]} failed ({code})")
        return out

    def _json(self, cmd: list, timeout: float = 30.0):
        out = self._ok(cmd, timeout)
        try:
            return json.loads(out or "null")
        except ValueError:
            raise ActionError(f"{cmd[0]} gave an answer I couldn't read: {out[:200]}")

    def adb_ready(self) -> bool:
        code, out, _ = self.run(["adb", "devices"], 15, None)
        return code == 0 and any(line.endswith("\tdevice") for line in out.splitlines()[1:])

    def _adb(self, *args: str, timeout: float = 30.0) -> str:
        if not self.adb_ready():
            raise ActionError("screen control needs wireless debugging connected; ask Jarvis to pair it")
        return self._ok(["adb", "shell", *args], timeout)

    def _screen_size(self) -> tuple[int, int]:
        m = re.search(r"(\d+)x(\d+)", self._adb("wm", "size"))
        return (int(m.group(1)), int(m.group(2))) if m else (1080, 2340)

    # ---- actions
    def info(self) -> dict:
        prop = lambda k: self.run(["getprop", k], 10, None)[1]
        code, out, _ = self.run(["termux-battery-status"], 15, None)
        battery = json.loads(out).get("percentage") if code == 0 and out.startswith("{") else None
        return {"agent": VERSION, "model": prop("ro.product.model"), "android": prop("ro.build.version.release"),
                "termux_api": code == 0, "battery": battery, "screen_control": self.adb_ready()}

    def send_text(self, to: str, message: str) -> dict:
        if not to or not message:
            raise ActionError("need a number and a message")
        self._ok(["termux-sms-send", "-n", to, message], 30)
        return {"sent": True, "to": to}

    def read_texts(self, limit: int = 10, box: str = "inbox") -> dict:
        box = box if box in ("all", "inbox", "sent", "draft", "outbox") else "inbox"
        items = self._json(["termux-sms-list", "-l", str(max(1, min(int(limit or 10), 50))), "-t", box]) or []
        return {"texts": [{k: m.get(k) for k in ("number", "received", "body", "type", "read")} for m in items]}

    def call(self, number: str) -> dict:
        if not re.fullmatch(r"[+(\d][\d\s().-]{2,20}", number or ""):
            raise ActionError("that doesn't look like a phone number")
        self._ok(["termux-telephony-call", re.sub(r"[\s().-]", "", number)], 20)
        return {"calling": number}

    def contacts(self, query: str = "") -> dict:
        items = self._json(["termux-contact-list"], 60) or []
        q = (query or "").lower()
        found = [{"name": c.get("name"), "number": c.get("number")} for c in items
                 if not q or q in (c.get("name") or "").lower() or q in (c.get("number") or "")]
        return {"contacts": found[:25], "total": len(found)}

    def notifications(self) -> dict:
        items = self._json(["termux-notification-list"], 30) or []
        return {"notifications": [{k: n.get(k) for k in ("packageName", "title", "content", "when")}
                                  for n in items][:30]}

    def clipboard(self, text: Optional[str] = None) -> dict:
        if text is None:
            return {"clipboard": self._ok(["termux-clipboard-get"], 15)}
        self._ok(["termux-clipboard-set"], 15, text)
        return {"copied": len(text)}

    def location(self) -> dict:
        return {"location": self._json(["termux-location", "-p", "network", "-r", "once"], 60)}

    def shell(self, command: str, timeout: int = 60) -> dict:
        if not (command or "").strip():
            raise ActionError("no command")
        code, out, _ = self.run(["bash", "-lc", command], float(max(1, min(int(timeout or 60), 600))), None)
        return {"exit_code": code, "output": out}

    def open_url(self, url: str) -> dict:
        if not re.match(r"^https?://", url or ""):
            raise ActionError("only http and https links")
        self._ok(["termux-open-url", url], 20)
        return {"opened": url}

    def apps(self, query: str = "") -> dict:
        out = self._adb("pm", "list", "packages", "-3")
        names = sorted(line.split(":", 1)[1] for line in out.splitlines() if line.startswith("package:"))
        q = (query or "").lower()
        return {"apps": [n for n in names if q in n.lower()][:100]}

    def open_app(self, app: str) -> dict:
        app = (app or "").strip()
        if not re.fullmatch(r"[\w .-]{1,80}", app):
            raise ActionError("give an app name or package")
        if "." in app and " " not in app:
            package = app
        else:
            out = self._adb("pm", "list", "packages")
            installed = [line.split(":", 1)[1] for line in out.splitlines() if line.startswith("package:")]
            known = APP_NAMES.get(app.lower())
            want = re.sub(r"\W", "", app.lower())
            matches = [known] if known in installed else \
                [p for p in installed if want in re.sub(r"\W", "", p.lower())]
            if not matches:
                raise ActionError(f"no app matching {app!r}")
            package = min(matches, key=len)
        self._adb("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
        return {"opened": package}

    def screen(self, picture: bool = False) -> dict:
        self._adb("uiautomator", "dump", "/sdcard/jarvis_ui.xml", timeout=30)
        xml = self._adb("cat", "/sdcard/jarvis_ui.xml")
        out = {"elements": ui_elements(xml)}
        if picture:
            code, _, raw = self.run(["adb", "exec-out", "screencap", "-p"], 30, None)
            if code == 0 and raw.startswith(b"\x89PNG") and len(raw) <= MAX_SHOT:
                out["png"] = base64.b64encode(raw).decode()
        return out

    def tap(self, x: Optional[int] = None, y: Optional[int] = None, text: str = "") -> dict:
        if text:
            want = text.strip().lower()
            self._adb("uiautomator", "dump", "/sdcard/jarvis_ui.xml", timeout=30)
            items = ui_elements(self._adb("cat", "/sdcard/jarvis_ui.xml"))
            hits = [e for e in items if want == (e["text"] or e["desc"]).lower()] or \
                   [e for e in items if want in (e["text"] + " " + e["desc"]).lower()]
            if not hits:
                raise ActionError(f"nothing on the screen says {text!r}")
            hit = next((e for e in hits if e["clickable"]), hits[0])
            x, y = hit["x"], hit["y"]
        if x is None or y is None:
            raise ActionError("give the words to tap or x and y")
        self._adb("input", "tap", str(int(x)), str(int(y)))
        return {"tapped": [int(x), int(y)]}

    def type_text(self, text: str) -> dict:
        if not text:
            raise ActionError("nothing to type")
        for part in re.split(r"(\n)", text[:2000]):
            if part == "\n":
                self._adb("input", "keyevent", "66")
            elif part:
                self._adb("input", "text", shlex.quote(part.replace(" ", "%s")))
        return {"typed": len(text)}

    def swipe(self, direction: str = "up") -> dict:
        if direction not in SWIPES:
            raise ActionError("swipe up, down, left or right")
        w, h = self._screen_size()
        a, b, c, d = SWIPES[direction]
        self._adb("input", "swipe", str(int(a * w)), str(int(b * h)), str(int(c * w)), str(int(d * h)), "300")
        return {"swiped": direction}

    def key(self, key: str) -> dict:
        code = KEYS.get((key or "").lower().replace(" ", "_"))
        if code is None:
            raise ActionError("keys: " + ", ".join(sorted(KEYS)))
        self._adb("input", "keyevent", str(code))
        return {"pressed": key}

    def pair_debugging(self, pair_port: int, code: str, connect_port: Optional[int] = None) -> dict:
        if not re.fullmatch(r"\d{6}", str(code or "")):
            raise ActionError("the pairing code is the 6 digits on the Wireless debugging screen")
        out = self._ok(["adb", "pair", f"127.0.0.1:{int(pair_port)}", str(code)], 30)
        if "success" not in out.lower():
            raise ActionError(out or "pairing failed")
        return self.connect_debugging(connect_port)

    def connect_debugging(self, port: Optional[int] = None) -> dict:
        ports = [int(port)] if port else self._connect_ports()
        for p in ports:
            out = self._ok(["adb", "connect", f"127.0.0.1:{p}"], 20)
            if "connected" in out.lower() and self.adb_ready():
                return {"screen_control": True, "port": p}
        raise ActionError("couldn't connect; give the port shown under 'IP address & Port' on Wireless debugging")

    def _connect_ports(self) -> list[int]:
        code, out, _ = self.run(["adb", "mdns", "services"], 15, None)
        return [int(p) for p in re.findall(r"_adb-tls-connect\._tcp\.?\s+\S+:(\d+)", out)] if code == 0 else []


def ui_elements(xml: str, limit: int = 120) -> list[dict]:
    """The visible, meaningful parts of a uiautomator dump: words, ids, whether they're tappable and their centre."""
    try:
        root = ET.fromstring(xml[xml.find("<"):] if "<" in xml else "")
    except ET.ParseError:
        return []
    out = []
    for node in root.iter("node"):
        text, desc = (node.get("text") or "").strip(), (node.get("content-desc") or "").strip()
        clickable = node.get("clickable") == "true"
        m = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds") or "")
        if not m or not (text or desc or (clickable and node.get("resource-id"))):
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        out.append({"text": text[:120], "desc": desc[:120], "id": (node.get("resource-id") or "").split("/")[-1],
                    "clickable": clickable, "x": (x1 + x2) // 2, "y": (y1 + y2) // 2})
        if len(out) >= limit:
            break
    return out


ACTIONS = {"info": "info", "send_text": "send_text", "read_texts": "read_texts", "call": "call",
           "contacts": "contacts", "notifications": "notifications", "clipboard": "clipboard",
           "location": "location", "shell": "shell", "open_url": "open_url", "apps": "apps", "open_app": "open_app",
           "screen": "screen", "tap": "tap", "type": "type_text", "swipe": "swipe", "key": "key",
           "pair_debugging": "pair_debugging", "connect_debugging": "connect_debugging"}


def execute(phone: Phone, action: str, args: dict) -> dict:
    """{"ok": True, "result": ...} or {"ok": False, "error": "..."}; unknown actions are refused."""
    method = ACTIONS.get(action)
    if method is None:
        return {"ok": False, "error": f"this phone doesn't do {action!r}"}
    try:
        return {"ok": True, "result": getattr(phone, method)(**(args or {}))}
    except ActionError as exc:
        return {"ok": False, "error": str(exc)}
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"bad request: {exc}"}


# ---- talking to Jarvis --------------------------------------------------------------------------------------
class Link:
    def __init__(self, host: str, token: str = "", opener=urllib.request.urlopen):
        if not HOST_RE.match(host or ""):
            raise ValueError("not a Jarvis link")
        self.base, self.token, self.opener = f"https://{host}", token, opener

    def request(self, path: str, body: Optional[dict] = None, timeout: float = 40.0) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET")
        req.add_header("User-Agent", f"jarvis-agent/{VERSION}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        with self.opener(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg))
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)


def pair(host: str, code: str, name: str = "", opener=urllib.request.urlopen) -> dict:
    try:
        got = Link(host, opener=opener).request("/api/pair", {"code": code, "name": name or "android",
                                                              "kind": "agent"})
    except urllib.error.HTTPError as exc:
        if exc.code != 403:
            raise
        got = {}
    if not got.get("token"):
        raise RuntimeError("Jarvis refused the pairing code; ask him for a new phone setup command")
    cfg = {"host": host, "token": got["token"]}
    save_config(cfg)
    return cfg


def serve(link: Link, phone: Phone, stop: Callable[[], bool] = lambda: False, sleep=time.sleep,
          wait_s: int = 25) -> None:
    """Fetch commands and answer them until stop() says so. Network trouble backs off and retries."""
    backoff, said_hello = 2.0, False
    while not stop():
        try:
            if not said_hello:
                link.request("/api/agent/hello", {"info": phone.info()})
                said_hello = True
            got = link.request(f"/api/agent/next?wait={wait_s}", timeout=wait_s + 15)
            backoff = 2.0
            cmd = got.get("command")
            if not cmd:
                continue
            outcome = execute(phone, str(cmd.get("action")), cmd.get("args") or {})
            link.request("/api/agent/result", {"id": cmd.get("id"), **outcome}, timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise SystemExit("Jarvis no longer knows this phone; run the setup command again")
            said_hello = False
            sleep(backoff)
            backoff = min(backoff * 2, 120)
        except (urllib.error.URLError, OSError, ValueError):
            said_hello = False
            sleep(backoff)
            backoff = min(backoff * 2, 120)


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "pair":
        cfg = pair(argv[1], argv[2], argv[3] if len(argv) > 3 else "")
        print("Paired with Jarvis.")
        return 0 if cfg else 1
    cfg = load_config()
    if not cfg.get("token"):
        print("Not paired yet: run the setup command Jarvis gave you.")
        return 1
    if len(argv) >= 2 and argv[0] == "host":  # Jarvis's link changed (the laptop restarted)
        Link(argv[1])
        cfg["host"] = argv[1]
        save_config(cfg)
        print("Saved Jarvis's new link.")
        return 0
    serve(Link(cfg["host"], cfg["token"]), Phone())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
