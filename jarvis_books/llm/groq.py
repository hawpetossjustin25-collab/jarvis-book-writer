"""Groq's free cloud models behind the same request API as Gemini: a free backup brain and researcher.

Groq's free plan allows about 1,000 requests a day and 8,000 tokens a minute per model, so requests are kept
small (the short tool list, a trimmed prompt) and a refused request makes the brain move on, not stop.
The key is stored encrypted for this Windows user, like the Gemini key, and never logged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import requests

from jarvis_books.keystore import unseal, write_sealed
from jarvis_books.llm.errors import InvalidStructuredOutput, LLMError
from jarvis_books.llm.history import fit as fit_turns, window
from jarvis_books.llm.local import _strip_thinking, to_json_schema, to_tools
from jarvis_books.llm.models import LLMRequest, LLMResponse, ModelBudget, RuntimeConfig
from jarvis_books.llm.policy import TEXT_ROLES
from jarvis_books.llm.prompt import _cut, fit_sections
from jarvis_books.llm.schema import parse_json, validate
from jarvis_books.llm.sizing import estimate_tokens

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1"
CHAT_MODEL = "openai/gpt-oss-120b"
RESEARCH_MODEL = "openai/gpt-oss-20b"  # its own daily allowance, so research never eats into Jarvis's
LABEL = "Groq"
ENV_VARS = ("JARVIS_GROQ_KEY", "GROQ_API_KEY")
INPUT_TOKENS = 5500  # leaves room for the answer inside the free plan's 8,000 tokens a minute
OUTPUT_TOKENS = 2000
MAX_TOOLS = 128
TOOL_SHARE = 0.35


def _duration(text: str) -> Optional[float]:
    """Groq's reset times look like 2m59.56s, 7.66s, 1h2m or 450ms."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    scale = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    return sum(float(n) * scale[u] for n, u in parts)


def _int(value) -> Optional[int]:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def read_limits(headers) -> dict:
    """What an OpenAI-style service says is left. Groq always sends x-ratelimit-* (requests per day, tokens per
    minute); OpenRouter sends X-RateLimit-* with a reset time in epoch milliseconds. Missing values stay missing."""
    h = {str(k).lower(): v for k, v in dict(headers).items()}
    out: dict = {}
    for name, key in (("remaining_requests", "x-ratelimit-remaining-requests"),
                      ("limit_requests", "x-ratelimit-limit-requests"),
                      ("remaining_tokens", "x-ratelimit-remaining-tokens"), ("limit_tokens", "x-ratelimit-limit-tokens")):
        if _int(h.get(key)) is not None:
            out[name] = _int(h.get(key))
    for name, key in (("reset_requests_s", "x-ratelimit-reset-requests"), ("reset_tokens_s", "x-ratelimit-reset-tokens")):
        d = _duration(str(h.get(key, "")))
        if d is not None:
            out[name] = d
    if "remaining_requests" not in out and _int(h.get("x-ratelimit-remaining")) is not None:
        out["remaining_requests"] = _int(h.get("x-ratelimit-remaining"))
        if _int(h.get("x-ratelimit-limit")) is not None:
            out["limit_requests"] = _int(h.get("x-ratelimit-limit"))
        reset = _int(h.get("x-ratelimit-reset"))
        if reset and reset > 10 ** 11:
            out["reset_at"] = reset / 1000.0
    return out


def key_path(root: Path) -> Path:
    return Path(root) / "groq.key"


def save_groq_key(key: str, root: Path) -> Path:
    key = key.strip()
    if not key:
        raise ValueError("empty key")
    return write_sealed(key_path(root), key.encode())


def load_groq_key(root: Path) -> Optional[str]:
    for var in ENV_VARS:
        if os.environ.get(var):
            return os.environ[var].strip()
    path = key_path(root)
    if not path.exists():
        return None
    return unseal(path.read_bytes()).decode().strip() or None


