"""YouTube Studio website route, driven in a real browser against a local stand-in for Studio's pages."""
from __future__ import annotations

import glob
import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from jarvis_books.publish.errors import PublishError
from jarvis_books.publish.studio import StudioUploader, find_edge, video_id_from

pytest.importorskip("playwright.sync_api")

VIDEO = "vidABCDEFGH"

DIALOG = """<!doctype html><html><body><ytcp-uploads-dialog>
<input type=file id=f>
<div id=title-textarea><div id=textbox contenteditable=true>untitled.mp4</div></div>
<div id=description-textarea><div id=textbox contenteditable=true></div></div>
<tp-yt-paper-radio-button name=VIDEO_MADE_FOR_KIDS_MFK data-group=kids aria-checked=false>kids</tp-yt-paper-radio-button>
<tp-yt-paper-radio-button name=VIDEO_MADE_FOR_KIDS_NOT_MFK data-group=kids aria-checked=false>not kids</tp-yt-paper-radio-button>
<button id=toggle-button>Show more</button>
<div id=more style="display:none">
 <tp-yt-paper-radio-button name=VIDEO_HAS_ALTERED_CONTENT_YES data-group=alt aria-checked=false>altered</tp-yt-paper-radio-button>
 <div id=tags-container><input id=text-input></div>
</div>
<div id=vis style="display:none">
 <tp-yt-paper-radio-button name=PRIVATE data-group=vis aria-checked=false>Private</tp-yt-paper-radio-button>
 <tp-yt-paper-radio-button name=UNLISTED data-group=vis aria-checked=false>Unlisted</tp-yt-paper-radio-button>
 <tp-yt-paper-radio-button name=PUBLIC data-group=vis aria-checked=false>Public</tp-yt-paper-radio-button>
</div>
<ytcp-video-info><a href="">link</a></ytcp-video-info>
<ytcp-video-upload-progress><span class=progress-label></span></ytcp-video-upload-progress>
<div class=error-short></div>
<button id=next-button>Next</button> <button id=done-button>Save</button>
</ytcp-uploads-dialog><script>
const SCENARIO = "%SCENARIO%";
let nexts = 0, file = "";
const label = document.querySelector('.progress-label');
document.querySelectorAll('tp-yt-paper-radio-button').forEach(r => r.addEventListener('click', () => {
  if (SCENARIO === 'stuck_private' && r.getAttribute('name') === 'PRIVATE') return;
  document.querySelectorAll(`[data-group=${r.dataset.group}]`).forEach(o => o.setAttribute('aria-checked', 'false'));
  r.setAttribute('aria-checked', 'true');
}));
document.getElementById('toggle-button').onclick = () => document.getElementById('more').style.display = 'block';
document.getElementById('next-button').onclick = () => {
  if (++nexts >= 3) document.getElementById('vis').style.display = 'block';
};
document.getElementById('f').onchange = e => {
  file = e.target.files[0].name;
  if (SCENARIO === 'limit') { document.querySelector('.error-short').textContent = 'Daily upload limit reached'; return; }
  document.querySelector('ytcp-video-info a').href = 'https://youtu.be/%VIDEO%';
  let pct = 0;
  label.textContent = 'Uploading 0%';
  if (SCENARIO === 'stall') { label.textContent = 'Uploading 10%'; return; }
  const t = setInterval(() => {
    pct += 25;
    label.textContent = pct >= 100 ? 'Upload complete ... Processing will begin shortly' : `Uploading ${pct}%`;
    if (pct >= 100) clearInterval(t);
  }, 150);
};
const checked = g => (document.querySelector(`[data-group=${g}][aria-checked=true]`) || {}).getAttribute?.('name') || '';
document.getElementById('done-button').onclick = () => fetch('/submitted', {method: 'POST', body: JSON.stringify({
  title: document.querySelector('#title-textarea #textbox').textContent,
  description: document.querySelector('#description-textarea #textbox').textContent,
  kids: checked('kids'), altered: checked('alt'), visibility: checked('vis'), file,
  tags: document.getElementById('text-input').value, progress: label.textContent})});
</script></body></html>"""


