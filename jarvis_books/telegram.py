"""Jarvis on Telegram: the author messages his own bot and the same Jarvis answers, with the same brains, tools,
memory and yes/no gate as the app.

The bot asks Telegram for new messages over HTTPS (long polling), so nothing on the router is opened. Only the
owner is served: the first private chat that sends the pairing code shown in Settings becomes the owner, and
everyone else is turned away. Questions from the yes/no gate come with Yes/No buttons, and reminders that ring
are sent to the phone too. A voice note is heard (voice/notes.py) and answered in text and with a voice note. The bot key is sealed for this Windows user like the Gemini key and is never logged
(errors from the network are reported without the address, which holds the key).
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import requests

from jarvis_books.keystore import unseal, write_sealed
from jarvis_books.voice.notes import MAX_NOTE_BYTES, HearingError

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"
ENV_VARS = ("JARVIS_TELEGRAM_KEY", "TELEGRAM_BOT_TOKEN")
KEY_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
MESSAGE_LIMIT = 4000  # Telegram allows 4,096 characters a message
POLL_S = 50  # how long one request waits for new messages
EDIT_EVERY_S = 1.5  # Telegram limits how often one message can be edited
STALE_S = 3600.0  # messages sent while Jarvis was closed for longer than this are not acted on
PAIR_TRIES = 5  # wrong pairing codes before a new code is made
THINKING = "Jarvis is thinking..."
OWNER, OFFSET, PAIR, PAIR_MISSES = "telegram.owner", "telegram.offset", "telegram.pair", "telegram.pair_misses"
_PAIR_RE = re.compile(r"(?:/start\s+)?(\d{8})")
_BUTTON_RE = re.compile(r"gate:(\d+):(yes|no)")


class TelegramError(Exception):
    def __init__(self, status: int, description: str = "", retry_after: Optional[float] = None):
        super().__init__(f"Telegram {status}: {description}")
        self.status, self.description, self.retry_after = status, description, retry_after


# ---- the bot key ------------------------------------------------------------------------------------------
def key_path(root: Path) -> Path:
    return Path(root) / "telegram.key"


def save_bot_key(key: str, root: Path) -> Path:
    key = (key or "").strip()
    if not KEY_RE.fullmatch(key):
        raise ValueError("that doesn't look like a Telegram bot key (it looks like 123456789:ABC...)")
    return write_sealed(key_path(root), key.encode())


def load_bot_key(root: Path) -> Optional[str]:
    for var in ENV_VARS:
        if os.environ.get(var):
            return os.environ[var].strip()
    path = key_path(root)
    if not path.exists():
        return None
    return unseal(path.read_bytes()).decode().strip() or None


def forget_bot_key(root: Path) -> None:
    key_path(root).unlink(missing_ok=True)


# ---- Telegram's Bot API -----------------------------------------------------------------------------------
def buttons_for(request_id: int) -> dict:
    return {"inline_keyboard": [[{"text": "Yes", "callback_data": f"gate:{request_id}:yes"},
                                 {"text": "No", "callback_data": f"gate:{request_id}:no"}]]}


class TelegramAPI:
    """The few Bot API calls Jarvis uses. Every failure becomes a TelegramError that never contains the key."""

    def __init__(self, token: str, session: Optional[requests.Session] = None):
        self._token = token
        self.session = session or requests.Session()

    def call(self, method: str, payload: Optional[dict] = None, timeout: float = 20.0, files: Optional[dict] = None):
        url = f"{API_URL}/bot{self._token}/{method}"
        try:
            if files:
                resp = self.session.post(url, data=payload or {}, files=files, timeout=timeout)
            else:
                resp = self.session.post(url, json=payload or {}, timeout=timeout)
        except requests.RequestException as exc:
            raise TelegramError(0, f"no connection ({type(exc).__name__})") from None
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(resp.status_code, "Telegram sent an answer that isn't JSON") from None
        if not isinstance(data, dict) or not data.get("ok"):
            data = data if isinstance(data, dict) else {}
            retry = (data.get("parameters") or {}).get("retry_after")
            raise TelegramError(int(data.get("error_code") or resp.status_code),
                                str(data.get("description") or "")[:300], float(retry) if retry else None)
        return data.get("result")

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(self, offset: Optional[int], timeout: int = POLL_S) -> list[dict]:
        payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=timeout + 15) or []

    def send_message(self, chat_id: int, text: str, buttons: Optional[dict] = None) -> dict:
        payload = {"chat_id": chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = buttons
        return self.call("sendMessage", payload)

    def edit_message(self, chat_id: int, message_id: int, text: str, buttons: Optional[dict] = None) -> dict:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if buttons:
            payload["reply_markup"] = buttons
        return self.call("editMessageText", payload)

    def clear_buttons(self, chat_id: int, message_id: int) -> None:
        self.call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                             "reply_markup": {"inline_keyboard": []}})

    def answer_button(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def typing(self, chat_id: int, action: str = "typing") -> None:
        self.call("sendChatAction", {"chat_id": chat_id, "action": action})

    def download(self, file_id: str, limit: int = MAX_NOTE_BYTES) -> bytes:
        """The bytes of a file someone sent the bot (Telegram lets bots fetch up to 20 MB)."""
        path = (self.call("getFile", {"file_id": file_id}) or {}).get("file_path")
        if not path:
            raise TelegramError(0, "Telegram didn't say where the file is")
        try:
            resp = self.session.get(f"{API_URL}/file/bot{self._token}/{path}", timeout=60)
        except requests.RequestException as exc:
            raise TelegramError(0, f"no connection ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise TelegramError(resp.status_code, "the file couldn't be downloaded")
        if len(resp.content) > limit:
            raise TelegramError(0, "the file is too big")
        return resp.content

    def send_voice(self, chat_id: int, audio: bytes, mime: str = "audio/ogg") -> dict:
        name = "jarvis.ogg" if mime == "audio/ogg" else "jarvis.mp3"
        return self.call("sendVoice", {"chat_id": str(chat_id)}, timeout=120, files={"voice": (name, audio, mime)})


def split_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Pieces of at most `limit` characters, cut at a paragraph, line or space where one is close."""
    text = (text or "").strip()
    pieces = []
    while len(text) > limit:
        cut = max(text.rfind(sep, 0, limit) for sep in ("\n\n", "\n", " "))
        cut = cut if cut > limit // 2 else limit
        pieces.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text or not pieces:
        pieces.append(text)
    return pieces