def to_openai_messages(system: str, contents: list[dict]) -> list[dict]:
    """Gemini 'contents' turns to OpenAI-style chat messages. Tool calls get ids and each result answers its
    call; a result whose call was trimmed out of the history becomes plain text so the request stays valid."""
    messages = [{"role": "system", "content": system}] if system else []
    open_calls: list[tuple[str, str]] = []  # (tool name, call id) still waiting for their result
    n = 0
    for turn in contents:
        parts = turn.get("parts") or []
        text = "".join(p.get("text", "") for p in parts if "text" in p)
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        replies = [p["functionResponse"] for p in parts if "functionResponse" in p]
        if turn.get("role") == "model":
            open_calls = []
            msg: dict = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = []
                for c in calls:
                    n += 1
                    cid = f"call_{n}"
                    open_calls.append((c.get("name", ""), cid))
                    msg["tool_calls"].append({"id": cid, "type": "function", "function": {
                        "name": c.get("name", ""), "arguments": json.dumps(c.get("args") or {}, ensure_ascii=False)}})
            messages.append(msg)
            continue
        loose = []
        for r in replies:
            body = json.dumps(r.get("response", {}), ensure_ascii=False)
            match = next((i for i, (name, _) in enumerate(open_calls) if name == r.get("name", "")), None)
            if match is None:
                loose.append(f"Result of {r.get('name', 'a tool')}: {body}")
                continue
            _, cid = open_calls.pop(match)
            messages.append({"role": "tool", "tool_call_id": cid, "content": body})
        for name, cid in open_calls:  # every call needs an answer before the next user message
            messages.append({"role": "tool", "tool_call_id": cid, "content": json.dumps({"error": "no result"})})
        open_calls = []
        if loose or text:
            messages.append({"role": "user", "content": "\n\n".join(loose + ([text] if text else []))})
    return messages


