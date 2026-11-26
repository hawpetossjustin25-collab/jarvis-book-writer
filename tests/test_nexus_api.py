import json
import socket
import threading
import time

import httpx
import pytest

from jarvis_books.nexus.api import NexusClient, NexusHub, NexusServer, RateLimit, TokenBook, systems_for, \
    ws_frame, ws_read
from jarvis_books.nexus.contracts import NexusRequest, NexusResponse, success
from jarvis_books.nexus.os1 import OS1
from jarvis_books.nexus.os2 import OS2
from jarvis_books.nexus.os3 import OS3, ToolRegistry, ToolSpec
from nexus_site import FAST, FakeWeb, page

A = "https://alpha.example/"


@pytest.fixture
def nexus(tmp_path):
    os1 = OS1(tmp_path, FAST, fetcher_factory=FakeWeb({A: page("Acme Robotics", "Acme Robotics was founded in "
                                                                                  "1998 in Atlanta.")}).factory())
    reg = ToolRegistry(os1.store.db)
    reg.register(ToolSpec("get_weather", "weather for a city", {"type": "object", "properties": {
        "city": {"type": "string"}}, "required": ["city"]}, lambda a: {"temp_f": 70, "city": a["city"]}))
    reg.register(ToolSpec("send_text", "text someone", {"type": "object", "properties": {}},
                          lambda a: {"needs_approval": True}, level="outside"))
    os3 = OS3(reg)
    hub = NexusHub(systems_for(os1, OS2(os1), os3))
    tokens = TokenBook(tmp_path)
    server = NexusServer(hub, tokens, port=0)
    assert server.start()
    yield server, tokens, hub
    server.stop()
    hub.close()
    os3.close()
    os1.close()


def post(server, token, body, raw=None):
    return httpx.post(f"http://127.0.0.1:{server.port}/v1/request", content=raw if raw is not None
                      else json.dumps(body), headers={"Authorization": f"Bearer {token}"} if token else {},
                      timeout=30, trust_env=False)


def req(dest, op, payload, **kw):
    return NexusRequest("jarvis", dest, op, payload, **kw).to_dict()


def test_rest_routes_to_every_system_with_ids_kept(nexus):
    server, tokens, _ = nexus
    owner = tokens.owner_token()
    r = post(server, owner, req("os1", "crawl", {"seeds": [A], "max_pages": 2}, correlation_id="job-7"))
    assert r.status_code == 200 and r.json()["results"][0]["pages_fetched"] == 1
    assert r.json()["correlation_id"] == "job-7" and r.json()["source"] == "os1"
    brief = post(server, owner, req("os2", "brief", {"query": "Acme Robotics"})).json()
    assert brief["status"] == "success" and brief["metadata"]["facts"]["unverified"][0]["predicate"] == "founded"
    weather = post(server, owner, req("os3", "execute", {"name": "get_weather", "args": {"city": "Atlanta"}})).json()
    assert weather["results"][0]["status"] == "success" and weather["results"][0]["result"]["temp_f"] == 70
    NexusResponse.from_dict(weather)


