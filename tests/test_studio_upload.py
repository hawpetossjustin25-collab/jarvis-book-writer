"""YouTube Studio website route, driven in a real browser against a local stand-in for Studio's pages."""
from __future__ import annotations

import glob
import http.server
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
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
<ytcp-thumbnail-uploader><input type=file id=file-loader></ytcp-thumbnail-uploader>
<ytcp-video-metadata-playlists><ytcp-dropdown-trigger id=pl-open>Select</ytcp-dropdown-trigger></ytcp-video-metadata-playlists>
<button id=toggle-button>Show more</button>
<div id=more style="display:none">
 <tp-yt-paper-radio-button name=VIDEO_HAS_ALTERED_CONTENT_YES data-group=alt aria-checked=false>altered</tp-yt-paper-radio-button>
 <div id=tags-container><input id=text-input></div>
</div>
<div id=vis style="display:none">
 <tp-yt-paper-radio-button name=PRIVATE data-group=vis aria-checked=false>Private</tp-yt-paper-radio-button>
 <tp-yt-paper-radio-button name=UNLISTED data-group=vis aria-checked=false>Unlisted</tp-yt-paper-radio-button>
 <tp-yt-paper-radio-button name=PUBLIC data-group=vis aria-checked=false>Public</tp-yt-paper-radio-button>
 <button id=second-container-expand-button>Schedule</button>
 <div id=sched style="display:none"><button id=datepicker-trigger>date</button>
  <ytcp-date-picker style="display:none"><input id=date></ytcp-date-picker>
  <div id=time-of-day-container><input id=tod></div></div>
</div>
<ytcp-video-info><a href="">link</a></ytcp-video-info>
<ytcp-video-upload-progress><span class=progress-label></span></ytcp-video-upload-progress>
<div class=error-short></div>
<button id=next-button>Next</button> <button id=done-button>Save</button>
</ytcp-uploads-dialog>
<ytcp-playlist-dialog id=pld style="display:none">
 <ytcp-search-bar><input id=search-input placeholder="Search for a playlist"></ytcp-search-bar>
 <div id=pl-items></div>
 <button class=new-playlist-button>New playlist</button> <ytcp-button class=done-button>Done</ytcp-button>
</ytcp-playlist-dialog>
<ytcp-playlist-creation-dialog id=plc style="display:none">
 <div id=title-textarea><textarea id=textbox></textarea></div>
 <ytcp-dropdown-trigger id=plvis>Public</ytcp-dropdown-trigger>
 <div id=plvis-menu style="display:none"><tp-yt-paper-item test-id=PRIVATE>Private</tp-yt-paper-item>
  <tp-yt-paper-item test-id=UNLISTED>Unlisted</tp-yt-paper-item><tp-yt-paper-item test-id=PUBLIC>Public</tp-yt-paper-item></div>
 <button id=close-button aria-label=Close>x</button> <button id=create-button>Create</button>
</ytcp-playlist-creation-dialog><script>
const SCENARIO = "%SCENARIO%";
let nexts = 0, file = "", thumb = "", created = null, picked = [];
const $ = id => document.getElementById(id), show = (id, on) => $(id).style.display = on ? 'block' : 'none';
$('file-loader').onchange = e => thumb = e.target.files[0].name;
$('pl-open').onclick = () => show('pld', true);
document.querySelector('.new-playlist-button').onclick = () => show('plc', true);
$('plvis').onclick = () => show('plvis-menu', true);
document.querySelectorAll('#plvis-menu tp-yt-paper-item').forEach(i => i.onclick = () => {
  if (SCENARIO !== 'stuck_playlist_vis') $('plvis').textContent = 'Visibility ' + i.textContent;
  show('plvis-menu', false);
});
// Like Studio: the checkbox has no text (the name is a sibling span), only the first rows are drawn until
// you search, and Escape does nothing.
const lists = ['Instrumentals', 'Health', 'Mix', 'Old Stories'].map(n => ({name: n, on: false}));
const draw = () => {
  const q = $('search-input').value.toLowerCase();
  const rows = lists.filter(l => !q || l.name.toLowerCase().includes(q)).slice(0, 3);
  $('pl-items').innerHTML = '';
  rows.forEach(l => {
    const row = document.createElement('label'); row.className = 'ytcp-checkbox-label';
    row.innerHTML = '<ytcp-checkbox-lit><div role=checkbox style="display:inline-block;width:16px;height:16px;border:1px solid" aria-checked=' + l.on + '></div></ytcp-checkbox-lit>' +
      '<span class=checkbox-label><span class="label label-text"></span></span>';
    row.querySelector('.label-text').textContent = l.name;
    row.querySelector('[role=checkbox]').onclick = () => { l.on = !l.on; draw(); };
    $('pl-items').appendChild(row);
  });
};
$('search-input').oninput = draw; draw();
$('close-button').onclick = () => show('plc', false);
$('create-button').onclick = () => {
  created = {title: document.querySelector('#plc textarea').value, vis: $('plvis').textContent};
  lists.push({name: created.title, on: SCENARIO !== 'new_playlist_unticked'}); show('plc', false); draw();
};
document.querySelector('#pld .done-button').onclick = () => {
  picked = lists.filter(l => l.on).map(l => l.name);
  show('pld', false);
};
$('second-container-expand-button').onclick = () => {
  show('sched', true);
  document.querySelectorAll('[data-group=vis]').forEach(o => o.setAttribute('aria-checked', 'false'));
};
$('datepicker-trigger').onclick = () => document.querySelector('ytcp-date-picker').style.display = 'block';
$('date').addEventListener('change', e => { if (SCENARIO === 'bad_date') e.target.value = 'Jan 1, 2030'; });
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
  tags: document.getElementById('text-input').value, progress: label.textContent, thumb, created, picked,
  date: $('sched').style.display === 'block' ? $('date').value : '', time: $('tod').value})});
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
    proc = type("Proc", (), {"pid": 1, "poll": lambda self: None, "terminate": lambda self: ended.append(True)})()
    t = [0.0]
    up = make(fake_studio, tmp_path / "p", browser_cmd=["edge.exe"], run_browser=lambda cmd: proc,
              sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0],
              titles=lambda pid: ["Sign in - Google Accounts"])
    with pytest.raises(PublishError, match="too long"):
        up.sign_in(timeout_s=5)
    assert ended == [True]