class GroqEngine:
    provider = "groq"

    def __init__(self, key: Callable[[], Optional[str]], model: str = CHAT_MODEL,
                 session: Optional[requests.Session] = None, timeout_s: float = 120.0,
                 input_tokens: int = INPUT_TOKENS, output_tokens: int = OUTPUT_TOKENS, base_url: str = GROQ_URL,
                 label: str = LABEL):
        self.key = key
        self.label = label
        self.limits: dict = {}  # what the service said is left after the last answer (see read_limits)
        self.model = model
        self.session = session or requests.Session()
        self.timeout_s = timeout_s
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.base_url = base_url.rstrip("/")
        self.problem = ""
        self._lock = threading.Lock()

    def _key(self) -> Optional[str]:
        try:
            return self.key()
        except Exception as exc:  # an unreadable key file is "no key", not a crash
            log.warning("could not read the Groq key: %s", type(exc).__name__)
            return None

    @property
    def available(self) -> bool:
        return bool(self._key())

    def initialize(self) -> bool:
        ok = self.available
        if not ok:
            self.problem = "no Groq key is saved (Settings)"
        return ok

    def status(self) -> dict:
        return {"model": self.model, "ready": self.available, "problem": "" if self.available else
                "no Groq key is saved", "free_plan": "about 1,000 requests a day"}

    def get_runtime_configuration(self) -> RuntimeConfig:
        if not self.available:
            raise LLMError("no Groq key is saved", kind="unavailable")
        out = self.output_tokens
        budget = ModelBudget(self.model, self.input_tokens, out, True, 30, None, None)
        return RuntimeConfig(version=0, routes={r: (self.model,) for r in TEXT_ROLES}, budgets={self.model: budget},
                             memory_budget_tokens=int(self.input_tokens * 0.3), history_turns=8,
                             words_per_call=max(200, int(out / 1.35 * 0.6)), scene_words=max(200, int(out / 1.35 * 0.5)),
                             concurrency=1, tts_workers=1, embed_batch=1, max_retries=1, adaptive=True,
                             reason=f"{self.label} free plan")

    # ---- requests ---------------------------------------------------------------------------
    def build(self, request: LLMRequest) -> dict:
        budget_in = self.input_tokens
        declarations = request.local_tools if request.local_tools is not None else request.tools
        tools = to_tools(declarations or [])[:MAX_TOOLS]
        for t in tools:
            t["function"]["parameters"].setdefault("properties", {})
        while tools and estimate_tokens(json.dumps(tools), 3.0) > budget_in * TOOL_SHARE:
            tools.pop()
        if tools:
            budget_in = max(512, budget_in - estimate_tokens(json.dumps(tools), 3.0))
        system = request.system
        if estimate_tokens(system) > budget_in * 0.5:
            system = _cut(system, int(budget_in * 0.5), 4.0, "start")
        answering = not (request.instruction or request.sections)
        history = window(request.history, 8, answering)
        room = int(budget_in * 0.8) - estimate_tokens(system)
        history = fit_turns(history, room, lambda t: estimate_tokens(json.dumps(t.get("parts", ""))), answering)
        used = estimate_tokens(system) + sum(estimate_tokens(json.dumps(t.get("parts", ""))) for t in history)
        contents = list(history)
        if request.instruction or request.sections:
            instruction = request.instruction
            if request.json_schema:
                instruction += ("\n\nAnswer with JSON only, matching this JSON schema: "
                                + json.dumps(to_json_schema(request.json_schema)))
            fit = fit_sections(instruction, request.sections, max(512, budget_in - used), 4.0)
            contents.append({"role": "user", "parts": [{"text": fit.text}]})
        if not contents:
            raise LLMError("empty request", kind="invalid")
        payload: dict = {"model": self.model, "messages": to_openai_messages(system, contents),
                         "max_completion_tokens": min(request.max_output_tokens or self.output_tokens,
                                                      self.output_tokens)}
        if request.temperature is not None:
            payload["temperature"] = float(request.temperature)
        if tools:
            payload["tools"] = tools
        elif request.json_schema:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post(self, payload: dict, key: str) -> dict:
        try:
            resp = self.session.post(self.base_url + "/chat/completions", json=payload, timeout=self.timeout_s,
                                     headers={"Authorization": f"Bearer {key}"})
        except requests.RequestException as exc:
            raise LLMError(f"{self.label} could not be reached ({type(exc).__name__})", kind="network") from exc
        self.limits = read_limits(getattr(resp, "headers", None) or {})
        if resp.status_code == 200:
            return resp.json()
        try:
            detail = str((resp.json().get("error") or {}).get("message", ""))[:200]
        except ValueError:
            detail = resp.text[:200]
        status = resp.status_code
        if status in (401, 403):
            raise LLMError(f"{self.label} refused the key ({status})", kind="unavailable", status=status)
        if status == 429:
            retry = resp.headers.get("retry-after")
            raise LLMError(f"{self.label}'s free limit is used up for now: {detail}", kind="rate", status=status,
                           retry_after_s=float(retry) if retry and retry.replace(".", "", 1).isdigit() else None)
        if status == 413:
            raise LLMError(f"the request is too big for {self.label}'s free plan", kind="too_big", status=status)
        if status >= 500:
            raise LLMError(f"{self.label} is having trouble ({status})", kind="overload", status=status)
        raise LLMError(f"{self.label} could not use the request ({status}): {detail}", kind="bad_request", status=status)

    def generate(self, request: LLMRequest) -> LLMResponse:
        key = self._key()
        if not key:
            raise LLMError("no Groq key is saved", kind="unavailable")
        with self._lock:
            payload = self.build(request)
            for attempt in (1, 2):
                start = time.perf_counter()
                try:
                    body = self._post(payload, key)
                except LLMError as exc:
                    if attempt == 1 and exc.kind == "bad_request" and "tool call validation" in str(exc).lower():
                        # the model called a tool that isn't in the list; Groq refuses that, and a retry
                        # with a reminder almost always picks a real one
                        payload["messages"].append({"role": "user", "content": (
                            "Only call tools from the tools list you were given, by their exact names.")})
                        continue
                    raise
                latency = time.perf_counter() - start
                choice = (body.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                text = _strip_thinking(msg.get("content") or "")
                calls = []
                for c in msg.get("tool_calls") or []:
                    fn = c.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    if fn.get("name"):
                        args = {k: v for k, v in args.items() if k} if isinstance(args, dict) else {}
                        calls.append({"name": fn["name"], "args": args})
                data = None
                if request.json_schema and not calls:
                    try:
                        data = parse_json(text)
                        errors = validate(data, request.json_schema)
                    except ValueError as exc:
                        errors = [str(exc)]
                    if errors:
                        if attempt == 1:
                            payload["messages"].append({"role": "assistant", "content": text})
                            payload["messages"].append({"role": "user", "content": (
                                "That did not match the JSON format (" + "; ".join(errors)[:300] +
                                "). Return only valid JSON matching the schema.")})
                            continue
                        raise InvalidStructuredOutput("; ".join(errors)[:300])
                usage = body.get("usage") or {}
                parts = ([{"text": text}] if text else []) + [{"functionCall": c} for c in calls]
                self.problem = ""
                return LLMResponse(text=text, model=f"{self.label} ({self.model})", data=data, function_calls=calls,
                                   content={"role": "model", "parts": parts}, finish_reason=choice.get("finish_reason"),
                                   prompt_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                                   latency_s=latency, attempts=attempt)
        raise LLMError("generation failed", kind="unknown")


class GroqSearch:
    """Web search through Groq's own search tool (browser_search on the research model).

    Free search engines often treat a home computer that searches all day as a robot: DuckDuckGo asks for a
    robot check, and Bing answers with results for the first word only. Groq searches from its own servers, so
    this is the researcher's way to the web when those engines are blocked. It uses the research model's daily
    allowance; with no key, or while Groq's limit is used up, it finds nothing and asks Groq nothing."""

    def __init__(self, key: Callable[[], Optional[str]], model: str = RESEARCH_MODEL,
                 session: Optional[requests.Session] = None, timeout_s: float = 90.0, base_url: str = GROQ_URL,
                 clock: Callable[[], float] = time.time):
        self.key = key
        self.model = model
        self.session = session or requests.Session()
        self.timeout_s = timeout_s
        self.base_url = base_url.rstrip("/")
        self.clock = clock
        self.rest_until = 0.0

    def search(self, query: str, limit: int = 8) -> list[dict]:
        try:
            key = self.key()
        except Exception as exc:
            log.warning("could not read the Groq key: %s", type(exc).__name__)
            return []
        if not key or not query.strip() or self.clock() < self.rest_until:
            return []
        payload = {"model": self.model, "tools": [{"type": "browser_search"}], "reasoning_effort": "low",
                   "max_completion_tokens": 1024,
                   "messages": [{"role": "user", "content": f"Search the web for: {query.strip()[:300]}\n"
                                                            "Then reply with just the word done."}]}
        try:
            resp = self.session.post(self.base_url + "/chat/completions", json=payload, timeout=self.timeout_s,
                                     headers={"Authorization": f"Bearer {key}"})
        except requests.RequestException as exc:
            log.info("Groq web search could not reach Groq (%s)", type(exc).__name__)
            return []
        if resp.status_code == 429:
            retry = str(resp.headers.get("retry-after") or "")
            self.rest_until = self.clock() + (float(retry) if retry.replace(".", "", 1).isdigit() else 60.0)
            log.info("Groq web search: the free limit is used up for now")
            return []
        if resp.status_code != 200:
            log.info("Groq web search answered %s", resp.status_code)
            return []
        try:
            message = resp.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            return []
        return search_results(message, limit)


def search_results(message: dict, limit: int) -> list[dict]:
    """The pages Groq's search found, in order; text from the pages it opened becomes their snippet."""
    found, opened = [], {}
    for tool in message.get("executed_tools") or []:
        rows = tool.get("search_results") if isinstance(tool, dict) else None
        rows = rows.get("results") if isinstance(rows, dict) else rows
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            if tool.get("type") == "browser_search":
                found.append({"title": " ".join(str(row.get("title") or "").split())[:200], "url": url,
                              "snippet": " ".join(str(row.get("content") or "").split())[:300]})
            elif row.get("content"):
                opened.setdefault(url, " ".join(str(row["content"]).split())[:300])
    out, seen = [], set()
    for r in found:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        r["snippet"] = r["snippet"] or opened.get(r["url"], "")
        out.append(r)
    return out[:limit]

