"""NEXUS API: the one door OS1, OS2, OS3 and Jarvis talk through.

`NexusHub` routes contract requests to the right system (enforcing timeouts, publishing events, keeping health).
`NexusServer` serves it on this computer only (127.0.0.1) as REST (`POST /v1/request`), Server-Sent Events
(`GET /v1/events`) and WebSocket (`GET /v1/ws`), behind bearer tokens with roles and a per-token rate limit.
`NexusClient` is the matching caller with a retry policy for timeouts and busy systems.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import queue
import secrets
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

import httpx

from jarvis_books.nexus.contracts import VERSION, ContractError, NexusRequest, NexusResponse, failure

log = logging.getLogger(__name__)

PORT = 8770
MAX_BODY = 1_000_000
DEFAULT_TIMEOUT_MS = 30_000
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ROLES = ("reader", "operator", "owner")


class NexusHub:
    """Routes each request to its destination system, with a time limit, and tells listeners what happened."""

    def __init__(self, systems: dict[str, Callable[[NexusRequest, str], NexusResponse]], workers: int = 8,
                 clock=time.time):
        self.systems = dict(systems)
        self.clock = clock
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nexus-hub")
        self._listeners: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.stats = {name: {"requests": 0, "errors": 0, "timeouts": 0, "total_ms": 0.0} for name in self.systems}
        self.started_at = clock()

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def subscribe(self, maxsize: int = 200) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize)
        with self._lock:
            self._listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def publish(self, event: str, data: dict) -> None:
        item = {"event": event, "at": self.clock(), **data}
        with self._lock:
            listeners = list(self._listeners)
        for q in listeners:
            try:
                q.put_nowait(item)
            except queue.Full:  # a slow listener misses events rather than slowing everyone down
                pass

    def route(self, request: NexusRequest | dict | str, role: str = "owner") -> NexusResponse:
        started = time.perf_counter()
        try:
            if isinstance(request, str):
                request = NexusRequest.from_json(request)
            elif isinstance(request, dict):
                request = NexusRequest.from_dict(request)
        except ContractError as exc:
            raw = request if isinstance(request, dict) else {}
            bogus = NexusRequest("jarvis", "jarvis", "invalid", {}, str(raw.get("request_id") or "invalid")[:100])
            return failure(bogus, "jarvis", exc.code, str(exc), started)
        target = self.systems.get(request.destination)
        if target is None:
            return failure(request, "jarvis", "unavailable", f"{request.destination} isn't running here", started)
        limit = (request.timeout_ms or DEFAULT_TIMEOUT_MS) / 1000
        self.publish("request", {"request_id": request.request_id, "correlation_id": request.correlation_id,
                                 "destination": request.destination, "operation": request.operation})
        stats = self.stats[request.destination]
        future = self._pool.submit(target, request, role)
        try:
            response = future.result(timeout=limit)
        except FutureTimeout:
            stats["timeouts"] += 1
            response = failure(request, request.destination, "timeout",
                               f"{request.destination} {request.operation} took longer than {limit:g} s; it may "
                               f"still finish in the background", started)
        except Exception as exc:
            log.exception("nexus route %s failed", request.destination)
            response = failure(request, request.destination, "internal", f"{type(exc).__name__}: {exc}", started)
        NexusResponse.from_dict(response.to_dict())  # every reply is checked against the contract
        stats["requests"] += 1
        stats["errors"] += 0 if response.ok else 1
        stats["total_ms"] += response.execution_time_ms
        self.publish("response", {"request_id": response.request_id, "correlation_id": response.correlation_id,
                                  "source": response.source, "operation": response.operation,
                                  "status": response.status, "error": response.error_code,
                                  "execution_time_ms": round(response.execution_time_ms, 3)})
        return response

    def health(self) -> dict:
        systems = {}
        for name, s in self.stats.items():
            n = s["requests"]
            systems[name] = {"up": True, "requests": n, "errors": s["errors"], "timeouts": s["timeouts"],
                             "error_rate": round(s["errors"] / n, 4) if n else 0.0,
                             "avg_ms": round(s["total_ms"] / n, 3) if n else 0.0}
        return {"version": VERSION, "status": "ok", "uptime_s": round(self.clock() - self.started_at, 1),
                "systems": systems}


class TokenBook:
    """Bearer tokens and their roles. Only SHA-256 hashes are stored; the owner token lives in a private file.
    The file is shared with other programs on this account (a check script, a second Jarvis), so it is read again
    whenever it changed on disk."""

    def __init__(self, home: Path):
        self.path = Path(home) / "nexus_api.json"
        self._lock = threading.Lock()
        self._stamp = self._file_stamp()
        self._data = self._load()

    def _file_stamp(self) -> Optional[tuple]:
        try:
            st = self.path.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def _refresh(self) -> None:
        stamp = self._file_stamp()
        if stamp != self._stamp:
            self._stamp, self._data = stamp, self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data.get("tokens"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"tokens": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._stamp = self._file_stamp()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(self, role: str, label: str = "") -> str:
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        token = "nx_" + secrets.token_urlsafe(32)
        with self._lock:
            self._refresh()
            self._data["tokens"][self._hash(token)] = {"role": role, "label": label[:60], "created": time.time()}
            self._save()
        return token

    def owner_token(self) -> str:
        """The owner token for Jarvis's own clients, made once and kept in nexus_api.key (this account only)."""
        key = self.path.with_name("nexus_api.key")
        try:
            token = key.read_text(encoding="utf-8").strip()
            if self.role_of(token) == "owner":
                return token
        except OSError:
            pass
        token = self.issue("owner", "jarvis")
        key.write_text(token, encoding="utf-8")
        try:
            os.chmod(key, 0o600)
        except OSError:
            pass
        return token

    def role_of(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        digest = self._hash(token)
        with self._lock:
            self._refresh()
            for known, info in self._data["tokens"].items():
                if hmac.compare_digest(known, digest):
                    return info["role"]
        return None

    def revoke(self, token: str) -> bool:
        with self._lock:
            self._refresh()
            gone = self._data["tokens"].pop(self._hash(token), None) is not None
            if gone:
                self._save()
        return gone


class RateLimit:
    """A token bucket per caller: `per_minute` requests a minute, bursts up to `burst`."""

    def __init__(self, per_minute: int = 120, burst: int = 30, clock=time.monotonic):
        self.rate, self.burst, self.clock = per_minute / 60.0, burst, clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, who: str) -> bool:
        now = self.clock()
        with self._lock:
            tokens, last = self._buckets.get(who, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1:
                self._buckets[who] = (tokens, now)
                return False
            self._buckets[who] = (tokens - 1, now)
            return True


class NexusServer:
    def __init__(self, hub: NexusHub, tokens: TokenBook, port: int = PORT, host: str = "127.0.0.1",
                 limit: Optional[RateLimit] = None):
        self.hub, self.tokens, self.host, self.requested_port = hub, tokens, host, port
        self.limit = limit or RateLimit()
        self.port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.stopping = threading.Event()
        self.error = ""

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> bool:
        if self._server is not None:
            return True
        self.stopping.clear()
        server = self

        class Handler(_Handler):
            nexus = server

        try:
            self._server = ThreadingHTTPServer((self.host, self.requested_port), Handler)
        except OSError as exc:
            self.error = f"port {self.requested_port} is busy ({exc.strerror or exc})"
            log.warning("nexus api: %s", self.error)
            return False
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="nexus-api", daemon=True)
        self._thread.start()
        log.info("nexus api on http://%s:%d", self.host, self.port)
        return True

    def stop(self) -> None:
        self.stopping.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def status(self) -> dict:
        return {"running": self.running, "url": f"http://{self.host}:{self.port}" if self.running else "",
                "error": self.error}