def test_sign_in_window_closes_itself_once_studio_is_showing(fake_studio, tmp_path):
    profile = tmp_path / "youtube_browser"
    sign_in_by_hand(fake_studio, profile)
    state = {"open": True, "closed_at": None}
    t = [0.0]

    class Proc:
        pid = 4242

        def poll(self):
            return None if state["open"] else 0

        def terminate(self):
            state["open"] = False

    def close(pid):
        assert pid == 4242
        state["closed_at"] = t[0]
        state["open"] = False

    titles = lambda pid: ["Sign in - Google Accounts"] if t[0] < 5 else ["Channel dashboard - YouTube Studio"]
    up = make(fake_studio, profile, browser_cmd=["edge.exe"], run_browser=lambda cmd: Proc(),
              sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0], titles=titles, close=close)
    assert up.sign_in()["title"] == "Justin Reads"
    assert state["closed_at"] == 15  # Studio showed at 5 s, then 10 s for Edge to save the sign-in


def test_lingering_edge_is_ended_after_its_window_closes(fake_studio, tmp_path):
    profile = tmp_path / "p"
    sign_in_by_hand(fake_studio, profile)
    ended = []
    t = [0.0]
    proc = type("Proc", (), {"pid": 7, "poll": lambda self: 0 if ended else None,
                             "terminate": lambda self: ended.append(t[0])})()
    up = make(fake_studio, profile, browser_cmd=["edge.exe"], run_browser=lambda cmd: proc,
              sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0],
              titles=lambda pid: ["YouTube Studio"], close=lambda pid: None)
    assert up.sign_in()["title"] == "Justin Reads"
    assert ended and 40 <= ended[0] <= 42


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


def test_aborted_studio_loads_are_tried_again_but_other_failures_are_not(tmp_path):
    class Page:
        def __init__(self, failures):
            self.failures, self.calls = list(failures), 0

        def goto(self, url, wait_until=None):
            self.calls += 1
            if self.failures:
                raise RuntimeError(self.failures.pop(0))

    up = StudioUploader(tmp_path / "p", Lookup(401), sleep=lambda s: None)
    page = Page(["Page.goto: net::ERR_ABORTED at https://studio.youtube.com/"] * 2)
    up._goto(page, "https://studio.youtube.com/")
    assert page.calls == 3
    with pytest.raises(RuntimeError, match="ERR_ABORTED"):
        up._goto(Page(["net::ERR_ABORTED"] * 3), "u")
    page = Page(["Timeout 60000ms exceeded"])
    with pytest.raises(RuntimeError, match="Timeout"):
        up._goto(page, "u")
    assert page.calls == 1


def submitted(fake) -> dict:
    deadline = time.time() + 5
    while not fake.submitted and time.time() < deadline:
        time.sleep(0.05)
    [form] = fake.submitted
    return form


def test_thumbnail_and_a_new_playlist_with_the_video_visibility_are_set(fake_studio, tmp_path):
    profile = tmp_path / "p"
    sign_in_by_hand(fake_studio, profile)
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png")
    result = make(fake_studio, profile).upload(video(tmp_path), "T", "D", [], "unlisted",
                                               playlist="The Iron Gale (Audiobook)", thumbnail=cover)
    assert result["notes"] == []
    form = submitted(fake_studio)
    assert form["thumb"] == "cover.png" and form["visibility"] == "UNLISTED"
    assert form["created"] == {"title": "The Iron Gale (Audiobook)", "vis": "Visibility Unlisted"}
    assert form["picked"] == ["The Iron Gale (Audiobook)"]


