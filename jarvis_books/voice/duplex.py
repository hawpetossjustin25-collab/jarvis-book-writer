"""Keeps Jarvis from hearing himself.

Laptop speakers leak into the microphone and the raw Qt microphone stream has no echo cancellation, so
Gemini's voice detector hears Jarvis and interrupts him. While Jarvis is talking (and briefly after) the
microphone is held back from Gemini. A local detector still listens: when the user talks over Jarvis for
about half a sentence, clearly louder than the speaker echo, it is treated as a barge-in. Jarvis stops
and the buffered start of what the user said is sent on, so none of it is lost.
"""
from __future__ import annotations

import array
import math
from collections import deque

FRAME_MS = 20


def rms(pcm16: bytes) -> float:
    samples = array.array("h", pcm16[: len(pcm16) - len(pcm16) % 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class DuplexGate:
    def __init__(self, rate: int = 16000, barge_in_ms: int = 700, window_ms: int = 1000, tail_ms: int = 400,
                 calibrate_ms: int = 300, preroll_ms: int = 1500, floor: float = 700.0, ratio: float = 2.5):
        self.frame_bytes = rate * FRAME_MS // 1000 * 2
        self.barge_frames = max(1, barge_in_ms // FRAME_MS)
        self.window = deque(maxlen=max(self.barge_frames, window_ms // FRAME_MS))
        self.tail_s = tail_ms / 1000
        self.calibrate_frames = calibrate_ms // FRAME_MS
        self.preroll: deque[bytes] = deque(maxlen=preroll_ms // FRAME_MS)
        self.floor, self.ratio = floor, ratio
        self.echo = 0.0  # learned loudness of Jarvis's own voice in the mic; kept across turns
        self.enabled = True
        self._playing = False
        self._quiet_at = 0.0
        self._frames_this_turn = 0
        self._pending = b""

    @property
    def holding(self) -> bool:
        """True while the microphone is being held back from Gemini."""
        return self._playing

    def set_playing(self, playing: bool, now: float) -> None:
        if playing and not self._playing:
            self._playing, self._frames_this_turn = True, 0
            self.window.clear()
            self.preroll.clear()
        elif not playing and self._playing:
            if not self._quiet_at:
                self._quiet_at = now + self.tail_s  # room echo dies down before the mic opens again
            elif now >= self._quiet_at:
                self._release()
        if playing:
            self._quiet_at = 0.0

    def _release(self) -> None:
        self._playing, self._quiet_at = False, 0.0
        self.window.clear()
        self.preroll.clear()

    def threshold(self) -> float:
        return max(self.floor, self.echo * self.ratio)

    def feed(self, pcm16: bytes) -> tuple[bytes, bool]:
        """Returns (audio to send to Gemini now, whether the user just barged in)."""
        if not self.enabled or not self._playing:
            out, self._pending = self._pending + pcm16, b""
            return out, False
        data = self._pending + pcm16
        cut = len(data) - len(data) % self.frame_bytes
        self._pending = data[cut:]
        for i in range(0, cut, self.frame_bytes):
            frame = data[i:i + self.frame_bytes]
            self.preroll.append(frame)
            level = rms(frame)
            self._frames_this_turn += 1
            if self._frames_this_turn <= self.calibrate_frames and self.echo == 0.0:
                self.echo = max(self.echo, level)  # first moments of Jarvis speaking: learn the echo level
                continue
            loud = level > self.threshold()
            if not loud:
                self.echo = 0.95 * self.echo + 0.05 * level if self.echo else level
            self.window.append(loud)
            if sum(self.window) >= self.barge_frames:
                said = b"".join(self.preroll) + data[i + self.frame_bytes:cut]
                self._release()
                return said, True
        return b"", False