class FakeStudio(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.scenario = "ok"
        self.submitted: list[dict] = []
        self.url = f"http://127.0.0.1:{self.server_address[1]}"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: str = "", headers: dict = None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        data = body.encode()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        signed = "sid=ok" in (self.headers.get("Cookie") or "")
        path = self.path.split("?")[0]
        if path == "/login-ok":
            return self._send(200, "signed in", {"Set-Cookie": "sid=ok; Max-Age=86400; Path=/"})
        if not signed and path in ("/studio", "/upload"):
            return self._send(302, headers={"Location": "/ServiceLogin?continue=studio"})
        if path == "/studio":
            return self._send(302, headers={"Location": "/channel/UCfake1234567890/dashboard"})
        if path.startswith("/channel/"):
            return self._send(200, "<div id=entity-name> Justin Reads </div>")
        if path == "/upload":
            return self._send(200, DIALOG.replace("%SCENARIO%", self.server.scenario).replace("%VIDEO%", VIDEO))
        return self._send(200, "sign in page")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.submitted.append(json.loads(body))
        self._send(200, "ok")


def browser_launch() -> dict:
    if sys.platform == "win32" and find_edge():
        return {"channel": "msedge"}
    found = sorted(glob.glob("/opt/.devin/chrome/chrome/linux-*/chrome-linux64/chrome"))
    for path in found[-1:] + ["/usr/bin/google-chrome-stable", "/usr/bin/chromium"]:
        if os.path.exists(path):
            return {"executable_path": path}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            if os.path.exists(p.chromium.executable_path):
                return {}
    except Exception:
        pass
    pytest.skip("no Chromium-based browser for the Studio tests")


class Lookup:
    """YouTube's public oEmbed lookup: 401 for private videos, 200 for anything anyone can see."""

    def __init__(self, status: int):
        self.status, self.asked = status, []

    def get(self, url, params=None, timeout=None):
        self.asked.append(params["url"])
        return type("R", (), {"status_code": self.status})()


@pytest.fixture
def fake_studio():
    server = FakeStudio()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def make(fake, profile: Path, status: int = 401, **kw) -> StudioUploader:
    return StudioUploader(profile, Lookup(status), launch=browser_launch(), studio_url=fake.url + "/studio",
                          upload_url=fake.url + "/upload", sign_in_url=fake.url + "/ServiceLogin",
                          step_timeout_ms=8000, poll_s=0.1, **kw)


def sign_in_by_hand(fake, profile: Path):
    """Stands in for Justin signing in: a browser on Jarvis's profile gets the session cookie, then closes."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile), headless=True, **browser_launch())
        (ctx.pages[0] if ctx.pages else ctx.new_page()).goto(fake.url + "/login-ok")
        ctx.close()


def video(tmp_path) -> Path:
    path = tmp_path / "book.mp4"
    path.write_bytes(b"\0" * 2048)
    return path


def test_sign_in_opens_a_plain_window_on_jarvis_profile_and_the_session_lasts(fake_studio, tmp_path):
    profile = tmp_path / "youtube_browser"
    launched = []

    def run_browser(cmd):
        launched.append(cmd)
        sign_in_by_hand(fake_studio, profile)
        return type("Proc", (), {"poll": lambda self: 0, "terminate": lambda self: None})()

    up = make(fake_studio, profile, browser_cmd=["edge.exe"], run_browser=run_browser)
    assert up.sign_in() == {"id": "UCfake1234567890", "title": "Justin Reads"}
    [cmd] = launched
    assert cmd[0] == "edge.exe" and f"--user-data-dir={profile}" in cmd and cmd[-1].endswith("/ServiceLogin")
    assert not any("remote-debugging" in a or "automation" in a for a in cmd)  # Google refuses automated sign-ins
    state = json.loads((profile / "Local State").read_text())
    assert state["background_mode"]["enabled"] is False  # otherwise Edge never exits when its window closes
    assert make(fake_studio, profile).channel()["title"] == "Justin Reads"  # a fresh Jarvis still signed in
    up.forget()
    with pytest.raises(PublishError, match="signed out"):
        make(fake_studio, profile).channel()


def test_sign_in_gives_up_when_the_window_is_left_open_too_long(fake_studio, tmp_path):
    ended = []
    proc = type("Proc", (), {"poll": lambda self: None, "terminate": lambda self: ended.append(True)})()
    t = [0.0]
    up = make(fake_studio, tmp_path / "p", browser_cmd=["edge.exe"], run_browser=lambda cmd: proc,
              sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    with pytest.raises(PublishError, match="too long"):
        up.sign_in(timeout_s=5)
    assert ended == [True]


def test_private_upload_fills_studio_and_saves_only_after_the_file_is_up(fake_studio, tmp_path):
    profile = tmp_path / "youtube_browser"
    sign_in_by_hand(fake_studio, profile)
    up = make(fake_studio, profile)
    seen = []
    result = up.upload(video(tmp_path), "The Iron Gale", "A storm-witch.\n\nNarrated with an AI voice.",
                       ["fantasy", "audiobook"], "private", progress=seen.append)
    assert result == {"id": VIDEO, "url": f"https://youtu.be/{VIDEO}", "notes": []}
    deadline = time.time() + 5
    while not fake_studio.submitted and time.time() < deadline:
        time.sleep(0.05)
    [form] = fake_studio.submitted
    assert form["title"] == "The Iron Gale" and "AI voice" in form["description"]
    assert form["kids"] == "VIDEO_MADE_FOR_KIDS_NOT_MFK" and form["altered"] == "VIDEO_HAS_ALTERED_CONTENT_YES"
    assert form["visibility"] == "PRIVATE" and form["file"] == "book.mp4"
    assert form["tags"].startswith("fantasy,audiobook")
    assert "Upload complete" in form["progress"]  # never saved half-uploaded
    assert seen[-1] == 1.0 and seen == sorted(seen)
    assert up.http.asked == [f"https://youtu.be/{VIDEO}"]  # privacy double-checked from outside


def test_private_upload_that_youtube_shows_publicly_is_reported(fake_studio, tmp_path):
    profile = tmp_path / "p"
    sign_in_by_hand(fake_studio, profile)
    with pytest.raises(PublishError, match="meant to be private"):
        make(fake_studio, profile, status=200).upload(video(tmp_path), "T", "D", [], "private")


def test_never_saves_when_the_private_choice_does_not_take(fake_studio, tmp_path):
    profile = tmp_path / "p"
    sign_in_by_hand(fake_studio, profile)
    fake_studio.scenario = "stuck_private"
    with pytest.raises(PublishError, match="choosing 'private'") as err:
        make(fake_studio, profile).upload(video(tmp_path), "T", "D", [], "private")
    assert err.value.retryable is False
    time.sleep(0.3)
    assert fake_studio.submitted == []
    assert (tmp_path / "youtube_studio_error.png").exists()


def test_studio_error_message_is_passed_on_and_limits_can_be_retried(fake_studio, tmp_path):
    profile = tmp_path / "p"
    sign_in_by_hand(fake_studio, profile)
    fake_studio.scenario = "limit"
    with pytest.raises(PublishError, match="Daily upload limit reached") as err:
        make(fake_studio, profile).upload(video(tmp_path), "T", "D", [], "unlisted")
    assert err.value.retryable and fake_studio.submitted == []


def test_stalled_upload_fails_as_retryable_and_cancel_stops_it(fake_studio, tmp_path):
    profile = tmp_path / "p"
    sign_in_by_hand(fake_studio, profile)
    fake_studio.scenario = "stall"
    with pytest.raises(PublishError, match="stopped moving") as err:
        make(fake_studio, profile, stall_s=1.0).upload(video(tmp_path), "T", "D", [], "private")
    assert err.value.retryable and fake_studio.submitted == []
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(PublishError, match="cancelled"):
        make(fake_studio, profile).upload(video(tmp_path), "T", "D", [], "private", cancel=cancel)


def test_signed_out_profile_asks_to_connect_again_and_bad_input_is_refused(fake_studio, tmp_path):
    up = make(fake_studio, tmp_path / "p")
    with pytest.raises(PublishError, match="Connect YouTube"):
        up.upload(video(tmp_path), "T", "D", [], "private")
    with pytest.raises(PublishError, match="privacy"):
        up.upload(video(tmp_path), "T", "D", [], "everyone")
    with pytest.raises(PublishError, match="missing"):
        up.upload(tmp_path / "nope.mp4", "T", "D", [], "private")


def test_video_ids_come_from_any_studio_link():
    assert video_id_from("https://youtu.be/abcdefghijk") == "abcdefghijk"
    assert video_id_from("https://studio.youtube.com/video/abcdefghijk/edit") == "abcdefghijk"
    assert video_id_from("https://www.youtube.com/watch?v=abcdefghijk&t=1") == "abcdefghijk"
    assert video_id_from("") == ""
