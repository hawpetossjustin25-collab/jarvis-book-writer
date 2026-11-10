"""Audiobook player: plays cached passages in order, recording ahead in the background, so any
book can be listened to while it is still being written."""
from __future__ import annotations

import logging
import threading
from typing import Optional

from PyQt6.QtCore import QObject, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

from jarvis_books.audio.narrator import Narrator, Segment

log = logging.getLogger(__name__)
PREFETCH = 3


class AudiobookPlayer(QObject):
    position_changed = pyqtSignal(int, int, int)  # chapter, segment index, total segments
    state_changed = pyqtSignal(str)
    error = pyqtSignal(str)
    _ready = pyqtSignal(int, str)  # segment index, file path (from worker thread)
    _failed = pyqtSignal(int, str)  # segment index, reason (from worker thread)

    def __init__(self, narrator: Narrator, parent=None):
        super().__init__(parent)
        self.narrator = narrator
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.player.setAudioOutput(self.output)
        if hasattr(self.player, "setPitchCompensation"):  # Qt 6.10+: faster or slower, never chipmunk or slurred
            self.player.setPitchCompensation(True)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(lambda e, msg: self._give_up(f"Could not play audio: {msg}"))
        self._ready.connect(self._on_ready)
        self._failed.connect(self._on_failed)
        self.book_id: Optional[str] = None
        self.segments: list[Segment] = []
        self.index = 0
        self.playing = False
        self._waiting = False
        self._prefetching: set[int] = set()
        self._lock = threading.Lock()

    @property
    def audible(self) -> bool:
        """The audiobook is coming out of the speakers right now."""
        return self.playing and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def set_speed(self, rate: float) -> None:
        self.player.setPlaybackRate(max(0.5, min(3.0, rate)))

    def set_volume(self, value: float) -> None:
        self.output.setVolume(max(0.0, min(1.0, value)))

    def load(self, book_id: str, chapter: int = 0) -> int:
        self.stop()
        self.book_id = book_id
        self.segments = self.narrator.book_segments(book_id)
        self.index = 0
        if chapter:
            self.index = next((i for i, s in enumerate(self.segments) if s.chapter_number >= chapter), 0)
        else:
            saved = self.narrator.library.get_playback(book_id)
            if saved:
                self.index = next((i for i, s in enumerate(self.segments) if s.chapter_number == saved.chapter_number
                                   and s.ord >= saved.segment), self.index)
        return len(self.segments)

    def refresh(self) -> None:
        """Pick up chapters written or edited since playback started (keeps the current position)."""
        if not self.book_id:
            return
        current = self.segments[self.index] if self.segments and self.index < len(self.segments) else None
        self.segments = self.narrator.book_segments(self.book_id)
        if current:
            self.index = next((i for i, s in enumerate(self.segments)
                               if (s.chapter_number, s.ord) >= (current.chapter_number, current.ord)), len(self.segments))

    def play(self) -> None:
        if not self.segments:
            self.error.emit("Nothing to play yet: this book has no written chapters.")
            return
        self.playing = True
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PausedState:
            self.player.play()
            self.state_changed.emit("playing")
            return
        self._play_index(self.index)

    def pause(self) -> None:
        self.playing = False
        self.player.pause()
        self._save()
        self.state_changed.emit("paused")

    def stop(self) -> None:
        self.playing = False
        self.player.stop()
        self._save()
        self.state_changed.emit("stopped")

    def skip(self, delta: int) -> None:
        self.index = max(0, min(len(self.segments) - 1, self.index + delta))
        if self.playing:
            self._play_index(self.index)

    def jump_chapter(self, delta: int) -> None:
        if not self.segments:
            return
        current = self.segments[self.index].chapter_number
        chapters = sorted({s.chapter_number for s in self.segments})
        pos = chapters.index(current) + delta
        if 0 <= pos < len(chapters):
            self.index = next(i for i, s in enumerate(self.segments) if s.chapter_number == chapters[pos])
            if self.playing:
                self._play_index(self.index)

    def _play_index(self, i: int) -> None:
        if i >= len(self.segments):
            self.playing = False
            self.state_changed.emit("finished")
            return
        self.index = i
        seg = self.segments[i]
        self.position_changed.emit(seg.chapter_number, i, len(self.segments))
        path = self.narrator.cached_path(seg)
        if path:
            self._waiting = False
            self.player.setSource(QUrl.fromLocalFile(str(path)))
            self.player.play()
            self.state_changed.emit("playing")
        else:
            self._waiting = True
            self.state_changed.emit("recording")
            self._fetch(i)
        for j in range(i + 1, min(len(self.segments), i + 1 + PREFETCH)):
            self._fetch(j)

    def _fetch(self, i: int) -> None:
        with self._lock:
            if i in self._prefetching:
                return
            self._prefetching.add(i)
        seg = self.segments[i]

        def work():
            try:
                path = self.narrator.ensure(seg)
                self._ready.emit(i, str(path))
            except Exception as exc:
                log.warning("narration failed: %s", exc)
                self._failed.emit(i, f"Could not record audio: {exc}")
            finally:
                with self._lock:
                    self._prefetching.discard(i)
        threading.Thread(target=work, daemon=True).start()

    def _on_ready(self, i: int, path: str) -> None:
        if self._waiting and self.playing and i == self.index:
            self._waiting = False
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.play()
            self.state_changed.emit("playing")

    def _on_failed(self, i: int, reason: str) -> None:
        if i == self.index and self._waiting and self.playing:
            self._give_up(reason)  # the passage it is waiting for can't be recorded: stop instead of sitting silent

    def _give_up(self, reason: str) -> None:
        self.playing = False
        self._waiting = False
        self.player.stop()
        self.state_changed.emit("stopped")
        self.error.emit(reason)

    def _on_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self.playing:
            self._save(next_segment=True)
            self._play_index(self.index + 1)

    def _save(self, next_segment: bool = False) -> None:
        if not self.book_id or not self.segments or self.index >= len(self.segments):
            return
        seg = self.segments[min(self.index + (1 if next_segment else 0), len(self.segments) - 1)]
        self.narrator.library.set_playback(self.book_id, seg.chapter_number, seg.ord, int(self.player.position()))
