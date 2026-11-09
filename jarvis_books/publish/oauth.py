"""OAuth 2.0 for installed apps: sign-in in the system browser, loopback redirect and PKCE.

The app never sees the user's password. Tokens are kept in an encrypted, per-user AccountStore.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from jarvis_books.keystore import unseal, write_sealed
from jarvis_books.publish.errors import PublishError, api_message

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
DONE_PAGE = (b"<html><body style='font-family:sans-serif;background:#0f1b2d;color:#e2e8f0;padding:40px'>"
             b"<h2>Signed in</h2><p>You can close this tab and go back to Jarvis Book Writer.</p></body></html>")


class AccountStore:
    """Connected accounts and their tokens, encrypted for this Windows user and never mirrored to OneDrive."""

    def __init__(self, home: Path):
        self.path = Path(home) / "accounts.sealed"
        self._lock = threading.Lock()

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(unseal(self.path.read_bytes()).decode() or "{}")

    def get(self, provider: str) -> Optional[dict]:
        with self._lock:
            return self._read().get(provider)

    def put(self, provider: str, record: dict) -> None:
        with self._lock:
            data = self._read()
            data[provider] = record
            write_sealed(self.path, json.dumps(data).encode())

    def remove(self, provider: str) -> None:
        with self._lock:
            data = self._read()
            data.pop(provider, None)
            write_sealed(self.path, json.dumps(data).encode())

    def summary(self) -> dict:
        """What the UI may show: account names only, never tokens."""
        with self._lock:
            return {p: {"account": r.get("account", ""), "connected_at": r.get("connected_at")}
                    for p, r in self._read().items()}


def load_client(path: Path) -> dict:
    """Read a Google 'Desktop app' OAuth client file (client_secret_*.json from Google Cloud Console)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PublishError(f"could not read the Google client file: {exc}") from None
    client = data.get("installed")
    if not isinstance(client, dict) or not client.get("client_id"):
        raise PublishError("that file is not a Google 'Desktop app' OAuth client (it needs an 'installed' section)")
    return {"client_id": client["client_id"], "client_secret": client.get("client_secret", "")}


class _Catcher(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802 - http.server API
        query = parse_qs(urlparse(self.path).query)
        if "code" not in query and "error" not in query:
            self.send_response(404)
            self.end_headers()
            return
        self.server.result = {k: v[0] for k, v in query.items()}  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(DONE_PAGE)

    def log_message(self, *args):  # keep auth codes out of logs
        pass


class GoogleOAuth:
    def __init__(self, client: dict, http, open_browser: Callable[[str], object] = webbrowser.open,
                 timeout_s: float = 300.0):
        self.client = client
        self.http = http
        self.open_browser = open_browser
        self.timeout_s = timeout_s

    def sign_in(self, scopes: list[str], cancel: Optional[threading.Event] = None) -> dict:
        """Open Google's sign-in page and wait for the loopback redirect. Returns the token record."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        server = HTTPServer(("127.0.0.1", 0), _Catcher)
        server.result = {}  # type: ignore[attr-defined]
        server.timeout = 0.5
        redirect = f"http://127.0.0.1:{server.server_port}"
        url = GOOGLE_AUTH_URL + "?" + urlencode({
            "client_id": self.client["client_id"], "redirect_uri": redirect, "response_type": "code",
            "scope": " ".join(scopes), "code_challenge": challenge, "code_challenge_method": "S256",
            "state": state, "access_type": "offline", "prompt": "consent"})
        try:
            self.open_browser(url)
            deadline = time.time() + self.timeout_s
            while not server.result and time.time() < deadline:  # type: ignore[attr-defined]
                if cancel is not None and cancel.is_set():
                    raise PublishError("sign-in cancelled")
                server.handle_request()
            result = server.result  # type: ignore[attr-defined]
        finally:
            server.server_close()
        if not result:
            raise PublishError("sign-in timed out; try Connect again")
        if result.get("error"):
            raise PublishError(f"Google sign-in was not completed ({result['error']})")
        if result.get("state") != state:
            raise PublishError("sign-in response did not match this request; try again")
        tokens = self._token({"code": result["code"], "redirect_uri": redirect, "grant_type": "authorization_code",
                              "code_verifier": verifier})
        if not tokens.get("refresh_token"):
            raise PublishError("Google did not grant offline access; disconnect and connect again")
        return tokens

    def refresh(self, record: dict) -> dict:
        fresh = self._token({"refresh_token": record["refresh_token"], "grant_type": "refresh_token"})
        return {**record, **fresh, "refresh_token": fresh.get("refresh_token") or record["refresh_token"]}

    def revoke(self, record: dict) -> None:
        token = record.get("refresh_token") or record.get("access_token")
        if token:
            try:
                self.http.request("POST", GOOGLE_REVOKE_URL, data={"token": token}, timeout=30)
            except Exception:
                pass  # disconnecting locally still succeeds

    def _token(self, form: dict) -> dict:
        form = {**form, "client_id": self.client["client_id"]}
        if self.client.get("client_secret"):
            form["client_secret"] = self.client["client_secret"]
        resp = self.http.request("POST", GOOGLE_TOKEN_URL, data=form, timeout=60)
        if resp.status_code != 200:
            try:
                detail = resp.json().get("error_description") or resp.json().get("error")
            except Exception:
                detail = api_message(resp)
            raise PublishError(f"Google sign-in failed: {detail}", status=resp.status_code)
        data = resp.json()
        data["expires_at"] = time.time() + float(data.get("expires_in", 3600)) - 60
        return {k: data[k] for k in ("access_token", "refresh_token", "expires_at", "scope") if k in data}
