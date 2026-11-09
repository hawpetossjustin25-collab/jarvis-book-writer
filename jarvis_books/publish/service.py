"""Publishing service: connected accounts, preparing audiobook videos and background uploads."""
from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable, Optional

import requests

from jarvis_books.audio.export import _run, chapter_audio, ffmpeg_path
from jarvis_books.publish.errors import PublishError
from jarvis_books.publish.oauth import AccountStore, GoogleOAuth, load_client
from jarvis_books.publish.video import chapter_timestamps, render_video
from jarvis_books.publish.youtube import SCOPES, YouTube
from jarvis_books.storage.models import Post
from jarvis_books.text import safe_filename

log = logging.getLogger(__name__)

PRIVACY = ("private", "unlisted", "public")
AI_DISCLOSURE = "Narrated with an AI voice (Google Gemini)."
CATEGORY_ENTERTAINMENT = "24"


def _clean(text: str, limit: int) -> str:
    return (text or "").replace("<", "").replace(">", "").strip()[:limit]  # YouTube rejects angle brackets


class Publisher:
    def __init__(self, home: Path, library, narrator, jobs, http=None,
                 open_browser: Callable[[str], object] = webbrowser.open, sleep: Callable[[float], None] = time.sleep):
        self.home = Path(home)
        self.library = library
        self.narrator = narrator
        self.jobs = jobs
        self.http = http or requests.Session()
        self.open_browser = open_browser
        self.sleep = sleep
        self.accounts = AccountStore(self.home)
        self.client_path = self.home / "google_client.json"
        self.out_dir = self.home / "publish"

    # ---- accounts ---------------------------------------------------------------------
    def has_google_client(self) -> bool:
        return self.client_path.exists()

    def import_google_client(self, path: Path) -> None:
        load_client(path)  # validates before replacing anything
        shutil.copyfile(path, self.client_path)

    def _oauth(self) -> GoogleOAuth:
        if not self.has_google_client():
            raise PublishError("YouTube sign-in is not set up yet (no Google client file)")
        return GoogleOAuth(load_client(self.client_path), self.http, self.open_browser)

    def youtube(self) -> YouTube:
        return YouTube(self.accounts, self._oauth(), self.http, sleep=self.sleep)

    def connect_youtube(self, cancel: Optional[threading.Event] = None) -> str:
        tokens = self._oauth().sign_in(SCOPES, cancel)
        self.accounts.put("youtube", {**tokens, "account": "", "connected_at": time.time()})
        try:
            channel = self.youtube().channel()
        except PublishError:
            self.accounts.remove("youtube")
            raise
        self.accounts.put("youtube", {**self.accounts.get("youtube"), "account": channel["title"],
                                      "channel_id": channel["id"]})
        return channel["title"]

    def disconnect(self, provider: str) -> None:
        record = self.accounts.get(provider)
        if record and provider == "youtube" and self.has_google_client():
            self._oauth().revoke(record)
        self.accounts.remove(provider)

    def connected(self) -> dict:
        return self.accounts.summary()

    # ---- posts ------------------------------------------------------------------------
    def _text_hash(self, book_id: str, chapters: list[int]) -> str:
        texts = [c.text for c in self.library.list_chapters(book_id) if c.number in chapters]
        return hashlib.sha256("\n\x00".join(texts).encode()).hexdigest()

    def is_stale(self, post: Post) -> bool:
        """True when a published chapter was edited after it was posted."""
        return bool(post.text_hash) and post.text_hash != self._text_hash(post.book_id, post.chapters)

    def publish_youtube(self, book_id: str, chapters: Optional[list[int]] = None, title: str = "",
                        description: str = "", tags: tuple = (), privacy: str = "private",
                        publish_at: str = "", per_chapter: bool = False, playlist: bool = True,
                        cover: str = "") -> list[Post]:
        if privacy not in PRIVACY:
            raise PublishError(f"privacy must be one of {', '.join(PRIVACY)}")
        if publish_at:
            privacy = "private"  # YouTube only schedules private videos; it goes public at publish_at
        if not self.accounts.get("youtube"):
            raise PublishError("Connect your YouTube account first")
        if not ffmpeg_path():
            raise PublishError("FFmpeg is not available to make the video")
        book = self.library.get_book(book_id)
        written = {c.number: c for c in self.library.list_chapters(book_id) if c.word_count > 0}
        chosen = sorted(set(chapters)) if chapters else sorted(written)
        missing = [n for n in chosen if n not in written]
        if not chosen or missing:
            raise PublishError("these chapters are not written yet: " + ", ".join(map(str, missing or chosen))
                               if missing else "the book has no written chapters yet")
        tag_list, used = [], 0
        for t in (_clean(t, 100) for t in tags):
            if t and used + len(t) + 1 <= 480:  # YouTube allows 500 characters of tags in total
                tag_list.append(t)
                used += len(t) + 1
        groups = [[n] for n in chosen] if per_chapter else [chosen]
        posts = []
        for group in groups:
            heading = written[group[0]].heading
            post_title = _clean(title or book.title, 100) if not per_chapter else \
                _clean(f"{title or book.title} - {heading}", 100)
            settings = {"description": _clean(description or book.logline or book.premise, 4500), "tags": tag_list,
                        "publish_at": publish_at, "playlist": playlist, "cover": cover, "subtitle":
                        heading if len(group) == 1 else f"Chapters {group[0]}-{group[-1]}"}
            post = self.library.add_post(book_id, "youtube", post_title, privacy, group, settings)
            self._submit(post)
            posts.append(post)
        return posts

    def retry(self, post_id: int) -> Post:
        post = self.library.get_post(post_id)
        if post.status not in ("failed", "cancelled"):
            raise PublishError("only failed or cancelled posts can be retried")
        post = self.library.update_post(post_id, status="queued", error="")
        self._submit(post)
        return post

    def _submit(self, post: Post):
        return self.jobs.submit("publish", f"Posting '{post.title}' to YouTube",
                                lambda p, c, pid=post.id: self._run_youtube(pid, p, c), post.book_id)

    # ---- the background job -------------------------------------------------------------
    def _render(self, post: Post, progress, cancel) -> tuple[Path, str]:
        self.library.update_post(post.id, status="rendering", progress=0.0)
        book = self.library.get_book(post.book_id)
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            parts = chapter_audio(self.narrator, post.book_id, tmp, post.chapters,
                                  progress=lambda f, m: progress(f * 0.3, m), cancel=cancel)
            listing = tmp / "all.txt"
            listing.write_text("".join(f"file '{w.as_posix()}'\n" for _, _, w, _ in parts), encoding="utf-8")
            audio = tmp / "all.wav"
            _run([ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                  "-i", str(listing), "-c", "copy", str(audio)])
            total = sum(d for _, _, _, d in parts)
            out = self.out_dir / f"{post.id:05d} - {safe_filename(post.title)}.mp4"
            progress(0.3, "Making the video")
            render_video(audio, out, book.title, post.settings.get("subtitle", ""), post.settings.get("cover") or None,
                         duration_ms=total, progress=lambda f: progress(0.3 + 0.2 * f, "Making the video"),
                         cancel=cancel)
        stamps = chapter_timestamps([(h, d) for _, h, _, d in parts])
        description = "\n\n".join(x for x in (post.settings.get("description", ""), stamps, AI_DISCLOSURE) if x)
        return out, description[:5000]

    def _run_youtube(self, post_id: int, progress, cancel) -> str:
        post = self.library.get_post(post_id)
        try:
            path = Path(post.file_path) if post.file_path else None
            description = post.settings.get("final_description", "")
            if path is None or not path.exists() or not description:
                path, description = self._render(post, progress, cancel)
                post = self.library.update_post(post_id, file_path=str(path), upload_uri="",
                                                text_hash=self._text_hash(post.book_id, post.chapters),
                                                settings={**post.settings, "final_description": description})
            self.library.update_post(post_id, status="uploading")
            s = post.settings
            status = {"privacyStatus": post.privacy, "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}
            if s.get("publish_at"):
                status["publishAt"] = s["publish_at"]
            metadata = {"snippet": {"title": post.title, "description": description, "tags": s.get("tags", []),
                                    "categoryId": CATEGORY_ENTERTAINMENT}, "status": status}
            yt = self.youtube()
            video = yt.upload(path, metadata, session_uri=post.upload_uri,
                              on_session=lambda uri: self.library.update_post(post_id, upload_uri=uri),
                              progress=lambda f: (progress(0.5 + 0.45 * f, "Uploading to YouTube"),
                                                  self.library.update_post(post_id, progress=f)),
                              cancel=cancel)
            video_id = video["id"]
            self.library.update_post(post_id, status="processing", remote_id=video_id, upload_uri="",
                                     url=f"https://youtu.be/{video_id}", progress=1.0)
            notes = self._after_upload(yt, post, video_id)
            self.library.update_post(post_id, status="published", error="; ".join(notes))
            progress(1.0, "Posted to YouTube")
            return f"https://youtu.be/{video_id}"
        except Exception as exc:
            cancelled = cancel is not None and cancel.is_set()
            self.library.update_post(post_id, status="cancelled" if cancelled else "failed", error=str(exc)[:500])
            raise

    def _after_upload(self, yt: YouTube, post: Post, video_id: str) -> list[str]:
        """Playlist and thumbnail are extras: if they fail the video is still published."""
        notes = []
        if post.settings.get("playlist"):
            key = f"youtube.playlist.{post.book_id}"
            try:
                playlist = self.library.get_meta(key)
                if not playlist:
                    book = self.library.get_book(post.book_id)
                    playlist = yt.create_playlist(f"{book.title} (Audiobook)", book.logline or book.premise,
                                                  "public" if post.privacy == "public" else post.privacy)
                    self.library.set_meta(key, playlist)
                yt.add_to_playlist(playlist, video_id)
            except PublishError as exc:
                notes.append(f"playlist not updated: {exc}")
        cover = post.settings.get("cover")
        if cover and Path(cover).exists():
            try:
                yt.set_thumbnail(video_id, Path(cover))
            except PublishError as exc:
                notes.append(f"thumbnail not set (YouTube requires a phone-verified channel): {exc}")
        return notes
