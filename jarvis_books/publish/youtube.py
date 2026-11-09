"""YouTube Data API v3: channel lookup, resumable video upload (resume + retry), playlists and thumbnails."""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import requests

from jarvis_books.publish.errors import PublishError, api_message
from jarvis_books.publish.oauth import AccountStore, GoogleOAuth

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
SCOPES = ["https://www.googleapis.com/auth/youtube"]  # upload, playlists and reading the channel name
CHUNK = 8 * 1024 * 1024  # must be a multiple of 256 KiB
LIMIT_REASONS = ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded")


class YouTube:
    def __init__(self, accounts: AccountStore, oauth: GoogleOAuth, http, sleep: Callable[[float], None] = time.sleep,
                 chunk_size: int = CHUNK, max_retries: int = 6):
        if chunk_size % (256 * 1024):
            raise ValueError("chunk size must be a multiple of 256 KiB")
        self.accounts = accounts
        self.oauth = oauth
        self.http = http
        self.sleep = sleep
        self.chunk_size = chunk_size
        self.max_retries = max_retries

    # ---- auth ------------------------------------------------------------------------
    def _token(self, force_refresh: bool = False) -> str:
        record = self.accounts.get("youtube")
        if not record:
            raise PublishError("Connect your YouTube account first")
        if force_refresh or record.get("expires_at", 0) <= time.time():
            record = self.oauth.refresh(record)
            self.accounts.put("youtube", record)
        return record["access_token"]

    def _call(self, method: str, url: str, headers: Optional[dict] = None, **kw) -> requests.Response:
        for attempt in range(2):
            h = {"Authorization": f"Bearer {self._token(force_refresh=attempt > 0)}", **(headers or {})}
            resp = self.http.request(method, url, headers=h, timeout=kw.pop("timeout", 60), **kw)
            if resp.status_code != 401:
                return resp
        return resp

    @staticmethod
    def _check(resp) -> requests.Response:
        if resp.status_code < 300:
            return resp
        message = api_message(resp)
        if resp.status_code == 403 and any(r in message for r in LIMIT_REASONS):
            raise PublishError("YouTube's upload limit for today was reached. It resets at midnight Pacific time; "
                               "the post will continue when you press Retry.", retryable=True, status=403)
        if resp.status_code == 401:
            raise PublishError("YouTube sign-in expired; disconnect and connect again", status=401)
        raise PublishError(f"YouTube refused the request: {message}", retryable=resp.status_code >= 500,
                           status=resp.status_code)

    # ---- channel ---------------------------------------------------------------------
    def channel(self) -> dict:
        resp = self._check(self._call("GET", f"{API}/channels", params={"part": "snippet", "mine": "true"}))
        items = resp.json().get("items") or []
        if not items:
            raise PublishError("This Google account has no YouTube channel yet. Create one at youtube.com, then "
                               "connect again.")
        return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}

    # ---- upload ----------------------------------------------------------------------
    def _start(self, metadata: dict, total: int) -> str:
        resp = self._check(self._call(
            "POST", UPLOAD_URL, params={"uploadType": "resumable", "part": ",".join(metadata)}, json=metadata,
            headers={"X-Upload-Content-Length": str(total), "X-Upload-Content-Type": "video/mp4"}))
        uri = resp.headers.get("Location")
        if not uri:
            raise PublishError("YouTube did not return an upload address", retryable=True)
        return uri

    def _query(self, uri: str, total: int):
        """Ask how much of an interrupted upload YouTube has. Returns (offset, video) or None if the session is gone."""
        resp = self._call("PUT", uri, headers={"Content-Length": "0", "Content-Range": f"bytes */{total}"})
        if resp.status_code in (200, 201):
            return total, resp.json()
        if resp.status_code == 308:
            return _next_offset(resp), None
        if resp.status_code in (404, 410):
            return None
        self._check(resp)
        return 0, None

    def upload(self, path: Path, metadata: dict, session_uri: str = "", on_session: Optional[Callable[[str], None]] = None,
               progress: Optional[Callable[[float], None]] = None, cancel: Optional[threading.Event] = None) -> dict:
        """Upload a video, resuming `session_uri` when given. Returns the created video resource."""
        total = os.path.getsize(path)
        uri, offset = session_uri, 0
        if uri:
            state = self._query(uri, total)
            if state is None:
                uri = ""
            else:
                offset, video = state
                if video:
                    return video
        if not uri:
            uri = self._start(metadata, total)
            if on_session:
                on_session(uri)
        failures = restarts = 0
        with open(path, "rb") as f:
            while True:
                if cancel is not None and cancel.is_set():
                    raise PublishError("upload cancelled", retryable=True)
                f.seek(offset)
                chunk = f.read(self.chunk_size)
                end = offset + len(chunk) - 1
                try:
                    resp = self._call("PUT", uri, data=chunk, timeout=600, headers={
                        "Content-Length": str(len(chunk)), "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {offset}-{end}/{total}"})
                except (requests.ConnectionError, requests.Timeout) as exc:
                    log.warning("upload chunk failed: %s", type(exc).__name__)
                    resp = None
                if resp is not None and resp.status_code in (200, 201):
                    if progress:
                        progress(1.0)
                    return resp.json()
                if resp is not None and resp.status_code == 308:
                    offset, failures = _next_offset(resp), 0
                    if progress:
                        progress(offset / total)
                    continue
                if resp is not None and resp.status_code in (404, 410) and restarts < 1:
                    restarts += 1  # the session expired (they last about a week): start a fresh one
                    uri, offset = self._start(metadata, total), 0
                    if on_session:
                        on_session(uri)
                    continue
                if resp is None or resp.status_code in (500, 502, 503, 504):
                    failures += 1
                    if failures > self.max_retries:
                        raise PublishError("YouTube kept failing. Press Retry to continue from where it stopped.",
                                           retryable=True)
                    self.sleep(min(2 ** failures + random.random(), 64))
                    state = self._query_safely(uri, total)
                    if state is None:
                        continue  # still unreachable; the next attempt backs off further
                    offset, video = state
                    if video:
                        return video
                    continue
                self._check(resp)

    def _query_safely(self, uri: str, total: int):
        try:
            return self._query(uri, total)
        except (requests.ConnectionError, requests.Timeout):
            return None

    # ---- playlists and thumbnails -----------------------------------------------------
    def create_playlist(self, title: str, description: str, privacy: str) -> str:
        body = {"snippet": {"title": title[:150], "description": description[:5000]},
                "status": {"privacyStatus": privacy}}
        resp = self._check(self._call("POST", f"{API}/playlists", params={"part": "snippet,status"}, json=body))
        return resp.json()["id"]

    def add_to_playlist(self, playlist_id: str, video_id: str) -> None:
        body = {"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
        self._check(self._call("POST", f"{API}/playlistItems", params={"part": "snippet"}, json=body))

    def set_thumbnail(self, video_id: str, image: Path) -> None:
        mime = "image/png" if str(image).lower().endswith(".png") else "image/jpeg"
        self._check(self._call("POST", THUMBNAIL_URL, params={"videoId": video_id, "uploadType": "media"},
                               data=Path(image).read_bytes(), headers={"Content-Type": mime}))


def _next_offset(resp) -> int:
    rng = resp.headers.get("Range", "")
    return int(rng.rsplit("-", 1)[1]) + 1 if "-" in rng else 0
