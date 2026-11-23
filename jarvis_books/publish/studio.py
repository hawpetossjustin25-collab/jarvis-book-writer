"""Posting through the YouTube Studio website, in Jarvis's own Edge profile.

This route needs no Google Cloud project, OAuth client or 2-step verification: Justin signs in to YouTube once in a
plain Edge window that uses Jarvis's profile folder, and afterwards Jarvis drives YouTube Studio in that profile
(hidden) to upload. Studio's page structure can change, so every step names itself in its error and leaves a
screenshot next to the profile, and a video is never left more visible than asked: it stays a private draft unless
the chosen visibility is confirmed as selected, and a "private" upload is checked from outside before it counts.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from jarvis_books.publish.errors import PublishError

log = logging.getLogger(__name__)

STUDIO_URL = "https://studio.youtube.com/"
UPLOAD_URL = "https://www.youtube.com/upload"
SIGN_IN_URL = ("https://accounts.google.com/ServiceLogin?service=youtube&passive=true"
               "&continue=https%3A%2F%2Fstudio.youtube.com%2F")
OEMBED_URL = "https://www.youtube.com/oembed"
VISIBILITY = {"private": "PRIVATE", "unlisted": "UNLISTED", "public": "PUBLIC"}
SIGN_IN_MARKERS = ("accounts.google.", "/servicelogin", "/signin", "/v3/signin")

# Everything Studio-specific lives here, so a Studio redesign is a one-place fix.
SEL = {
    "channel": "#entity-name, ytcp-navigation-drawer #channel-title, #channel-title",
    "file": "input[type=file]",
    "title": "#title-textarea #textbox",
    "description": "#description-textarea #textbox",
    "not_for_kids": "tp-yt-paper-radio-button[name=VIDEO_MADE_FOR_KIDS_NOT_MFK]",
    "show_more": "ytcp-uploads-details #toggle-button, #toggle-button",
    "altered_yes": "tp-yt-paper-radio-button[name=VIDEO_HAS_ALTERED_CONTENT_YES]",
    "tags": "#tags-container input#text-input, ytcp-free-text-chip-bar input",
    "next": "#next-button",
    "visibility": "tp-yt-paper-radio-button[name={}]",
    "done": "#done-button",
    "video_url": "ytcp-video-info a[href*='youtu'], .video-url-fadeable a[href*='youtu']",
    "progress": "ytcp-video-upload-progress .progress-label, .progress-label",
    "error": "ytcp-uploads-dialog .error-short, ytcp-uploads-dialog .error-area, #error-message",
}
UPLOADED = re.compile(r"upload complete|uploaded|processing|checks|finished|100\s*%", re.I)
VIDEO_ID = re.compile(r"(?:youtu\.be/|[?&]v=|/video/)([\w-]{11})")
CHANNEL_ID = re.compile(r"/channel/(UC[\w-]+)")
LIMIT_WORDS = ("limit", "try again later", "too many")


def find_edge() -> str:
    """Microsoft Edge on this Windows machine ('' when it isn't installed)."""
    for root in (os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                 os.environ.get("PROGRAMFILES", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
        path = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if root and path.exists():
            return str(path)
    return shutil.which("msedge") or ""


def video_id_from(url: str) -> str:
    m = VIDEO_ID.search(url or "")
    return m.group(1) if m else ""


class StudioUploader:
    """Uploads one video at a time through YouTube Studio with a persistent browser profile."""

    def __init__(self, profile: Path, http, launch: Optional[dict] = None, studio_url: str = STUDIO_URL,
                 upload_url: str = UPLOAD_URL, sign_in_url: str = SIGN_IN_URL, oembed_url: str = OEMBED_URL,
                 browser_cmd: Optional[list[str]] = None, run_browser: Callable = subprocess.Popen,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 step_timeout_ms: int = 60_000, stall_s: float = 600.0, poll_s: float = 2.0):
        self.profile = Path(profile)
        self.http = http
        self.launch = launch if launch is not None else {"channel": "msedge"}
        self.studio_url, self.upload_url, self.sign_in_url, self.oembed_url = (studio_url, upload_url, sign_in_url,
                                                                               oembed_url)
        self.browser_cmd = browser_cmd
        self.run_browser = run_browser
        self.sleep, self.clock = sleep, clock
        self.step_timeout_ms, self.stall_s, self.poll_s = step_timeout_ms, stall_s, poll_s
        self._lock = threading.Lock()  # one browser on the profile at a time

    # ---- signing in -----------------------------------------------------------------
    def sign_in(self, cancel: Optional[threading.Event] = None, timeout_s: float = 1200.0) -> dict:
        """Open a normal Edge window on Jarvis's profile (no automation, so Google allows the sign-in), wait for
        it to be closed, then confirm the channel from the saved session."""
        self.profile.mkdir(parents=True, exist_ok=True)
        self._no_background_mode()
        cmd = self.browser_cmd or [find_edge()]
        if not cmd[0]:
            raise PublishError("Microsoft Edge is needed to sign in to YouTube, and it isn't installed")
        proc = self.run_browser(cmd + [f"--user-data-dir={self.profile}", "--no-first-run",
                                       "--no-default-browser-check", "--new-window", self.sign_in_url])
        deadline = self.clock() + timeout_s
        while proc.poll() is None:
            if (cancel is not None and cancel.is_set()) or self.clock() > deadline:
                proc.terminate()
                raise PublishError("YouTube sign-in was cancelled" if cancel is not None and cancel.is_set()
                                   else "YouTube sign-in took too long; try Connect YouTube again")
            self.sleep(1.0)
        return self.channel()

    def _no_background_mode(self) -> None:
        """Edge keeps running after its window closes when background mode is on; then sign-in would never end."""
        state_path = self.profile / "Local State"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        except (OSError, ValueError):
            state = {}
        state.setdefault("background_mode", {})["enabled"] = False
        state_path.write_text(json.dumps(state), encoding="utf-8")

    def channel(self) -> dict:
        """The signed-in channel's name and id, or a PublishError asking to connect again."""
        with self._session() as page:
            self._open_studio(page)
            return self._read_channel(page)

    def forget(self) -> None:
        """Sign out by removing the profile (cookies and all)."""
        shutil.rmtree(self.profile, ignore_errors=True)

    # ---- uploading ------------------------------------------------------------------
    def upload(self, path: Path, title: str, description: str, tags: list[str], privacy: str,
               progress: Optional[Callable[[float], None]] = None,
               cancel: Optional[threading.Event] = None) -> dict:
        """Upload `path`, returning {"id", "url", "notes"} once YouTube has the whole file."""
        if privacy not in VISIBILITY:
            raise PublishError(f"privacy must be one of {', '.join(VISIBILITY)}")
        path = Path(path)
        if not path.exists():
            raise PublishError(f"the video file is missing: {path.name}", retryable=True)
        notes: list[str] = []
        with self._session() as page:
            step = "opening YouTube Studio"
            try:
                self._open_studio(page)
                step = "opening the upload window"
                page.goto(self.upload_url, wait_until="domcontentloaded")
                self._signed_in_or_raise(page)
                step = "choosing the video file"
                page.locator(SEL["file"]).first.set_input_files(str(path), timeout=self.step_timeout_ms)
                step = "entering the title"
                self._fill(page, SEL["title"], title[:100])
                step = "entering the description"
                self._fill(page, SEL["description"], description[:5000])
                step = "marking it not made for kids"
                self._choose(page, SEL["not_for_kids"])
                notes += self._extras(page, tags)
                step = "moving to the visibility page"
                for _ in range(3):
                    self._raise_studio_error(page)
                    page.locator(SEL["next"]).first.click(timeout=self.step_timeout_ms)
                step = f"choosing '{privacy}'"
                self._choose(page, SEL["visibility"].format(VISIBILITY[privacy]))
                step = "reading the video link"
                video_url = self._video_url(page)
                step = "waiting for the upload"
                self._wait_uploaded(page, progress, cancel)
                step = f"checking '{privacy}' is still chosen"
                if not self._checked(page, SEL["visibility"].format(VISIBILITY[privacy])):
                    raise PublishError("the visibility changed before saving")
                step = "saving"
                page.locator(SEL["done"]).first.click(timeout=self.step_timeout_ms)
                self.sleep(self.poll_s)
                self._raise_studio_error(page)
            except PublishError as exc:
                self._screenshot(page)
                raise PublishError(f"YouTube Studio, {step}: {exc}", retryable=exc.retryable) from None
            except Exception as exc:  # Playwright timeouts and page changes
                self._screenshot(page)
                log.warning("Studio upload failed while %s: %s", step, exc)
                raise PublishError(f"YouTube Studio, {step}: the page didn't respond as expected "
                                   f"({type(exc).__name__}). Any upload stays a private draft.",
                                   retryable=True) from None
        video_id = video_id_from(video_url)
        if privacy == "private":
            self.confirm_private(video_id)
        return {"id": video_id, "url": f"https://youtu.be/{video_id}", "notes": notes}

    def confirm_private(self, video_id: str) -> None:
        """A private video is invisible to anyone signed out; if YouTube's public lookup finds it, say so."""
        try:
            resp = self.http.get(self.oembed_url, params={"url": f"https://youtu.be/{video_id}", "format": "json"},
                                 timeout=30)
        except Exception as exc:
            log.info("could not double-check privacy of %s: %s", video_id, exc)
            return
        if resp.status_code == 200:
            raise PublishError(f"video {video_id} was meant to be private but YouTube shows it publicly; "
                               "open it in YouTube Studio and set it to Private")

    # ---- browser plumbing ---------------------------------------------------------------
    def _session(self):
        return _Session(self)

    def _open_studio(self, page) -> None:
        page.goto(self.studio_url, wait_until="domcontentloaded")
        self._signed_in_or_raise(page)

    def _signed_in_or_raise(self, page) -> None:
        url = (page.url or "").lower()
        if any(m in url for m in SIGN_IN_MARKERS):
            raise PublishError("YouTube is signed out in Jarvis's browser. Click Connect YouTube to sign in again")

    def _read_channel(self, page) -> dict:
        name = ""
        try:
            el = page.locator(SEL["channel"]).first
            el.wait_for(state="attached", timeout=self.step_timeout_ms)
            name = (el.text_content() or "").strip()
        except Exception:
            pass
        m = CHANNEL_ID.search(page.url or "")
        if not name and not m:
            raise PublishError("this Google account has no YouTube channel yet. Create one at youtube.com, then "
                               "connect again")
        return {"id": m.group(1) if m else "", "title": name or "YouTube channel"}

    def _fill(self, page, selector: str, text: str) -> None:
        box = page.locator(selector).first
        box.wait_for(state="visible", timeout=self.step_timeout_ms)
        box.fill(text, timeout=self.step_timeout_ms)

    def _checked(self, page, selector: str) -> bool:
        el = page.locator(selector).first
        return (el.get_attribute("aria-checked") or "").lower() == "true" or el.get_attribute("checked") is not None

    def _choose(self, page, selector: str) -> None:
        page.locator(selector).first.click(timeout=self.step_timeout_ms)
        for _ in range(10):
            if self._checked(page, selector):
                return
            self.sleep(0.2)
        raise PublishError("the choice didn't take")

    def _extras(self, page, tags: list[str]) -> list[str]:
        """The AI label and tags sit under 'Show more'. Missing them isn't fatal: the description says it's AI."""
        notes = []
        quick = min(self.step_timeout_ms, 10_000)
        try:
            page.locator(SEL["show_more"]).first.click(timeout=quick)
        except Exception:
            pass
        try:
            page.locator(SEL["altered_yes"]).first.click(timeout=quick)
            if not self._checked(page, SEL["altered_yes"]):
                raise PublishError("not selected")
        except Exception:
            notes.append("the AI-content label wasn't found in Studio (the description still says it's AI-narrated)")
        if tags:
            try:
                box = page.locator(SEL["tags"]).first
                box.fill(",".join(tags) + ",", timeout=quick)
                box.press("Enter", timeout=quick)
            except Exception:
                notes.append("tags weren't added")
        return notes

    def _video_url(self, page) -> str:
        link = page.locator(SEL["video_url"]).first
        deadline = self.clock() + self.step_timeout_ms / 1000
        while True:
            href = ""
            try:
                href = link.get_attribute("href", timeout=self.step_timeout_ms) or link.text_content() or ""
            except Exception:
                pass
            if video_id_from(href):
                return href
            if self.clock() > deadline:
                raise PublishError("YouTube didn't give the video a link")
            self.sleep(self.poll_s)

    def _raise_studio_error(self, page) -> None:
        for el in page.locator(SEL["error"]).all():
            try:
                text = (el.text_content() or "").strip() if el.is_visible() else ""
            except Exception:
                text = ""
            if text:
                raise PublishError(text, retryable=any(w in text.lower() for w in LIMIT_WORDS))

    def _wait_uploaded(self, page, progress, cancel) -> None:
        last, last_change = -1.0, self.clock()
        label = page.locator(SEL["progress"]).first
        while True:
            if cancel is not None and cancel.is_set():
                raise PublishError("the upload was cancelled; it stays a private draft in YouTube Studio")
            self._raise_studio_error(page)
            try:
                text = (label.text_content(timeout=self.step_timeout_ms) or "").strip()
            except Exception:
                text = ""
            if UPLOADED.search(text) and not re.search(r"uploading", text, re.I):
                if progress:
                    progress(1.0)
                return
            m = re.search(r"(\d{1,3})\s*%", text)
            frac = int(m.group(1)) / 100 if m else last
            if frac != last:
                last, last_change = frac, self.clock()
                if progress and frac >= 0:
                    progress(frac)
            elif self.clock() - last_change > self.stall_s:
                raise PublishError(f"the upload stopped moving ({text or 'no progress shown'})", retryable=True)
            self.sleep(self.poll_s)

    def _screenshot(self, page) -> None:
        try:
            page.screenshot(path=str(self.profile.parent / "youtube_studio_error.png"))
        except Exception:
            pass


class _Session:
    """Opens the profile hidden first; if YouTube treats the hidden browser as signed out, tries once more with a
    real window parked off-screen. Always closes the browser so the profile is free for the next job."""

    def __init__(self, owner: StudioUploader):
        self.o = owner
        self._pw = self._ctx = None

    def __enter__(self):
        self.o._lock.acquire()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.o._lock.release()
            raise PublishError("the Playwright package is missing; reinstall Jarvis") from None
        try:
            self._pw = sync_playwright().start()
            self._ctx = self._launch(headless=True)
            page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            page.set_default_timeout(self.o.step_timeout_ms)
            page.goto(self.o.studio_url, wait_until="domcontentloaded")
            if any(m in (page.url or "").lower() for m in SIGN_IN_MARKERS):
                self._ctx.close()
                self._ctx = self._launch(headless=False)
                page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
                page.set_default_timeout(self.o.step_timeout_ms)
            return page
        except PublishError:
            self.__exit__(None, None, None)
            raise
        except Exception as exc:
            self.__exit__(None, None, None)
            text = str(exc)
            if "ProcessSingleton" in text or "user data directory is already in use" in text.lower():
                raise PublishError("Jarvis's YouTube browser window is still open; close it and try again",
                                   retryable=True) from None
            raise PublishError(f"could not start Jarvis's YouTube browser: {text.splitlines()[0][:200]}",
                               retryable=True) from None

    def _launch(self, headless: bool):
        args = ["--no-first-run", "--no-default-browser-check"]
        if not headless:
            args += ["--window-position=-32000,-32000", "--window-size=1280,900"]
        return self._pw.chromium.launch_persistent_context(
            str(self.o.profile), headless=headless, args=args, viewport={"width": 1280, "height": 900},
            **self.o.launch)

    def __exit__(self, *exc):
        try:
            if self._ctx is not None:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = self._pw = None
        self.o._lock.release()
        return False