def test_authentication_roles_and_validation(nexus):
    server, tokens, _ = nexus
    assert post(server, None, req("os1", "status", {})).status_code == 401
    assert post(server, "nx_wrong", req("os1", "status", {})).status_code == 401
    reader = tokens.issue("reader", "test")
    assert post(server, reader, req("os1", "status", {})).status_code == 200
    denied = post(server, reader, req("os1", "crawl", {"seeds": [A]}))
    assert denied.status_code == 400 and "reader may not crawl" in denied.json()["error"]["message"]
    no_text = post(server, reader, req("os3", "execute", {"name": "send_text", "args": {}}))
    assert no_text.status_code == 400 and "may not run outside" in no_text.json()["error"]["message"]
    assert post(server, reader, None, raw="{oops").status_code == 400
    bad = post(server, reader, {"request_id": "x", "source": "jarvis"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_request"
    assert post(server, reader, req("os1", "fly", {})).json()["error"]["code"] == "unknown_operation"
    assert tokens.revoke(reader) and post(server, reader, req("os1", "status", {})).status_code == 401
    with pytest.raises(ValueError):
        tokens.issue("god")


def test_tokens_are_stored_hashed_and_owner_token_is_reused(nexus, tmp_path):
    server, tokens, _ = nexus
    owner = tokens.owner_token()
    assert TokenBook(tmp_path).owner_token() == owner
    stored = (tmp_path / "nexus_api.json").read_text()
    assert owner not in stored and TokenBook(tmp_path).role_of(owner) == "owner"


def test_a_token_made_by_another_program_works_on_the_running_server(nexus, tmp_path):
    server, tokens, _ = nexus
    assert tokens.role_of("nx_unknown") is None  # the server has read the file once already
    outside = TokenBook(tmp_path)  # e.g. a check script on the same account, while Jarvis runs
    reader = outside.issue("reader", "script")
    url = f"http://127.0.0.1:{server.port}/v1/request"
    body = NexusRequest("jarvis", "os1", "status", {}).to_json()
    r = httpx.post(url, content=body, headers={"Authorization": f"Bearer {reader}"}, trust_env=False)
    assert r.status_code == 200 and r.json()["status"] == "success"
    keep = tokens.issue("operator", "kept")  # the server's own save keeps the outside token
    assert outside.role_of(keep) == "operator" and tokens.role_of(reader) == "reader"
    assert outside.revoke(reader)
    r = httpx.post(url, content=body, headers={"Authorization": f"Bearer {reader}"}, trust_env=False)
    assert r.status_code == 401


def test_only_local_host_names_are_served(nexus):
    server, tokens, _ = nexus
    r = httpx.post(f"http://127.0.0.1:{server.port}/v1/request", content="{}",
                   headers={"Host": "evil.example", "Authorization": f"Bearer {tokens.owner_token()}"},
                   trust_env=False)
    assert r.status_code == 403


def test_rate_limit():
    clock = [0.0]
    limit = RateLimit(per_minute=60, burst=2, clock=lambda: clock[0])
    assert limit.allow("a") and limit.allow("a") and not limit.allow("a") and limit.allow("b")
    clock[0] = 1.0
    assert limit.allow("a") and not limit.allow("a")


def test_rate_limited_requests_get_429(tmp_path):
    hub = NexusHub({"os1": lambda r, role: success(r, "os1", [], time.perf_counter())})
    tokens = TokenBook(tmp_path)
    server = NexusServer(hub, tokens, port=0, limit=RateLimit(per_minute=1, burst=1))
    server.start()
    try:
        t = tokens.owner_token()
        assert post(server, t, req("os1", "status", {})).status_code == 200
        assert post(server, t, req("os1", "status", {})).status_code == 429
    finally:
        server.stop()
        hub.close()


def test_hub_timeouts_health_and_events():
    release = threading.Event()

    def slow(r, role):
        release.wait(5)
        return success(r, "os2", [], time.perf_counter())

    hub = NexusHub({"os2": slow, "os1": lambda r, role: success(r, "os1", [1], time.perf_counter())})
    events = hub.subscribe()
    out = hub.route(NexusRequest("jarvis", "os2", "brief", {"query": "x"}, timeout_ms=100))
    assert out.error_code == "timeout" and "longer than 0.1 s" in out.error_message
    release.set()
    assert hub.route(NexusRequest("jarvis", "os1", "status", {})).ok
    assert hub.route(NexusRequest("jarvis", "os3", "tools", {})).error_code == "unavailable"
    health = hub.health()
    assert health["systems"]["os2"]["timeouts"] == 1 and health["systems"]["os1"]["requests"] == 1
    seen = [events.get_nowait()["event"] for _ in range(4)]
    assert seen == ["request", "response", "request", "response"]
    hub.close()


def test_server_sent_events_stream(nexus):
    server, tokens, hub = nexus
    owner = tokens.owner_token()
    lines = []

    def listen():
        with httpx.stream("GET", f"http://127.0.0.1:{server.port}/v1/events",
                          headers={"Authorization": f"Bearer {owner}"}, timeout=10, trust_env=False) as r:
            for line in r.iter_lines():
                lines.append(line)
                if line.startswith("data:") and '"response"' in line:
                    return

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not hub._listeners and time.time() < deadline:
        time.sleep(0.02)
    post(server, owner, req("os1", "status", {}))
    t.join(10)
    assert any(l == "event: request" for l in lines) and any('"status": "success"' in l for l in lines)


def test_websocket_requests_and_events(nexus):
    server, tokens, _ = nexus
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    sock.sendall((f"GET /v1/ws HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
                  f"Authorization: Bearer {tokens.owner_token()}\r\n\r\n").encode())
    stream = sock.makefile("rb")
    status = stream.readline()
    assert b"101" in status
    headers = b""
    while not headers.endswith(b"\r\n\r\n"):
        headers += stream.readline()
    assert b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in headers  # RFC 6455's worked example
    sock.sendall(ws_frame(json.dumps(req("os3", "execute", {"name": "get_weather", "args": {"city": "A"}},
                                         request_id="ws-1")).encode(), mask=True))
    got = []
    while not any(m.get("type") == "response" for m in got):
        op, data = ws_read(stream)
        assert op == 1
        got.append(json.loads(data))
    reply = next(m for m in got if m["type"] == "response")
    assert reply["request_id"] == "ws-1" and reply["results"][0]["status"] == "success"
    sock.sendall(ws_frame(b"not json", mask=True))
    while True:
        op, data = ws_read(stream)
        msg = json.loads(data)
        if msg["type"] == "error":
            break
    sock.sendall(ws_frame(b"", opcode=8, mask=True))
    assert ws_read(stream)[0] == 8
    sock.close()


def test_websocket_needs_a_token(nexus):
    server, _, _ = nexus
    r = httpx.get(f"http://127.0.0.1:{server.port}/v1/ws", headers={"Upgrade": "websocket"}, trust_env=False)
    assert r.status_code == 401


def test_ws_frames_round_trip_large_and_fragmented():
    import io
    big = b"x" * 70_000
    assert ws_read(io.BytesIO(ws_frame(big, mask=True))) == (1, big)
    part1 = bytes([0x01, 3]) + b"abc"
    part2 = bytes([0x80, 3]) + b"def"
    assert ws_read(io.BytesIO(part1 + part2)) == (1, b"abcdef")
    assert ws_read(io.BytesIO(b"")) == (None, b"")


def test_client_retries_busy_and_dropped_connections(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content)
        if calls["n"] == 1:
            raise httpx.ConnectError("refused")
        if calls["n"] == 2:
            return httpx.Response(503, json={"version": "1", "request_id": body["request_id"], "status": "error",
                                             "source": "os1", "operation": "status", "results": [],
                                             "error": {"code": "unavailable", "message": "busy"},
                                             "metadata": {"execution_time_ms": 1, "result_count": 0}})
        return httpx.Response(200, json={"version": "1", "request_id": body["request_id"], "status": "success",
                                         "source": "os1", "operation": "status", "results": [{"ok": 1}],
                                         "metadata": {"execution_time_ms": 1, "result_count": 1}})

    waits = []
    client = NexusClient("http://127.0.0.1:1", "t", retries=2, client=httpx.Client(
        transport=httpx.MockTransport(handler)), sleep=waits.append)
    out = client.call("os1", "status", {})
    assert out.ok and calls["n"] == 3 and waits == [0.5, 1.0]
    calls["n"] = 10
    assert client.call("os1", "status", {}).ok and calls["n"] == 11
    refused = NexusClient("http://127.0.0.1:1", "t", retries=1, client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={}))), sleep=lambda s: None)
    with pytest.raises(PermissionError):
        refused.call("os1", "status", {})


def test_client_against_the_real_server(nexus):
    server, tokens, _ = nexus
    client = NexusClient(f"http://127.0.0.1:{server.port}", tokens.owner_token())
    assert client.call("os2", "status", {}).ok and client.health()["status"] == "ok"
    client.close()
