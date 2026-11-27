"""Extra brains: any number of API keys from any OpenAI-style service, with an automatic key switcher.

Keys are sealed for this Windows user (DPAPI) like the Gemini key and are never shown, logged or sent to a model.
Keys from one account share that account's limits (Gemini counts per project and Groq per organization, not per
key), so keys are grouped by service + account and a group rests as a whole.

The switcher watches what is left: services that report it (Groq's x-ratelimit headers, OpenRouter's key status)
are read; for the rest Jarvis counts his own use against the daily limit the author typed in. When a group gets
low it is rested until its reset, and the next key answers. Failures rest the group for a while; a refused key is
set aside until it is tested again.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import requests

from jarvis_books.keystore import unseal, write_sealed
from jarvis_books.llm.errors import LLMError
from jarvis_books.llm.groq import GroqEngine
from jarvis_books.llm.models import LLMRequest, LLMResponse, RuntimeConfig

log = logging.getLogger(__name__)

# day: when the free daily allowance starts again ("utc" or "pacific"). models_url: the model list, if it differs.
PROVIDERS = {
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "day": "utc",
                   "prefer": (":free",), "signup": "https://openrouter.ai/keys", "input": 24000, "output": 4000},
    "groq": {"label": "Groq", "base_url": "https://api.groq.com/openai/v1", "day": "utc",
             "prefer": ("openai/gpt-oss-120b", "llama-3.3-70b-versatile"), "signup": "https://console.groq.com/keys",
             "input": 5500, "output": 2000},
    "gemini": {"label": "Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "day": "pacific",
               "prefer": ("flash",), "signup": "https://aistudio.google.com/apikey", "input": 30000, "output": 6000},
    "mistral": {"label": "Mistral", "base_url": "https://api.mistral.ai/v1", "day": "utc",
                "prefer": ("mistral-small-latest", "mistral-medium-latest"), "signup": "https://console.mistral.ai",
                "input": 24000, "output": 4000},
    "github": {"label": "GitHub Models", "base_url": "https://models.github.ai/inference", "day": "utc",
               "models_url": "https://models.github.ai/catalog/models", "prefer": ("gpt-4.1-mini", "gpt-4.1"),
               "signup": "https://github.com/settings/tokens", "input": 6000, "output": 2000},
    "cerebras": {"label": "Cerebras", "base_url": "https://api.cerebras.ai/v1", "day": "utc",
                 "prefer": ("gpt-oss-120b",), "signup": "https://cloud.cerebras.ai", "input": 24000, "output": 4000},
    "sambanova": {"label": "SambaNova", "base_url": "https://api.sambanova.ai/v1", "day": "utc", "prefer": (),
                  "signup": "https://cloud.sambanova.ai", "input": 24000, "output": 4000},
    "together": {"label": "Together", "base_url": "https://api.together.xyz/v1", "day": "utc", "prefer": (),
                 "signup": "https://api.together.ai", "input": 24000, "output": 4000},
    "custom": {"label": "Other (OpenAI-style)", "base_url": "", "day": "utc", "prefer": (), "signup": "",
               "input": 8000, "output": 2000},
}
LOW_SHARE = 0.05       # a group with 5% (or 2 requests) left rests until its reset
REST_S = {"rate": 60.0, "overload": 120.0, "network": 60.0, "too_big": 0.0, "bad_request": 0.0}


def fingerprint(key: str) -> str:
    return "..." + key[-4:] if len(key) >= 12 else "(short key)"


def next_day(provider: str, now: float) -> float:
    """When the provider's daily allowance resets: midnight UTC, or midnight Pacific for Gemini."""
    if PROVIDERS.get(provider, {}).get("day") == "pacific":
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("America/Los_Angeles")
        except Exception:  # no tz database: Pacific standard time is close enough to rest a group
            tz = dt.timezone(dt.timedelta(hours=-8))
    else:
        tz = dt.timezone.utc
    t = dt.datetime.fromtimestamp(now, tz)
    return (t.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)).timestamp()


def day_of(provider: str, now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(next_day(provider, now) - 1))


