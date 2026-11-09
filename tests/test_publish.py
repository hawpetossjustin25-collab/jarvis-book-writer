import json
import threading
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest

from fakes import FakeGemini
from jarvis_books.publish.errors import PublishError
from jarvis_books.publish.oauth import GOOGLE_TOKEN_URL, AccountStore, GoogleOAuth, load_client
from jarvis_books.publish.video import chapter_timestamps, format_timestamp, render_video
from jarvis_books.publish.youtube import API, UPLOAD_URL, YouTube
from jarvis_books.studio import Studio

KIB256 = 256 * 1024
SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=abc"


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeGoogle:
    """Stands in for Google's token and YouTube endpoints, including a resumable upload session."""

    def __init__(self, fail_chunks=0, drop_session_after=None):
        self.received = bytearray()
        self.total = 0
        self.calls = []
        self.fail_chunks = fail_chunks
        self.tokens = 0
        self.started = 0
        self.metadata = None

    def request(self, method, url, headers=None, data=None, json=None, params=None, timeout=None):
        self.calls.append((method, url.split("?")[0], dict(headers or {}), params))
        if url == GOOGLE_TOKEN_URL:
            self.tokens += 1
            assert data["client_id"] == "cid"
            return Resp(200, {"access_token": f"at{self.tokens}", "refresh_token": "rt", "expires_in": 3600})
        if url.startswith("https://oauth2.googleapis.com/revoke"):
            return Resp(200, {})
        if url == f"{API}/channels":
            return Resp(200, {"items": [{"id": "UC1", "snippet": {"title": "Justin Reads"}}]})
        if url == UPLOAD_URL:
            self.started += 1
            self.metadata = json
            self.total = int(headers["X-Upload-Content-Length"])
            self.received = bytearray()
            return Resp(200, {}, {"Location": SESSION})
        if url == SESSION:
            rng = headers["Content-Range"]
            if rng.startswith("bytes */"):
                return self._state()
            if self.fail_chunks:
                self.fail_chunks -= 1
                return Resp(503, {"error": {"message": "backend error"}})
            start = int(rng.split()[1].split("-")[0])
            assert start == len(self.received), "client must resume exactly where the server is"
            self.received += data
            return self._state()
        if url == f"{API}/playlists":
            return Resp(200, {"id": "PL1"})
        if url == f"{API}/playlistItems":
            return Resp(200, {"id": "item"})
        raise AssertionError(f"unexpected call {method} {url}")

    def _state(self):
        if len(self.received) >= self.total:
            return Resp(200, {"id": "vid123", "status": {"uploadStatus": "uploaded"}})
        return Resp(308, None, {"Range": f"bytes=0-{len(self.received) - 1}"} if self.received else {})


def browser_that_approves(url):
    """Acts as the user's browser: follows Google's redirect back to the app's loopback address."""
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["code_challenge_method"] == "S256" and q["access_type"] == "offline"
    target = f"{q['redirect_uri']}/?code=good-code&state={q['state']}"
    threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=10).read(), daemon=True).start()


def connected_store(tmp_path):
    store = AccountStore(tmp_path)
    store.put("youtube", {"access_token": "at0", "refresh_token": "rt", "expires_at": 9e12, "account": "Justin"})
    return store


def test_sign_in_uses_pkce_loopback_and_stores_tokens_sealed(tmp_path):
    google = FakeGoogle()
    oauth = GoogleOAuth({"client_id": "cid", "client_secret": "s"}, google, browser_that_approves, timeout_s=10)
    tokens = oauth.sign_in(["scope"])
    assert tokens["access_token"] == "at1" and tokens["refresh_token"] == "rt"
    store = AccountStore(tmp_path)
    store.put("youtube", {**tokens, "account": "Justin Reads"})
    assert b"at1" not in store.path.read_bytes() or store.path.read_bytes().startswith(b"plain:")
    assert store.summary() == {"youtube": {"account": "Justin Reads", "connected_at": None}}


