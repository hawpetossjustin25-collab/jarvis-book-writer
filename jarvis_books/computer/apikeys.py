"""API keys the author saved for Jarvis's API calls, encrypted for this Windows user (DPAPI).

Jarvis never sees a key: he writes {key:NAME} in a call and the key is put in only when the request is sent.
Anything an API sends back is scrubbed of the keys before Jarvis reads it.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from jarvis_books.errors import NotFound
from jarvis_books.keystore import unseal, write_sealed

NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
SLOT = re.compile(r"\{key:([A-Za-z][A-Za-z0-9_-]{0,39})\}")
HIDDEN = "[hidden key]"


class ApiKeys:
    def __init__(self, folder: Path):
        self.path = Path(folder) / "api_keys.sealed"
        self._lock = threading.Lock()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return json.loads(unseal(self.path.read_bytes()).decode() or "{}")

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._load())

    def save(self, name: str, value: str) -> None:
        name, value = name.strip(), value.strip()
        if not NAME.match(name):
            raise ValueError("a key name starts with a letter and uses letters, numbers, - or _ (up to 40)")
        if not value:
            raise ValueError("the key is empty")
        with self._lock:
            keys = self._load()
            keys[name] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_sealed(self.path, json.dumps(keys).encode())

    def remove(self, name: str) -> bool:
        with self._lock:
            keys = self._load()
            if keys.pop(name, None) is None:
                return False
            write_sealed(self.path, json.dumps(keys).encode())
            return True

    def fill(self, value: Any) -> Any:
        """Put the real keys into {key:NAME} slots (in strings, and inside dicts and lists)."""
        keys = None

        def one(text: str) -> str:
            nonlocal keys
            if "{key:" not in text:
                return text
            keys = self._load() if keys is None else keys

            def sub(m: re.Match) -> str:
                if m.group(1) not in keys:
                    raise NotFound(f"there is no saved API key called {m.group(1)}; the author can add it in "
                                   f"Settings > API keys")
                return keys[m.group(1)]
            return SLOT.sub(sub, text)

        def walk(v: Any) -> Any:
            if isinstance(v, str):
                return one(v)
            if isinstance(v, dict):
                return {walk(k): walk(x) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x) for x in v]
            return v
        return walk(value)

    def scrub(self, text: str) -> str:
        with self._lock:
            values = sorted(self._load().values(), key=len, reverse=True)
        for v in values:
            if len(v) >= 4:
                text = text.replace(v, HIDDEN)
        return text
