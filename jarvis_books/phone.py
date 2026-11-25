"""Jarvis on the phone: a home-screen web app that talks to the same Jarvis as the desktop app.

Two parts run inside Jarvis:

* `PhoneServer` is a small HTTP server on 127.0.0.1. It serves the phone app's files (jarvis_books/phone_app) and a
  JSON API. A phone pairs once with an 8-digit code and gets a long random token. Only the token's hash is saved.
  Each message is one "turn" that runs through the same Assistant, tools, memory and yes/no gate as everywhere
  else. The phone long-polls the turn to show the reply while it is written, and answers gate questions with
  Yes/No.
* `Tunnel` runs Cloudflare's free quick tunnel (cloudflared). It connects outward and gives the server a public
  https://<random>.trycloudflare.com address, so nothing on the router is opened. The address changes when the
  tunnel restarts; Jarvis then shows and sends (on Telegram) the new link.

The phone link opens the app straight from the tunnel address with a one-time pairing code in the part after
"#", which browsers never send to a server. The app also runs from a static copy (for example GitHub Pages, origin
PAGES_ORIGIN) and then remembers which tunnel to talk to; opening a fresh link updates it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, urlsplit

import requests

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).with_name("phone_app")
PAGES_ORIGIN = "https://hawpetossjustin25-collab.github.io"
PORT = 8765
MAX_BODY = 64 * 1024
MAX_TEXT = 4000
MAX_DEVICES = 5
PAIR_TRIES = 5
TURN_WAIT_S = 25.0  # one long poll; Cloudflare closes idle requests after 100 s
KEEP_TURNS = 20
PAIR, PAIR_MISSES, DEVICES, URL_META = "phone.pair", "phone.pair_misses", "phone.devices", "phone.url"
TUNNEL_RE = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)\b")
STATIC = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
          "style.css": "text/css; charset=utf-8", "sw.js": "text/javascript; charset=utf-8",
          "manifest.webmanifest": "application/manifest+json", "icon-192.png": "image/png",
          "icon-512.png": "image/png"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; manifest-src 'self'; "
       "connect-src 'self' https://*.trycloudflare.com; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Busy(Exception):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def phone_link(host: Optional[str], code: str = "") -> Optional[str]:
    """The link to open on the phone: the app served through the tunnel, with a pairing code after the '#'."""
    if not host or not re.fullmatch(r"[a-z0-9-]+\.trycloudflare\.com", host):
        return None
    return f"https://{host}/#c={quote(code)}" if code else f"https://{host}/"


@dataclass
class Turn:
    id: str
    text: str = ""
    reply: str = ""
    done: bool = False
    version: int = 0
    ask: Optional[dict] = None
    started: float = field(default_factory=time.time)

    def view(self) -> dict:
        return {"id": self.id, "text": self.reply, "done": self.done, "version": self.version, "ask": self.ask}


class PhoneServer:
    """Serves the phone app and runs its messages through `assistant.send`, one turn at a time."""

    def __init__(self, library, assistant, gate, port: int = PORT, host: str = "127.0.0.1",
                 clock: Callable[[], float] = time.time, turn_wait_s: float = TURN_WAIT_S,
                 allowed_origins: tuple[str, ...] = (PAGES_ORIGIN,), app_dir: Path = APP_DIR):
        self.library, self.assistant, self.gate = library, assistant, gate
        self.wanted_port, self.host, self.clock = port, host, clock
        self.turn_wait_s, self.allowed_origins, self.app_dir = turn_wait_s, allowed_origins, app_dir
        self.port: Optional[int] = None
        self.error: Optional[str] = None
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._turns: dict[str, Turn] = {}
        self._current: Optional[Turn] = None
        self._turn_thread: Optional[int] = None
        self._asked = None
        gate.subscribe(self._on_gate)

    # ---- running ----------------------------------------------------------------------------------------
    def start(self) -> bool:
        if self._httpd is not None:
            return True
        handler = _handler_for(self)
        for port in (self.wanted_port, 0):
            try:
                self._httpd = ThreadingHTTPServer((self.host, port), handler)
                break
            except OSError as exc:
                log.warning("phone: port %s is taken (%s)", port, exc)
        if self._httpd is None:
            self.error = "Couldn't start the phone server."
            return False
        self._httpd.daemon_threads = True
        self.port, self.error = self._httpd.server_address[1], None
        threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.5}, name="jarvis-phone",
                         daemon=True).start()
        log.info("phone: serving on %s:%s", self.host, self.port)
        return True

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        self.port = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    # ---- pairing ----------------------------------------------------------------------------------------
    def pair_code(self) -> str:
        code = self.library.get_meta(PAIR, "")
        if not code:
            code = f"{secrets.randbelow(10 ** 8):08d}"
            self.library.set_meta(PAIR, code)
            self.library.set_meta(PAIR_MISSES, "0")
        return code

    def devices(self) -> list[dict]:
        try:
            found = json.loads(self.library.get_meta(DEVICES, "") or "[]")
        except ValueError:
            return []
        return [d for d in found if isinstance(d, dict) and isinstance(d.get("hash"), str)]

    def _save_devices(self, devices: list[dict]) -> None:
        self.library.set_meta(DEVICES, json.dumps(devices[-MAX_DEVICES:]))

    def pair(self, code: str, name: str = "") -> Optional[str]:
        """A new device token for the right code (the code is then used up); None for a wrong one."""
        with self._lock:
            if not re.fullmatch(r"\d{8}", code or "") or not secrets.compare_digest(code, self.pair_code()):
                misses = int(self.library.get_meta(PAIR_MISSES, "0") or 0) + 1
                self.library.set_meta(PAIR_MISSES, str(misses))
                if misses >= PAIR_TRIES:
                    self.library.set_meta(PAIR, "")
                    log.warning("phone: %s wrong pairing codes, made a new code", misses)
                return None
            token = secrets.token_urlsafe(32)
            devices = self.devices() + [{"hash": _hash(token), "name": (name or "phone")[:40],
                                         "added": self.clock(), "seen": self.clock()}]
            self._save_devices(devices)
            self.library.set_meta(PAIR, "")
            log.info("phone: paired a device")
            return token

    def device_for(self, token: Optional[str]) -> Optional[dict]:
        if not token:
            return None
        h = _hash(token)
        for d in self.devices():
            if secrets.compare_digest(d["hash"], h):
                return d
        return None

    def unpair(self, token: str) -> bool:
        with self._lock:
            h, devices = _hash(token), self.devices()
            kept = [d for d in devices if d["hash"] != h]
            self._save_devices(kept)
            return len(kept) < len(devices)

    def forget_devices(self) -> None:
        self.library.set_meta(DEVICES, "[]")

    # ---- turns ------------------------------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._current is not None and not self._current.done

    def send(self, text: str, decided: Optional[dict] = None) -> Turn:
        with self._lock:
            if self.busy:
                raise Busy()
            turn = Turn(secrets.token_hex(8), text=text, started=self.clock())
            self._turns[turn.id] = self._current = turn
            for old in sorted(self._turns.values(), key=lambda t: t.started)[:-KEEP_TURNS]:
                self._turns.pop(old.id, None)
        threading.Thread(target=self._run, args=(turn, decided), name="jarvis-phone-turn", daemon=True).start()
        return turn

    def _run(self, turn: Turn, decided: Optional[dict]) -> None:
        def on_text(partial: str) -> None:
            with self._changed:
                if partial and partial != turn.reply:
                    turn.reply, turn.version = partial, turn.version + 1
                    self._changed.notify_all()

        self._asked, self._turn_thread = None, threading.get_ident()
        try:
            reply = self.assistant.send(turn.text, on_text=on_text, decided=decided)
        except Exception:
            log.exception("phone turn failed")
            reply = "Something went wrong on my side. Try again in a moment."
        finally:
            self._turn_thread = None
            asked, self._asked = self._asked, None
        with self._changed:
            pending = self.gate.pending
            if asked is not None and pending is asked:
                turn.ask = {"id": asked.id, "tool": asked.tool, "summary": asked.summary}
            turn.reply, turn.done, turn.version = reply or "(done)", True, turn.version + 1
            self._changed.notify_all()

    def wait(self, turn_id: str, after: int = -1, wait_s: Optional[float] = None) -> Optional[dict]:
        """The turn once it has moved past version `after` (or is done), waiting up to wait_s."""
        deadline = time.monotonic() + max(0.0, min(self.turn_wait_s if wait_s is None else wait_s, self.turn_wait_s))
        with self._changed:
            turn = self._turns.get(turn_id)
            if turn is None:
                return None
            while turn.version <= after and not turn.done:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._changed.wait(left)
            return turn.view()

    def answer(self, request_id: int, yes: bool) -> Optional[Turn]:
        if self.busy:
            raise Busy()
        outcome = self.gate.answer(yes, request_id, via="phone")
        if outcome is None:
            return None
        return self.send("yes" if yes else "no", decided=outcome)

    def _on_gate(self, event: str, info: dict) -> None:
        if event == "asking" and self._turn_thread == threading.get_ident():
            self._asked = info.get("request")

    def status(self) -> dict:
        return {"running": self.running, "port": self.port, "devices": len(self.devices()), "busy": self.busy,
                "error": self.error}


def _handler_for(server: PhoneServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Jarvis"
        sys_version = ""

        def log_message(self, fmt, *args) -> None:  # paths only; tokens travel in headers, never in URLs
            log.debug("phone: %s %s", self.command, urlsplit(self.path).path)

        # ---- helpers ----
        def _cors(self) -> None:
            origin = self.headers.get("Origin", "")
            if origin in server.allowed_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data).encode(), "application/json", {"Cache-Control": "no-store"})

        def _body(self) -> Optional[dict]:
            try:
                size = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                size = -1
            if size < 0 or size > MAX_BODY:
                self._json(413, {"error": "too big"})
                return None
            try:
                data = json.loads(self.rfile.read(size) or b"{}")
            except ValueError:
                self._json(400, {"error": "not JSON"})
                return None
            if not isinstance(data, dict):
                self._json(400, {"error": "expected a JSON object"})
                return None
            return data

        def _token(self) -> Optional[str]:
            auth = self.headers.get("Authorization", "")
            return auth[7:].strip() if auth.startswith("Bearer ") else None

        def _device(self) -> Optional[dict]:
            device = server.device_for(self._token())
            if device is None:
                self._json(401, {"error": "not paired"})
            return device

        # ---- methods ----
        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._cors()
            if self.headers.get("Origin", "") in server.allowed_origins:
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            try:
                if url.path == "/api/hello":
                    token = self._token()
                    self._json(200, {"app": "jarvis", "version": 1, "busy": server.busy,
                                     "paired": server.device_for(token) is not None if token else False})
                elif url.path == "/api/turn":
                    if self._device() is None:
                        return
                    q = parse_qs(url.query)
                    try:
                        after = int(q.get("after", ["-1"])[0])
                        wait = float(q.get("wait", [str(server.turn_wait_s)])[0])
                    except ValueError:
                        self._json(400, {"error": "bad number"})
                        return
                    view = server.wait(q.get("id", [""])[0], after, wait)
                    self._json(200, view) if view is not None else self._json(404, {"error": "no such turn"})
                else:
                    self._static(url.path)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            try:
                data = self._body()
                if data is None:
                    return
                if path == "/api/pair":
                    token = server.pair(str(data.get("code", "")).strip(), str(data.get("name", "")))
                    self._json(200, {"token": token}) if token else self._json(403, {"error": "wrong code"})
                    return
                if self._device() is None:
                    return
                if path == "/api/send":
                    text = str(data.get("text", "")).strip()
                    if not text or len(text) > MAX_TEXT:
                        self._json(400, {"error": f"send 1 to {MAX_TEXT} characters"})
                        return
                    self._json(200, {"turn": server.send(text).id})
                elif path == "/api/answer":
                    try:
                        rid = int(data.get("request"))
                    except (TypeError, ValueError):
                        self._json(400, {"error": "request must be a number"})
                        return
                    turn = server.answer(rid, bool(data.get("yes")))
                    self._json(200, {"turn": turn.id}) if turn else self._json(409, {"error": "already closed"})
                elif path == "/api/unpair":
                    self._json(200, {"unpaired": server.unpair(self._token() or "")})
                else:
                    self._json(404, {"error": "not found"})
            except Busy:
                self._json(409, {"error": "Jarvis is still answering the last message"})
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _static(self, path: str) -> None:
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            ctype = STATIC.get(name)
            file = server.app_dir / name
            if ctype is None or not file.is_file():
                self._json(404, {"error": "not found"})
                return
            extra = {"Cache-Control": "no-cache"}
            if name == "index.html":
                extra.update({"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"})
            self._send(200, file.read_bytes(), ctype, extra)

    return Handler


# ---- Cloudflare quick tunnel ------------------------------------------------------------------------------
RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
MIN_BINARY = 5_000_000


def cloudflared_asset() -> str:
    system, machine = platform.system().lower(), platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64" if machine in ("x86_64", "amd64") else "386"
    if system == "windows":
        return f"cloudflared-windows-{arch}.exe"
    if system == "linux":
        return f"cloudflared-linux-{arch}"
    raise RuntimeError(f"no cloudflared download for {platform.system()}")


class Tunnel:
    """Keeps `cloudflared tunnel --url http://127.0.0.1:<port>` running and reports its public address."""

    def __init__(self, bin_dir: Path, port: Callable[[], Optional[int]],
                 on_url: Optional[Callable[[Optional[str]], None]] = None, command: Optional[list[str]] = None,
                 session: Optional[requests.Session] = None, restart_s: float = 5.0, max_restart_s: float = 300.0):
        self.bin_dir, self.port, self.on_url = Path(bin_dir), port, on_url
        self.command, self.session = command, session or requests.Session()
        self.restart_s, self.max_restart_s = restart_s, max_restart_s
        self.host: Optional[str] = None
        self.error: Optional[str] = None
        self._stop: Optional[threading.Event] = None
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def binary(self) -> Path:
        name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
        local = self.bin_dir / name
        if local.is_file() and local.stat().st_size >= MIN_BINARY:
            return local
        found = shutil.which("cloudflared")
        if found:
            return Path(found)
        return self._download(local)

    def _download(self, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(target.suffix + ".part")
        log.info("tunnel: downloading cloudflared")
        with self.session.get(RELEASES + cloudflared_asset(), stream=True, timeout=60) as resp:
            resp.raise_for_status()
            size = 0
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(1 << 16):
                    size += len(chunk)
                    if size > 200_000_000:
                        raise RuntimeError("cloudflared download is far too big")
                    fh.write(chunk)
        if size < MIN_BINARY:
            part.unlink(missing_ok=True)
            raise RuntimeError("cloudflared download was cut short")
        if os.name != "nt":
            part.chmod(0o755)
        os.replace(part, target)
        return target

    @property
    def pid_file(self) -> Path:
        return self.bin_dir / "cloudflared.pid"

    def _end_leftover(self) -> None:
        """Stops a cloudflared this Jarvis started before it was killed, so old links don't linger."""
        try:
            pid = int(self.pid_file.read_text().strip())
        except (OSError, ValueError):
            return
        self.pid_file.unlink(missing_ok=True)
        if not _is_cloudflared(pid):
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.kill(pid, 15)
            log.info("tunnel: stopped a leftover cloudflared")
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("tunnel: couldn't stop a leftover cloudflared (%s)", exc)

    def start(self) -> None:
        with self._lock:
            if self._stop is not None and not self._stop.is_set():
                return
            self._end_leftover()
            self._stop = threading.Event()
            threading.Thread(target=self._run, args=(self._stop,), name="jarvis-tunnel", daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            if self._stop is not None:
                self._stop.set()
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.pid_file.unlink(missing_ok=True)
        self._set(None)

    @property
    def running(self) -> bool:
        return self._stop is not None and not self._stop.is_set()

    def _set(self, host: Optional[str]) -> None:
        if host == self.host:
            return
        self.host = host
        if self.on_url is not None:
            try:
                self.on_url(host)
            except Exception:
                log.exception("tunnel address listener failed")

    def _run(self, stop: threading.Event) -> None:
        backoff = self.restart_s
        while not stop.is_set():
            started = time.monotonic()
            try:
                port = self.port()
                if not port:
                    raise RuntimeError("the phone server isn't running")
                cmd = self.command or [str(self.binary()), "tunnel", "--no-autoupdate", "--url",
                                       f"http://127.0.0.1:{port}"]
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        text=True, encoding="utf-8", errors="replace", creationflags=flags)
                with self._lock:
                    self._proc = proc
                try:
                    self.bin_dir.mkdir(parents=True, exist_ok=True)
                    self.pid_file.write_text(str(proc.pid))
                except OSError:
                    pass
                if stop.is_set():
                    proc.terminate()
                    break
                for line in proc.stdout:
                    m = TUNNEL_RE.search(line)
                    if m:
                        self.error = None
                        self._set(m.group(1))
                proc.wait()
                self.error = f"the secure link stopped (code {proc.returncode})"
            except Exception as exc:
                self.error = f"couldn't start the secure link: {exc}"
                log.warning("tunnel: %s", self.error)
            self._set(None)
            if time.monotonic() - started > 600:
                backoff = self.restart_s  # it ran fine for a while: restart quickly
            stop.wait(backoff)
            backoff = min(self.max_restart_s, backoff * 2)

    def status(self) -> dict:
        return {"running": self.running, "host": self.host, "error": self.error}


def _is_cloudflared(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            return "cloudflared" in out.lower()
        return "cloudflared" in Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").lower()
    except (OSError, subprocess.SubprocessError):
        return False


def phone_line(server: dict, tunnel: dict, link: Optional[str]) -> str:
    """One plain line for Settings about the phone app."""
    if not server.get("running"):
        return server.get("error") or "The phone app is turned off."
    if link:
        paired = server.get("devices", 0)
        extra = f" {paired} phone{'s' if paired != 1 else ''} paired." if paired else ""
        return (f"Open this on your phone, then use Add to Home Screen: {link}{extra}")
    if tunnel.get("error"):
        return f"Phone app: {tunnel['error']}. Jarvis keeps retrying."
    return "Phone app: starting the secure link..."