def test_sign_in_rejects_a_forged_state(tmp_path):
    def forged(url):
        redirect = parse_qs(urlparse(url).query)["redirect_uri"][0]
        threading.Thread(target=lambda: urllib.request.urlopen(f"{redirect}/?code=x&state=evil", timeout=10).read(),
                         daemon=True).start()
    with pytest.raises(PublishError, match="did not match"):
        GoogleOAuth({"client_id": "cid"}, FakeGoogle(), forged, timeout_s=10).sign_in(["scope"])


def test_client_file_must_be_a_desktop_client(tmp_path):
    bad = tmp_path / "web.json"
    bad.write_text(json.dumps({"web": {"client_id": "x"}}))
    with pytest.raises(PublishError, match="Desktop app"):
        load_client(bad)
    good = tmp_path / "desktop.json"
    good.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "s"}}))
    assert load_client(good)["client_id"] == "cid"


def make_file(tmp_path, size):
    f = tmp_path / "v.mp4"
    f.write_bytes((bytes(range(256)) * (size // 256 + 1))[:size])
    return f


def test_resumable_upload_survives_server_errors_and_resumes_offset(tmp_path):
    google = FakeGoogle(fail_chunks=2)
    oauth = GoogleOAuth({"client_id": "cid"}, google)
    yt = YouTube(connected_store(tmp_path), oauth, google, sleep=lambda s: None, chunk_size=KIB256)
    seen, sessions = [], []
    video = yt.upload(make_file(tmp_path, KIB256 * 3 + 1000), {"snippet": {"title": "t"}, "status": {}},
                      on_session=sessions.append, progress=seen.append)
    assert video["id"] == "vid123" and len(google.received) == KIB256 * 3 + 1000
    assert sessions == [SESSION] and seen[-1] == 1.0 and google.started == 1


def test_interrupted_upload_resumes_the_saved_session(tmp_path):
    google = FakeGoogle()
    yt = YouTube(connected_store(tmp_path), GoogleOAuth({"client_id": "cid"}, google), google,
                 sleep=lambda s: None, chunk_size=KIB256)
    path = make_file(tmp_path, KIB256 * 2)
    cancel = threading.Event()
    with pytest.raises(PublishError, match="cancelled"):
        yt.upload(path, {"snippet": {}}, progress=lambda f: cancel.set(), cancel=cancel)
    assert 0 < len(google.received) < KIB256 * 2
    video = yt.upload(path, {"snippet": {}}, session_uri=SESSION)
    assert video["id"] == "vid123" and google.started == 1  # no second upload session


def test_expired_token_is_refreshed_and_quota_error_is_explained(tmp_path):
    google = FakeGoogle()
    store = AccountStore(tmp_path)
    store.put("youtube", {"access_token": "old", "refresh_token": "rt", "expires_at": 0})
    yt = YouTube(store, GoogleOAuth({"client_id": "cid"}, google), google)
    assert yt.channel()["title"] == "Justin Reads"
    assert store.get("youtube")["access_token"] == "at1"
    quota = Resp(403, {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
    with pytest.raises(PublishError, match="upload limit") as err:
        YouTube._check(quota)
    assert err.value.retryable


def test_chapter_timestamps_follow_youtube_rules():
    assert format_timestamp(3_725_000) == "1:02:05" and format_timestamp(65_000) == "1:05"
    assert chapter_timestamps([("One", 60_000), ("Two", 30_000)]) == ""  # fewer than 3 chapters
    assert chapter_timestamps([("One", 60_000), ("Two", 30_000), ("Three", 5_000)]) == ""  # too short
    assert chapter_timestamps([("One", 60_000), ("Two", 30_000), ("Three", 15_000)]).splitlines() == \
        ["0:00 One", "1:00 Two", "1:30 Three"]


def test_render_video_from_audio(tmp_path):
    from jarvis_books.audio.export import _run, ffmpeg_path
    audio = tmp_path / "a.wav"
    _run([ffmpeg_path(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(audio)])
    seen = []
    out = render_video(audio, tmp_path / "out.mp4", "The Iron Gale", "Chapter 1", duration_ms=2000,
                       progress=seen.append)
    assert out.stat().st_size > 1000
    import subprocess
    info = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    assert "Video: h264" in info and "Audio: aac" in info and "Duration: 00:00:02" in info


@pytest.fixture
def studio(tmp_path):
    from conftest import hardware
    s = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(),
               engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None})
    s.start(probe=False)
    yield s
    s.close()


def test_publish_book_to_youtube_end_to_end(studio, tmp_path):
    google = FakeGoogle(fail_chunks=1)
    pub = studio.publisher
    pub.http, pub.sleep, pub.open_browser = google, (lambda s: None), browser_that_approves
    client = tmp_path / "client_secret.json"
    client.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "s"}}))
    pub.import_google_client(client)
    assert pub.connect_youtube() == "Justin Reads"
    assert pub.connected()["youtube"]["account"] == "Justin Reads"
    lib = studio.library
    b = lib.create_book("The Iron Gale", "fantasy", "A storm-witch hunts a ship.")
    lib.set_outline(b.id, [{"number": 1, "title": "Landfall", "outline": "x"}, {"number": 2, "title": "Vault",
                                                                                 "outline": "y"}])
    lib.save_chapter_text(lib.get_chapter(b.id, 1).id, "Rain fell on the docks. Mara <watched> the ship.")
    with pytest.raises(PublishError, match="not written"):
        pub.publish_youtube(b.id, chapters=[2])
    [post] = pub.publish_youtube(b.id, privacy="unlisted", tags=("fantasy", "audiobook"), description="A <tale>")
    job = studio.jobs.wait(studio.jobs.recent(1)[0].id, timeout=60)
    assert job.status == "done", job.error
    post = lib.get_post(post.id)
    assert post.status == "published" and post.url == "https://youtu.be/vid123" and post.error == ""
    meta = google.metadata
    assert meta["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False,
                              "containsSyntheticMedia": True}
    assert "AI voice" in meta["snippet"]["description"] and "<" not in meta["snippet"]["description"]
    assert any(c[1] == f"{API}/playlistItems" for c in google.calls)
    assert lib.get_meta(f"youtube.playlist.{b.id}") == "PL1"
    assert not pub.is_stale(post)
    lib.save_chapter_text(lib.get_chapter(b.id, 1).id, "Rain fell. Mara changed her mind.")
    assert pub.is_stale(lib.get_post(post.id))
    for _, _, headers, _ in google.calls:
        assert "Authorization" not in headers or headers["Authorization"].startswith("Bearer at")
    pub.disconnect("youtube")
    assert pub.connected() == {}


