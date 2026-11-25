"""Voice notes for Jarvis on Telegram: hear what the author said and answer out loud.

Hearing: Gemini listens to the note directly. When Gemini is out (or there is no Gemini), Groq's free Whisper
writes it down instead. Speaking: the Edge voice chosen in Settings reads the reply, and FFmpeg turns it into the
Opus voice note Telegram plays in the chat (an MP3 is sent if FFmpeg can't convert it). Keys are never logged.
"""
from __future__ import annotations

import base64
import logging
import re
import subprocess
import sys
from typing import Callable, Optional

import requests

from jarvis_books.audio.export import NO_WINDOW, ffmpeg_path
from jarvis_books.audio.narrator import edge_tts_bytes
from jarvis_books.llm.groq import GROQ_URL

log = logging.getLogger(__name__)

MAX_NOTE_BYTES = 20 * 1024 * 1024  # the most a Telegram bot may download
SPEAK_CHARS = 1500  # longer replies are read up to here; the full text is in the chat
WHISPER_MODEL = "whisper-large-v3-turbo"
HEAR_PROMPT = ("Write down exactly what is said in this voice note, in the language spoken. Reply with only the "
               "words that were said: no notes, labels or quotes. If no words are said, reply with nothing.")
_MORE = " The rest is in the message."


class HearingError(Exception):
    """A voice note could not be turned into words. The message is plain and never holds a key."""


def speakable(text: str, limit: int = SPEAK_CHARS) -> str:
    """The reply as it should sound: no Markdown, code or web addresses, cut at a sentence near `limit`."""
    text = re.sub(r"```.*?(```|$)", " (The code is in the message.) ", text or "", flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\((?:[^)]+)\)", r"\1", text)
    text = re.sub(r"https?://\S+", "the link in the message", text)
    text = re.sub(r"^\s{0,3}(#{1,6}|[-*+•]|\d+[.)])\s+", "", text, flags=re.M)
    text = re.sub(r"[*_`~|>]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    cut = cut[:end + 1] if end > limit // 3 else cut[:cut.rfind(" ")] if " " in cut else cut
    return cut.rstrip() + _MORE


class VoiceNotes:
    def __init__(self, gemini_hear: Optional[Callable[[str, list], str]], groq_key: Callable[[], Optional[str]],
                 edge_voice: Callable[[], str], synth: Callable[[str, str], bytes] = edge_tts_bytes,
                 ffmpeg: Callable[[], Optional[str]] = ffmpeg_path, session: Optional[requests.Session] = None,
                 groq_url: str = GROQ_URL, timeout_s: float = 90.0):
        self.gemini_hear, self.groq_key, self.edge_voice = gemini_hear, groq_key, edge_voice
        self.synth, self.ffmpeg = synth, ffmpeg
        self.session = session or requests.Session()
        self.groq_url, self.timeout_s = groq_url.rstrip("/"), timeout_s

    # ---- hearing ----------------------------------------------------------------------------------------
    def hear(self, audio: bytes, mime: str = "audio/ogg") -> str:
        """What was said in the note ('' when it holds no words). Raises HearingError when no brain could listen."""
        if not audio:
            raise HearingError("the voice note was empty")
        if len(audio) > MAX_NOTE_BYTES:
            raise HearingError("the voice note is too big")
        mime = (mime or "audio/ogg").split(";")[0].strip()
        why = []
        if self.gemini_hear is not None:
            part = {"inlineData": {"mimeType": mime, "data": base64.b64encode(audio).decode()}}
            try:
                return _clean(self.gemini_hear(HEAR_PROMPT, [part, {"text": "Write down what is said."}]))
            except Exception as exc:
                log.info("voice note: Gemini couldn't listen (%s); trying Groq", type(exc).__name__)
                why.append("Gemini is unavailable")
        try:
            key = self.groq_key()
        except Exception as exc:
            log.warning("voice note: could not read the Groq key (%s)", type(exc).__name__)
            key = None
        if not key:
            why.append("there is no Groq key")
            raise HearingError("I couldn't listen to it: " + " and ".join(why))
        return self._whisper(audio, mime, key)

    def _whisper(self, audio: bytes, mime: str, key: str) -> str:
        ext = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a",
               "audio/wav": "wav", "audio/x-wav": "wav", "audio/webm": "webm", "audio/flac": "flac"}.get(mime, "ogg")
        try:
            resp = self.session.post(self.groq_url + "/audio/transcriptions", timeout=self.timeout_s,
                                     headers={"Authorization": f"Bearer {key}"},
                                     files={"file": (f"voice.{ext}", audio, mime)},
                                     data={"model": WHISPER_MODEL, "response_format": "json", "temperature": "0"})
        except requests.RequestException as exc:
            raise HearingError(f"I couldn't reach Groq to listen to it ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise HearingError(f"Groq couldn't listen to it (error {resp.status_code})")
        try:
            return _clean(str(resp.json().get("text") or ""))
        except (ValueError, AttributeError):
            raise HearingError("Groq sent back an answer I couldn't read") from None

    # ---- speaking ---------------------------------------------------------------------------------------
    def speak(self, text: str) -> tuple[bytes, str]:
        """(audio, mime) of the reply read aloud: an Opus voice note, or an MP3 when FFmpeg can't make one."""
        words = speakable(text)
        if not words:
            raise ValueError("nothing to say")
        mp3 = self.synth(words, self.edge_voice())
        if not mp3:
            raise RuntimeError("the voice made no sound")
        ffmpeg = self.ffmpeg()
        if ffmpeg:
            try:
                proc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-vn", "-ac", "1",
                                       "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1"],
                                      input=mp3, capture_output=True, timeout=120,
                                      creationflags=NO_WINDOW if sys.platform == "win32" else 0)
                if proc.returncode == 0 and proc.stdout.startswith(b"OggS"):
                    return proc.stdout, "audio/ogg"
                log.info("voice note: FFmpeg couldn't make Opus (exit %s); sending MP3", proc.returncode)
            except (OSError, subprocess.SubprocessError) as exc:
                log.info("voice note: FFmpeg failed (%s); sending MP3", type(exc).__name__)
        return mp3, "audio/mpeg"


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text.strip('"').strip()