class _Handler(BaseHTTPRequestHandler):
    nexus: NexusServer
    protocol_version = "HTTP/1.1"
    server_version = "NEXUS"
    sys_version = ""

    def log_message(self, fmt, *args):  # requests are logged by the hub without bodies, never with tokens
        pass

    # ---- helpers
    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _role(self) -> Optional[str]:
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        role = self.nexus.tokens.role_of(token)
        if role is None:
            self._json(401, {"error": {"code": "unauthorized", "message": "a valid bearer token is required"}})
            return None
        if not self.nexus.limit.allow(hashlib.sha256(token.encode()).hexdigest()[:16]):
            self._json(429, {"error": {"code": "rate_limited", "message": "too many requests; slow down"}})
            return None
        return role

    def _host_ok(self) -> bool:
        """Only this computer's own names: a web page can't reach the API by pointing a domain at 127.0.0.1."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host in ("127.0.0.1", "localhost", "::1"):
            return True
        self._json(403, {"error": {"code": "forbidden", "message": "local requests only"}})
        return False

    # ---- routes
    def do_GET(self):
        if not self._host_ok():
            return
        path = self.path.split("?")[0]
        if path == "/v1/health":
            return self._json(200, self.nexus.hub.health())
        role = self._role()
        if role is None:
            return
        if path == "/v1/events":
            return self._events()
        if path == "/v1/ws":
            return self._websocket(role)
        self._json(404, {"error": {"code": "not_found", "message": "unknown path"}})

    def do_POST(self):
        if not self._host_ok():
            return
        if self.path.split("?")[0] != "/v1/request":
            return self._json(404, {"error": {"code": "not_found", "message": "unknown path"}})
        role = self._role()
        if role is None:
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._json(413 if length > MAX_BODY else 400,
                              {"error": {"code": "invalid_request", "message": "send one JSON request body"}})
        try:
            raw = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._json(400, {"error": {"code": "invalid_request", "message": "body is not JSON"}})
        response = self.nexus.hub.route(raw, role)
        self._json(200 if response.ok else _HTTP.get(response.error_code, 500), response.to_dict())

    def _events(self):
        q = self.nexus.hub.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not self.nexus.stopping.is_set():
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                else:
                    self.wfile.write(f"event: {item['event']}\ndata: {json.dumps(item, default=str)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.nexus.hub.unsubscribe(q)

    def _websocket(self, role: str):
        key = self.headers.get("Sec-WebSocket-Key", "")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            return self._json(400, {"error": {"code": "invalid_request", "message": "WebSocket upgrade expected"}})
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        sock = self.connection
        send_lock = threading.Lock()
        events = self.nexus.hub.subscribe()
        done = threading.Event()

        def send(obj: dict) -> None:
            with send_lock:
                sock.sendall(ws_frame(json.dumps(obj, default=str).encode("utf-8")))

        def pump():
            while not done.is_set():
                try:
                    item = events.get(timeout=1)
                except queue.Empty:
                    continue
                try:
                    send({"type": "event", **item})
                except OSError:
                    return

        threading.Thread(target=pump, name="nexus-ws-events", daemon=True).start()
        try:
            while not self.nexus.stopping.is_set():
                opcode, payload = ws_read(self.rfile)
                if opcode == 8 or opcode is None:
                    with send_lock:
                        sock.sendall(ws_frame(b"", opcode=8))
                    break
                if opcode == 9:
                    with send_lock:
                        sock.sendall(ws_frame(payload, opcode=10))
                    continue
                if opcode != 1:
                    continue
                try:
                    raw = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    send({"type": "error", "error": {"code": "invalid_request", "message": "message is not JSON"}})
                    continue
                send({"type": "response", **self.nexus.hub.route(raw, role).to_dict()})
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            done.set()
            self.nexus.hub.unsubscribe(events)


_HTTP = {"invalid_request": 400, "unknown_operation": 400, "unsafe_url": 400, "not_found": 404, "timeout": 504,
         "unavailable": 503, "internal": 500}


def ws_frame(payload: bytes, opcode: int = 1, mask: bool = False) -> bytes:
    head = bytes([0x80 | opcode])
    n = len(payload)
    bit = 0x80 if mask else 0
    if n < 126:
        head += bytes([bit | n])
    elif n < 1 << 16:
        head += bytes([bit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([bit | 127]) + struct.pack("!Q", n)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def ws_read(stream, limit: int = MAX_BODY) -> tuple[Optional[int], bytes]:
    """One whole message (continuation frames joined). (None, b'') when the connection closed."""
    message, first_op = b"", None
    while True:
        head = stream.read(2)
        if len(head) < 2:
            return None, b""
        fin, opcode = head[0] & 0x80, head[0] & 0x0F
        masked, n = head[1] & 0x80, head[1] & 0x7F
        if n == 126:
            n = struct.unpack("!H", stream.read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", stream.read(8))[0]
        if n + len(message) > limit:
            raise ValueError("message too large")
        key = stream.read(4) if masked else b""
        data = stream.read(n)
        if masked:
            data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
        if opcode >= 8:  # control frames may arrive between fragments
            return opcode, data
        if first_op is None:
            first_op = opcode
        message += data
        if fin:
            return first_op, message


READ_ONLY = {"os1": {"search", "resource", "status", "history"},
             "os2": {"brief", "facts", "entity", "contradictions", "status"},
             "os3": {"tools", "describe", "validate", "plan", "history"}}


def safe_to_repeat(destination: str, operation: str, payload: dict) -> bool:
    """Only look-only requests may be sent twice: a crawl, an improvement round or a tool run that timed out may
    still be running on the server, and sending it again would start a second one."""
    if destination == "os1" and operation == "rsi":
        return payload.get("action") in ("status", "history")
    return operation in READ_ONLY.get(destination, ())


class NexusClient:
    """Calls the NEXUS API over REST with a retry policy. Busy (503), rate limits (429) and refused connections
    never reached the work, so they are retried with backoff. A reply that didn't come back in time (timed out on
    the server, or the connection dropped mid-request) is retried only for look-only requests. The HTTP wait
    follows the request's own timeout_ms."""

    MARGIN_S = 15.0

    def __init__(self, base_url: str, token: str, retries: int = 2, backoff_s: float = 0.5,
                 client: Optional[httpx.Client] = None, sleep=time.sleep):
        self.base_url, self.token, self.retries, self.backoff_s, self.sleep = base_url.rstrip("/"), token, \
            retries, backoff_s, sleep
        self.http = client or httpx.Client(timeout=DEFAULT_TIMEOUT_MS / 1000 + self.MARGIN_S, trust_env=False)

    def call(self, destination: str, operation: str, payload: dict, timeout_ms: Optional[int] = None,
             correlation_id: Optional[str] = None) -> NexusResponse:
        request = NexusRequest("jarvis", destination, operation, payload, correlation_id=correlation_id,
                               timeout_ms=timeout_ms)
        repeatable = safe_to_repeat(destination, operation, payload)
        wait_s = (timeout_ms or DEFAULT_TIMEOUT_MS) / 1000 + self.MARGIN_S
        last: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            if attempt:
                self.sleep(self.backoff_s * (2 ** (attempt - 1)))
            try:
                r = self.http.post(f"{self.base_url}/v1/request", content=request.to_json(), timeout=wait_s,
                                   headers={"Authorization": f"Bearer {self.token}",
                                            "Content-Type": "application/json"})
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = exc
                continue
            except httpx.TransportError as exc:
                if not repeatable:
                    raise ConnectionError(f"no reply from the NEXUS API for {destination} {operation}; it may "
                                          f"still be running there, so it was not sent again: {exc}") from exc
                last = exc
                continue
            if r.status_code == 429:
                last = RuntimeError("rate limited")
                continue
            if r.status_code == 401:
                raise PermissionError("the NEXUS API refused the token")
            response = NexusResponse.from_dict(r.json())
            retry = response.error_code == "unavailable" or (response.error_code == "timeout" and repeatable)
            if response.ok or not retry or attempt == self.retries:
                return response
        raise ConnectionError(f"NEXUS API unreachable after {self.retries + 1} tries: {last}")

    def health(self) -> dict:
        return self.http.get(f"{self.base_url}/v1/health").json()

    def close(self) -> None:
        self.http.close()


OS1_CHANGES = ("crawl", "revisit", "embed")  # these fetch from the web or write; readers may only look


def systems_for(os1, os2, os3) -> dict[str, Callable[[NexusRequest, str], NexusResponse]]:
    """The hub's routing table: each system's own handler, with caller roles applied."""
    def door1(request: NexusRequest, role: str) -> NexusResponse:
        if request.operation in OS1_CHANGES and role == "reader":
            return failure(request, "os1", "invalid_request", f"a reader may not {request.operation}",
                           time.perf_counter())
        return os1.handle(request)

    return {"os1": door1, "os2": lambda request, role: os2.handle(request),
            "os3": lambda request, role: os3.handle(request, role)}