def test_an_existing_playlist_is_ticked_not_made_again(fake_studio, tmp_path):
    profile = tmp_path / "p"
    sign_in_by_hand(fake_studio, profile)
    assert make(fake_studio, profile).upload(video(tmp_path), "T", "D", [], "private",
                                             playlist="Old Stories")["notes"] == []
    form = submitted(fake_studio)
    assert form["created"] is None and form["picked"] == ["Old Stories"] and form["thumb"] == ""


def test_a_playlist_that_would_be_public_is_not_made_and_the_video_still_posts(fake_studio, tmp_path):
    profile = tmp_path / "p"
    sign_in_by_hand(fake_studio, profile)
    fake_studio.scenario = "stuck_playlist_vis"  # also: the creation window must be closed by its X
    result = make(fake_studio, profile).upload(video(tmp_path), "T", "D", [], "private", playlist="Secret Book",
                                               thumbnail=tmp_path / "gone.png")
    assert result["id"] == VIDEO
    assert any("couldn't make the new playlist private" in n for n in result["notes"])
    assert any("cover file gone.png is missing" in n for n in result["notes"])
    form = submitted(fake_studio)
    assert form["created"] is None and form["picked"] == [] and form["visibility"] == "PRIVATE"


def test_scheduled_upload_sets_studio_date_and_time_and_stays_private(fake_studio, tmp_path):
    from jarvis_books.publish.studio import _clock, _date
    profile = tmp_path / "p"
    sign_in_by_hand(fake_studio, profile)
    when = datetime(2099, 3, 4, 21, 5, tzinfo=timezone.utc)
    up = make(fake_studio, profile)
    up.upload(video(tmp_path), "T", "D", [], "public", publish_at=when)
    form = submitted(fake_studio)
    local = when.astimezone()
    assert form["date"] == _date(local) and form["time"] == _clock(local)
    assert form["visibility"] == ""  # scheduled, not published now
    assert up.http.asked == [f"https://youtu.be/{VIDEO}"]  # checked as private from outside
    assert _date(datetime(2026, 12, 1, 9, 0)) == "Dec 1, 2026" and _clock(datetime(2026, 12, 1, 0, 7)) == "12:07 AM"
    with pytest.raises(PublishError, match="passed"):
        up.upload(video(tmp_path), "T", "D", [], "private", publish_at=datetime(2020, 1, 1, tzinfo=timezone.utc))


def test_schedule_studio_changes_is_never_saved(fake_studio, tmp_path):
    profile = tmp_path / "p"
    sign_in_by_hand(fake_studio, profile)
    fake_studio.scenario = "bad_date"
    with pytest.raises(PublishError, match="instead of"):
        make(fake_studio, profile).upload(video(tmp_path), "T", "D", [], "private",
                                          publish_at=datetime(2099, 3, 4, 21, 5, tzinfo=timezone.utc))
    time.sleep(0.3)
    assert fake_studio.submitted == []


def test_a_killed_browser_profile_is_marked_clean_so_old_tabs_dont_abort_studio(tmp_path):
    from jarvis_books.publish.studio import mark_clean_exit
    prefs = tmp_path / "p" / "Default" / "Preferences"
    mark_clean_exit(tmp_path / "p")  # a brand-new profile has nothing to fix
    prefs.parent.mkdir(parents=True)
    prefs.write_text(json.dumps({"profile": {"exit_type": "Crashed", "exited_cleanly": False, "name": "Jarvis"},
                                 "account_info": [{"email": "j@x"}]}), encoding="utf-8")
    mark_clean_exit(tmp_path / "p")
    data = json.loads(prefs.read_text(encoding="utf-8"))
    assert data["profile"] == {"exit_type": "Normal", "exited_cleanly": True, "name": "Jarvis"}
    assert data["account_info"] == [{"email": "j@x"}]
    prefs.write_text("{not json", encoding="utf-8")
    mark_clean_exit(tmp_path / "p")  # a damaged file is left for the browser to repair
    assert prefs.read_text(encoding="utf-8") == "{not json"


def test_a_new_playlist_studio_leaves_unticked_is_ticked(fake_studio, tmp_path):
    profile = tmp_path / "p"
    sign_in_by_hand(fake_studio, profile)
    fake_studio.scenario = "new_playlist_unticked"
    assert make(fake_studio, profile).upload(video(tmp_path), "T", "D", [], "private",
                                             playlist="Book Two (Audiobook)")["notes"] == []
    assert submitted(fake_studio)["picked"] == ["Book Two (Audiobook)"]