def _preview(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= MESSAGE_LIMIT else text[:MESSAGE_LIMIT - 3].rstrip() + "..."


# ---- the bridge -------------------------------------------------------------------------------------------
class TelegramBridge:
    """Reads the owner's Telegram messages on its own thread and answers each through `assistant.send`."""

    def __init__(self, library, assistant, gate, load_key: Callable[[], Optional[str]], planner=None,
                 api_factory: Callable[[str], TelegramAPI] = TelegramAPI, clock: Callable[[], float] = time.time,
                 edit_every_s: float = EDIT_EVERY_S, poll_s: int = POLL_S, voice=None):
        self.library, self.assistant, self.gate, self.voice = library, assistant, gate, voice
        self.load_key, self.api_factory, self.clock = load_key, api_factory, clock
        self.edit_every_s, self.poll_s = edit_every_s, poll_s
        self.bot_name: Optional[str] = None
        self.error: Optional[str] = None
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._turn_thread: Optional[int] = None
        self._asked = None
        gate.subscribe(self._on_gate)
        if planner is not None:
            planner.subscribe(self._on_planner)

    # ---- running ----------------------------------------------------------------------------------------
    def start(self) -> bool:
        """Start reading messages. False when no bot key is saved."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
                return True
            key = self.load_key()
            if not key:
                return False
            self.error, self._stop = None, threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop, key), name="jarvis-telegram",
                                            daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            if self._stop is not None:
                self._stop.set()
            self.bot_name = None

    def restart(self) -> bool:
        self.stop()
        return self.start()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._stop is not None and not self._stop.is_set())

    def owner(self) -> Optional[int]:
        raw = self.library.get_meta(OWNER, "")
        return int(raw) if raw and raw.lstrip("-").isdigit() else None

    def pair_code(self) -> str:
        code = self.library.get_meta(PAIR, "")
        if not code:
            code = f"{secrets.randbelow(10 ** 8):08d}"
            self.library.set_meta(PAIR, code)
            self.library.set_meta(PAIR_MISSES, "0")
        return code

    def forget(self) -> None:
        """A new bot (or no bot): drop the owner, the read position and the pairing code."""
        for key in (OWNER, OFFSET, PAIR, PAIR_MISSES):
            self.library.set_meta(key, "")

    def status(self) -> dict:
        owner = self.owner()
        out = {"key_saved": bool(self.load_key()), "running": self.running, "bot": self.bot_name,
               "paired": owner is not None, "error": self.error}
        if owner is None and out["key_saved"]:
            out["pair_code"] = self.pair_code()
        return out

    def _offset(self) -> Optional[int]:
        raw = self.library.get_meta(OFFSET, "")
        return int(raw) if raw and raw.isdigit() else None

    def _run(self, stop: threading.Event, key: str) -> None:
        api = self.api_factory(key)
        backoff = 1.0
        while not stop.is_set():
            try:
                if self.bot_name is None:
                    self.bot_name = (api.get_me() or {}).get("username")
                updates = api.get_updates(self._offset(), self.poll_s)
                self.error, backoff = None, 1.0
            except TelegramError as exc:
                if exc.status == 401 or exc.status == 404:
                    self.error = "Telegram says the bot key is wrong. Save the key from @BotFather again."
                    log.warning("telegram: bot key refused (%s)", exc.status)
                    return
                if exc.status == 409:
                    self.error = "Another program is reading this bot's messages."
                else:
                    self.error = f"Can't reach Telegram right now ({exc.description or exc.status})."
                log.warning("telegram: %s", self.error)
                stop.wait(exc.retry_after or backoff)
                backoff = min(60.0, backoff * 2)
                continue
            for update in updates:
                if stop.is_set():
                    return
                try:
                    self.handle(api, update)
                except Exception:
                    log.exception("telegram message failed")
                self.library.set_meta(OFFSET, str(int(update.get("update_id", 0)) + 1))

    # ---- one update -------------------------------------------------------------------------------------
    def handle(self, api: TelegramAPI, update: dict) -> None:
        if "callback_query" in update:
            self._on_button(api, update["callback_query"])
            return
        msg = update.get("message")
        if not isinstance(msg, dict):
            return
        chat, sender = msg.get("chat") or {}, msg.get("from") or {}
        if chat.get("type") != "private":
            return
        chat_id, text = chat.get("id"), (msg.get("text") or "").strip()
        owner = self.owner()
        if owner is None:
            self._pair(api, chat_id, sender.get("id"), text)
            return
        if chat_id != owner or sender.get("id") != owner:
            api.send_message(chat_id, "This Jarvis is private.")
            return
        note = msg.get("voice") or msg.get("audio")
        if not text and not isinstance(note, dict):
            api.send_message(chat_id, "I can read text messages and listen to voice notes here.")
            return
        if text == "/start":
            api.send_message(chat_id, "Jarvis here. Send me anything, typed or as a voice note.")
            return
        if self.clock() - float(msg.get("date") or self.clock()) > STALE_S:
            short = (text if len(text) <= 80 else text[:77] + "...") if text else ""
            what = f"\"{short}\"" if text else "a voice note"
            api.send_message(chat_id, f"I was closed when you sent {what}. Send it again if you still want it.")
            return
        if text:
            self.turn(api, chat_id, text)
        else:
            self.voice_turn(api, chat_id, note)

    def voice_turn(self, api: TelegramAPI, chat_id: int, note: dict) -> Optional[str]:
        """Hear a voice note, answer it like a typed message, then say the answer back as a voice note."""
        if self.voice is None:
            api.send_message(chat_id, "I can't listen to voice notes right now. Please type it.")
            return None
        if int(note.get("file_size") or 0) > MAX_NOTE_BYTES:
            api.send_message(chat_id, "That voice note is too big for me (Telegram lets me fetch up to 20 MB).")
            return None
        try:
            api.typing(chat_id)
        except TelegramError:
            pass
        try:
            heard = self.voice.hear(api.download(str(note.get("file_id") or "")), note.get("mime_type") or "audio/ogg")
        except (TelegramError, HearingError) as exc:
            why = exc.description if isinstance(exc, TelegramError) else str(exc)
            log.info("telegram: voice note not heard (%s)", type(exc).__name__)
            api.send_message(chat_id, f"I couldn't hear that voice note ({why}). Try again, or type it.")
            return None
        if not heard:
            api.send_message(chat_id, "I didn't hear any words in that voice note.")
            return None
        api.send_message(chat_id, f"You said: {heard}")
        reply = self.turn(api, chat_id, heard)
        self.say(api, chat_id, reply)
        return reply

    def say(self, api: TelegramAPI, chat_id: int, reply: str) -> None:
        if not reply or self.voice is None:
            return
        try:
            api.typing(chat_id, "record_voice")
        except TelegramError:
            pass
        try:
            audio, mime = self.voice.speak(reply)
            api.send_voice(chat_id, audio, mime)
        except Exception as exc:  # the text answer is already in the chat
            log.warning("telegram: spoken reply not sent (%s)", type(exc).__name__)

    def _pair(self, api: TelegramAPI, chat_id: int, sender_id: Optional[int], text: str) -> None:
        match = _PAIR_RE.fullmatch(text)
        if match and sender_id == chat_id and secrets.compare_digest(match.group(1), self.pair_code()):
            self.library.set_meta(OWNER, str(chat_id))
            self.library.set_meta(PAIR, "")
            log.info("telegram: paired with the owner")
            api.send_message(chat_id, "Paired. I'm Jarvis, and only you can talk to me here.")
            return
        if match:
            misses = int(self.library.get_meta(PAIR_MISSES, "0") or 0) + 1
            self.library.set_meta(PAIR_MISSES, str(misses))
            if misses >= PAIR_TRIES:
                self.library.set_meta(PAIR, "")  # too many wrong codes: the next one shown in Settings is new
                log.warning("telegram: %s wrong pairing codes, made a new code", misses)
        api.send_message(chat_id, "This Jarvis is private. To pair, send the code shown in Jarvis's Settings.")

    def turn(self, api: TelegramAPI, chat_id: int, text: str, decided: Optional[dict] = None) -> str:
        """Answer one message, showing the reply as it's written. Returns the reply."""
        try:
            api.typing(chat_id)
        except TelegramError:
            pass
        placeholder = (api.send_message(chat_id, THINKING) or {}).get("message_id")
        shown = {"at": self.clock(), "text": THINKING}

        def on_text(partial: str) -> None:
            now, preview = self.clock(), _preview(partial)
            if placeholder is None or not preview or preview == shown["text"] or now - shown["at"] < self.edit_every_s:
                return
            try:
                api.edit_message(chat_id, placeholder, preview)
                shown.update(at=now, text=preview)
            except TelegramError as exc:
                log.debug("telegram: partial edit skipped (%s)", exc.status)

        self._asked, self._turn_thread = None, threading.get_ident()
        try:
            reply = self.assistant.send(text, on_text=on_text, decided=decided)
        except Exception:
            log.exception("telegram turn failed")
            reply = "Something went wrong on my side. Try again in a moment."
        finally:
            self._turn_thread = None
        asked = self._asked
        buttons = buttons_for(asked.id) if asked is not None and self.gate.pending is asked else None
        self._deliver(api, chat_id, placeholder, reply or "(done)", buttons, shown["text"])
        return reply

    def _deliver(self, api, chat_id, placeholder, reply, buttons, shown) -> None:
        pieces = split_text(reply)
        for i, piece in enumerate(pieces):
            last = i == len(pieces) - 1
            marks = buttons if last else None
            if i == 0 and placeholder is not None:
                try:
                    api.edit_message(chat_id, placeholder, piece, marks)
                    continue
                except TelegramError as exc:
                    if "not modified" in exc.description and not marks and piece == shown:
                        continue
                    log.info("telegram: reply edit failed (%s); sending it as a new message", exc.status)
            api.send_message(chat_id, piece, marks)

    def _on_button(self, api: TelegramAPI, query: dict) -> None:
        sender = (query.get("from") or {}).get("id")
        msg = query.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        owner = self.owner()
        if owner is None or sender != owner or chat_id not in (None, owner):
            api.answer_button(query.get("id", ""), "This Jarvis is private.")
            return
        match = _BUTTON_RE.fullmatch(query.get("data") or "")
        if msg.get("message_id") is not None:
            try:
                api.clear_buttons(owner, msg["message_id"])
            except TelegramError:
                pass
        if not match:
            api.answer_button(query.get("id", ""))
            return
        yes = match.group(2) == "yes"
        outcome = self.gate.answer(yes, int(match.group(1)), via="telegram")
        if outcome is None:
            api.answer_button(query.get("id", ""), "That question is already closed.")
            return
        api.answer_button(query.get("id", ""), "Yes" if yes else "No")
        self.turn(api, owner, "yes" if yes else "no", decided=outcome)

    # ---- events from the rest of Jarvis -----------------------------------------------------------------
    def _on_gate(self, event: str, info: dict) -> None:
        if event == "asking" and self._turn_thread == threading.get_ident():
            self._asked = info.get("request")

    def _on_planner(self, event: str, payload) -> None:
        if event != "ringing" or payload is None or not self.running:
            return
        owner, key = self.owner(), self.load_key()
        if owner is None or not key:
            return
        try:
            self.api_factory(key).send_message(owner, f"Reminder: {payload.text}")
        except TelegramError as exc:
            log.warning("telegram: reminder not sent (%s)", exc.status)


def telegram_line(status: dict) -> str:
    """One plain line for Settings about the Telegram bot."""
    if not status.get("key_saved"):
        return "Not set up. In Telegram, message @BotFather, send /newbot, and paste the key it gives you here."
    if status.get("error"):
        return status["error"]
    bot = f"@{status['bot']}" if status.get("bot") else "your bot"
    if not status.get("running"):
        return f"Telegram is turned off for {bot}."
    if not status.get("paired"):
        return f"Connected as {bot}. To pair your phone, send {bot} this code: {status.get('pair_code', '')}"
    return f"Connected as {bot} and paired with your Telegram. Only you can talk to Jarvis there."