def test_publish_requires_a_connected_account(studio):
    b = studio.library.create_book("Untitled", "fantasy", "x")
    with pytest.raises(PublishError, match="Connect"):
        studio.publisher.publish_youtube(b.id)


def test_jarvis_asks_before_posting_and_then_posts(studio, tmp_path):
    google = FakeGoogle()
    pub = studio.publisher
    pub.http, pub.sleep, pub.open_browser = google, (lambda s: None), browser_that_approves
    client = tmp_path / "client_secret.json"
    client.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "s"}}))
    pub.import_google_client(client)
    pub.connect_youtube()
    lib = studio.library
    b = lib.create_book("Glass City", "mystery", "A detective in a city of glass.")
    lib.set_outline(b.id, [{"number": 1, "title": "Shards", "outline": "x"}])
    lib.save_chapter_text(lib.get_chapter(b.id, 1).id, "The towers rang like bells.")
    tools = studio.tools
    names = [d["name"] for d in tools.select("post it to youtube as unlisted")]
    assert "publish_audiobook" in names and "list_posts" in names
    ask = tools.call("publish_audiobook", {"book": "Glass", "privacy": "public"})
    assert ask["needs_confirmation"] and lib.list_posts(b.id) == []
    done = tools.call("publish_audiobook", {"book": "Glass", "privacy": "unlisted", "confirmed": True})
    assert done["started"], done
    studio.jobs.wait(studio.jobs.recent(1)[0].id, timeout=60)
    [post] = tools.call("list_posts", {"book": "Glass"})["posts"]
    assert post["status"] == "published" and post["url"] == "https://youtu.be/vid123"