class KeyPool:
    def __init__(self, home: Path, clock: Callable[[], float] = time.time, session: Optional[requests.Session] = None):
        self.path = Path(home) / "llm_keys.sealed"
        self.state_path = Path(home) / "llm_keys_state.json"
        self.clock = clock
        self.session = session or requests.Session()
        self._lock = threading.RLock()
        self._state = self._read_state()

    # ---- storage -------------------------------------------------------------------------------------
    def _keys(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(unseal(self.path.read_bytes()).decode() or "[]")

    def _write(self, keys: list[dict]) -> None:
        write_sealed(self.path, json.dumps(keys).encode())

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"groups": {}, "refused": {}}

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(self._state, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    @staticmethod
    def group_of(entry: dict) -> str:
        return f"{entry['provider']}:{(entry.get('account') or 'main').strip().lower()}"

    def add(self, provider: str, key: str, label: str = "", account: str = "", model: str = "", base_url: str = "",
            daily_limit: Optional[int] = None) -> dict:
        key = key.strip()
        if provider not in PROVIDERS:
            raise ValueError(f"unknown service {provider!r}")
        if len(key) < 8 or any(c.isspace() for c in key):
            raise ValueError("that doesn't look like an API key")
        base = (base_url or PROVIDERS[provider]["base_url"]).rstrip("/")
        if not base.startswith("https://"):
            raise ValueError("the service address must start with https://")
        if daily_limit is not None and daily_limit < 1:
            raise ValueError("the daily limit must be at least 1")
        with self._lock:
            keys = self._keys()
            digest = hashlib.sha256(key.encode()).hexdigest()[:16]
            if any(k["digest"] == digest for k in keys):
                raise ValueError("that key is already saved")
            entry = {"id": digest[:8], "digest": digest, "provider": provider, "key": key,
                     "label": label.strip() or PROVIDERS[provider]["label"], "account": account.strip() or "main",
                     "model": model.strip(), "base_url": base, "daily_limit": daily_limit, "added": self.clock()}
            keys.append(entry)
            self._write(keys)
            self._state["refused"].pop(entry["id"], None)
            self._save_state()
        return self.public(entry)

    def remove(self, key_id: str) -> bool:
        with self._lock:
            keys = self._keys()
            kept = [k for k in keys if k["id"] != key_id]
            if len(kept) == len(keys):
                return False
            self._write(kept)
            self._state["refused"].pop(key_id, None)
            self._save_state()
            return True

    def set_model(self, key_id: str, model: str) -> None:
        with self._lock:
            keys = self._keys()
            for k in keys:
                if k["id"] == key_id:
                    k["model"] = model
            self._write(keys)

    def entries(self) -> list[dict]:
        with self._lock:
            return self._keys()

    def public(self, entry: dict) -> dict:
        g = self._group(self.group_of(entry))
        now = self.clock()
        refused = self._state["refused"].get(entry["id"])
        status = ("refused: " + refused if refused else
                  f"resting until {time.strftime('%H:%M', time.localtime(g['rest_until']))}: {g['why']}"
                  if g.get("rest_until", 0) > now else "ready")
        return {"id": entry["id"], "service": PROVIDERS[entry["provider"]]["label"], "provider": entry["provider"],
                "label": entry["label"], "account": entry["account"], "key": fingerprint(entry["key"]),
                "model": entry["model"] or "(picked on first use)", "status": status, "used_today": g.get("used", 0),
                "left": g.get("remaining"), "daily_limit": g.get("limit") or entry.get("daily_limit")}

    def listing(self) -> list[dict]:
        return [self.public(e) for e in self.entries()]

    # ---- the switcher ----------------------------------------------------------------------------------
    def _group(self, name: str) -> dict:
        provider = name.split(":", 1)[0]
        g = self._state["groups"].setdefault(name, {})
        today = day_of(provider, self.clock())
        if g.get("day") != today:  # a new day: the allowance is back
            g.update({"day": today, "used": 0, "remaining": None})
            if g.get("why", "").startswith("daily"):
                g["rest_until"] = 0
        return g

    def ready(self) -> list[dict]:
        """Keys that may answer now, in the order the author added them; one key per group per pass."""
        with self._lock:
            now = self.clock()
            out, seen = [], set()
            for e in self._keys():
                name = self.group_of(e)
                g = self._group(name)
                if e["id"] in self._state["refused"] or g.get("rest_until", 0) > now or name in seen:
                    continue
                seen.add(name)
                out.append(e)
            return out

    def _rest(self, name: str, until: float, why: str) -> None:
        g = self._group(name)
        if until > g.get("rest_until", 0):
            g["rest_until"], g["why"] = until, why
            log.info("key group %s rests until %s: %s", name, time.strftime("%H:%M", time.localtime(until)), why)

    def note_success(self, entry: dict, limits: dict) -> None:
        with self._lock:
            name, now = self.group_of(entry), self.clock()
            g = self._group(name)
            g["used"] = g.get("used", 0) + 1
            limit = limits.get("limit_requests") or entry.get("daily_limit")
            left = limits.get("remaining_requests")
            if left is None and entry.get("daily_limit"):
                left = max(0, entry["daily_limit"] - g["used"])
            g["remaining"], g["limit"] = left, limit
            if left is not None and left <= max(2, int((limit or 0) * LOW_SHARE)):
                reset = limits.get("reset_at") or (now + limits["reset_requests_s"] if "reset_requests_s" in limits
                                                  else next_day(entry["provider"], now))
                self._rest(name, reset, f"daily allowance nearly used ({left} left)")
            elif limits.get("remaining_tokens") is not None and limits.get("limit_tokens") and \
                    limits["remaining_tokens"] < limits["limit_tokens"] * LOW_SHARE:
                self._rest(name, now + limits.get("reset_tokens_s", 60.0), "minute token limit nearly used")
            self._save_state()

    def note_failure(self, entry: dict, exc: LLMError) -> None:
        with self._lock:
            name, now = self.group_of(entry), self.clock()
            if exc.kind == "unavailable" and exc.status in (401, 403):
                self._state["refused"][entry["id"]] = str(exc)[:120]
            elif exc.kind == "rate":
                msg = str(exc).lower()
                daily = exc.daily_quota or "per day" in msg or "daily" in msg or "credits" in msg
                if daily:
                    self._rest(name, next_day(entry["provider"], now), "daily allowance used up")
                else:
                    self._rest(name, now + (exc.retry_after_s or REST_S["rate"]), "minute limit reached")
            elif exc.status == 402:
                self._rest(name, next_day(entry["provider"], now), "daily allowance used up (no credits left)")
            elif REST_S.get(exc.kind, 60.0) > 0:
                self._rest(name, now + REST_S.get(exc.kind, 60.0), str(exc)[:120])
            self._save_state()

    # ---- checking a key --------------------------------------------------------------------------------
    def models(self, entry: dict) -> list[str]:
        url = PROVIDERS[entry["provider"]].get("models_url") or entry["base_url"] + "/models"
        try:
            r = self.session.get(url, headers={"Authorization": f"Bearer {entry['key']}"}, timeout=30)
        except requests.RequestException as exc:
            raise LLMError(f"{entry['label']} could not be reached ({type(exc).__name__})", kind="network") from exc
        if r.status_code in (401, 403):
            raise LLMError(f"{entry['label']} refused the key ({r.status_code})", kind="unavailable",
                           status=r.status_code)
        if r.status_code != 200:
            raise LLMError(f"{entry['label']} model list failed ({r.status_code})", kind="overload", status=r.status_code)
        body = r.json()
        rows = body.get("data", body) if isinstance(body, dict) else body
        return [str(m.get("id")) for m in rows if isinstance(m, dict) and m.get("id")]

    def pick_model(self, entry: dict, names: list[str]) -> str:
        for want in PROVIDERS[entry["provider"]]["prefer"]:
            hit = [n for n in names if (n.endswith(want) if want.startswith(":") else want in n)]
            if hit:
                return hit[0]
        chat = [n for n in names if not any(w in n.lower() for w in ("embed", "whisper", "tts", "guard", "image",
                                                                      "moderation", "rerank", "audio"))]
        if not chat:
            raise LLMError(f"{entry['label']} lists no chat model", kind="unavailable")
        return chat[0]

    def check(self, key_id: str) -> dict:
        """Ask the service if the key works, pick a model if none is set, and read what's left where it says."""
        entry = next((e for e in self.entries() if e["id"] == key_id), None)
        if entry is None:
            raise KeyError(key_id)
        names = self.models(entry)
        if not entry["model"] or entry["model"] not in names:
            entry["model"] = self.pick_model(entry, names)
            self.set_model(key_id, entry["model"])
        with self._lock:
            self._state["refused"].pop(key_id, None)
            if entry["provider"] == "openrouter":
                self._openrouter_left(entry)
            self._save_state()
        return {**self.public(entry), "models": len(names)}

    def _openrouter_left(self, entry: dict) -> None:
        try:
            r = self.session.get(entry["base_url"] + "/key", headers={"Authorization": f"Bearer {entry['key']}"},
                                 timeout=30)
            info = (r.json() or {}).get("data", {}) if r.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            return
        free = info.get("free_model_daily_requests") or {}
        g = self._group(self.group_of(entry))
        if isinstance(free.get("remaining"), (int, float)):
            g["remaining"], g["limit"] = int(free["remaining"]), free.get("limit")
            if g["remaining"] <= max(2, int((free.get("limit") or 0) * LOW_SHARE)):
                self._rest(self.group_of(entry), next_day("openrouter", self.clock()),
                           f"daily allowance nearly used ({g['remaining']} left)")
        if isinstance(info.get("limit_remaining"), (int, float)) and info["limit_remaining"] <= 0:
            self._rest(self.group_of(entry), next_day("openrouter", self.clock()), "no credits left on the key")


class PoolEngine:
    """The extra keys as one brain for Brain: tries the ready groups in turn and reports to the switcher."""

    provider = "pool"

    def __init__(self, pool: KeyPool, session: Optional[requests.Session] = None):
        self.pool = pool
        self.session = session or pool.session
        self.last_key: Optional[str] = None

    @property
    def available(self) -> bool:
        return bool(self.pool.entries())

    def _engine(self, e: dict) -> GroqEngine:
        p = PROVIDERS[e["provider"]]
        if not e["model"]:
            e["model"] = self.pool.pick_model(e, self.pool.models(e))
            self.pool.set_model(e["id"], e["model"])
        key = e["key"]
        return GroqEngine(lambda: key, e["model"], session=self.session, base_url=e["base_url"],
                          input_tokens=p["input"], output_tokens=p["output"], label=f"{p['label']} ({e['label']})")

    def get_runtime_configuration(self) -> RuntimeConfig:
        for e in self.pool.ready():
            try:
                return self._engine(e).get_runtime_configuration()
            except LLMError:
                continue
        raise LLMError("no extra key is ready", kind="unavailable")

    def generate(self, request: LLMRequest) -> LLMResponse:
        reasons, limited = [], 0
        for e in self.pool.ready():
            try:
                eng = self._engine(e)
                resp = eng.generate(request)
            except LLMError as exc:
                if exc.kind == "invalid":
                    raise
                self.pool.note_failure(e, exc)
                limited += exc.kind == "rate"
                reasons.append(f"{e['label']}: {exc}")
                continue
            self.pool.note_success(e, eng.limits)
            self.last_key = e["id"]
            return resp
        if not reasons:
            raise LLMError("every extra key is resting or refused", kind="rate" if self.pool.entries() else "unavailable")
        raise LLMError("no extra key could answer: " + " | ".join(reasons), kind="rate" if limited else "unavailable")

    def status(self) -> str:
        keys = self.pool.listing()
        if not keys:
            return "no extra keys"
        ready = sum(k["status"] == "ready" for k in keys)
        return f"{ready} of {len(keys)} keys ready"
