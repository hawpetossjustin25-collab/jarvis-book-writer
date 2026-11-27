"""One Telegram bot, three Jarvises: Book Writer Jarvis (this app), Game Creator Jarvis (Worldcraft) and In-Game
Jarvis (the games Worldcraft makes).

The switcher picks who gets each message from the paired owner, in this order:
1. a name at the start: "book ...", "creator ..." (or "wc", "worldcraft"), "game ..." (or "in-game");
2. a reply to one of a Jarvis's messages goes back to that Jarvis;
3. "switch to game Jarvis" makes later messages stay with him until the next switch;
4. otherwise Book Writer Jarvis, as before.
Game Creator Jarvis's approval buttons ("wc:approve:...") always go to him.

Worldcraft and the games read their inbox and send their messages over the local NEXUS API, so no game holds the
bot key. The inbox is on disk, so messages wait while a game or Worldcraft is closed; old ones go after a week.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

BOOK, CREATOR, GAME = "book", "creator", "game"
NAMES = {BOOK: "Book Writer Jarvis", CREATOR: "Game Creator Jarvis", GAME: "In-Game Jarvis"}
CHANNELS = {CREATOR: "worldcraft", GAME: "game"}  # inbox name for each Jarvis that reads one
KEEP_S = 7 * 24 * 3600
LISTENING_S = 90.0
MAX_TEXT = 3500

_WHO = r"(book(?:\s*writer)?|(?:game\s*)?creator|worldcraft|wc|in[\s-]*game|game)"
_NAME = re.compile(rf"{_WHO}(?:\s+jarvis)?\b[:,]?\s*(.*)", re.I | re.S)
_SWITCH = re.compile(rf"(?:switch|talk|go|change)(?:\s+back)?\s+to\s+(?:the\s+)?{_WHO}(?:\s+jarvis)?[.!]?", re.I)
_ASK = re.compile(r"(?:which|what|who)(?:\s+\w+){0,3}\s+jarvis(?:\s+\w+){0,4}\??", re.I)


def target_of(word: str) -> str:
    w = re.sub(r"[\s-]+", "", word.lower())
    if w.startswith("book"):
        return BOOK
    if w in ("creator", "gamecreator", "worldcraft", "wc"):
        return CREATOR
    return GAME


class TelegramRelay:
    def __init__(self, home: Path, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.bridge = None  # TelegramBridge, set once Jarvis has made it
        self._lock = threading.Lock()
        self._polled: dict[str, float] = {}
        self.db = sqlite3.connect(Path(home) / "telegram_relay.db", check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS inbox (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
                user_id INTEGER, chat_id INTEGER, text TEXT, callback TEXT, date REAL, created REAL);
            CREATE TABLE IF NOT EXISTS sent (message_id INTEGER PRIMARY KEY, target TEXT NOT NULL, created REAL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        self.db.commit()

    # ---- the switcher ------------------------------------------------------------------------------------
    def current(self) -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key='target'").fetchone()
        return row[0] if row and row[0] in NAMES else BOOK

    def switch(self, target: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('target', ?)", (target,))
            self.db.commit()

    def route(self, text: str, reply_to: Optional[int] = None) -> tuple[str, str]:
        """(who, text for him). who is a Jarvis, "switch" (text = the new Jarvis) or "ask" (which one is on)."""
        text = text.strip()
        if m := _SWITCH.fullmatch(text):
            target = target_of(m.group(1))
            self.switch(target)
            return "switch", target
        if _ASK.fullmatch(text):
            return "ask", self.current()
        m = _NAME.fullmatch(text)
        if m and m.group(2).strip():
            return target_of(m.group(1)), m.group(2).strip()
        if reply_to is not None:
            row = self.db.execute("SELECT target FROM sent WHERE message_id=?", (reply_to,)).fetchone()
            return (row[0] if row else BOOK), text  # every bot message not sent here came from Book Writer Jarvis
        return self.current(), text

    # ---- inboxes -----------------------------------------------------------------------------------------
    def put(self, channel: str, user_id: int, chat_id: int, text: str = "", callback: str = "",
            date: Optional[float] = None) -> int:
        if channel not in CHANNELS.values():
            raise ValueError(f"unknown channel {channel!r}")
        now = self.clock()
        with self._lock:
            self.db.execute("DELETE FROM inbox WHERE created < ?", (now - KEEP_S,))
            self.db.execute("DELETE FROM sent WHERE created < ?", (now - KEEP_S,))
            cur = self.db.execute("INSERT INTO inbox (channel, user_id, chat_id, text, callback, date, created) "
                                  "VALUES (?,?,?,?,?,?,?)", (channel, user_id, chat_id, text[:MAX_TEXT], callback,
                                                             date or now, now))
            self.db.commit()
            return int(cur.lastrowid)

    def take(self, channel: str, after: int = 0, limit: int = 50) -> list[dict]:
        """Messages after the reader's last seen id. Nothing is deleted, so a reader that crashed reads them again."""
        self._polled[channel] = self.clock()
        with self._lock:
            rows = self.db.execute("SELECT id, user_id, chat_id, text, callback, date FROM inbox WHERE channel=? AND id>?"
                                   " ORDER BY id LIMIT ?", (channel, after, limit)).fetchall()
        return [{"update_id": r[0], "user_id": r[1], "chat_id": r[2], "text": r[3] or "", "callback": r[4] or "",
                 "date": r[5]} for r in rows]

    def listening(self, target: str) -> bool:
        """Whether that Jarvis read his inbox in the last minute and a half (Book Writer Jarvis always is)."""
        return target == BOOK or self.clock() - self._polled.get(CHANNELS[target], -1e18) < LISTENING_S

    def in_use(self) -> bool:
        """True once another Jarvis has talked here, so Book Writer Jarvis labels his replies too."""
        return self.current() != BOOK or self.db.execute("SELECT 1 FROM sent LIMIT 1").fetchone() is not None

    # ---- sending -----------------------------------------------------------------------------------------
    def connected(self) -> bool:
        b = self.bridge
        return b is not None and b.running and b.owner() is not None

    def send(self, text: str, target: str, source: str = "", buttons: Optional[list] = None) -> bool:
        """Send the owner a message from Game Creator Jarvis or In-Game Jarvis, labelled with who sent it.
        False when Telegram is off or unpaired."""
        if target not in CHANNELS or not self.connected():
            return False
        label = NAMES[target] + (f" · {source}" if source else "")
        markup = {"inline_keyboard": buttons} if buttons else None
        message_id = self.bridge.send_owner(f"[{label}] {text}"[:MAX_TEXT], markup)
        if message_id is None:
            return False
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO sent VALUES (?,?,?)", (message_id, target, self.clock()))
            self.db.commit()
        return True
