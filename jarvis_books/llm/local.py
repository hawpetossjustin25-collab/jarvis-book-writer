"""LFM3 (or any model) running on this computer through Ollama, behind the same request API as Gemini.

The engine adapts to the machine the way the Gemini engine adapts to the API: it reads the model's real
context window and tool support from Ollama, sizes the context to the RAM that is free, and keeps measuring
how fast this CPU reads prompts and writes answers so every request fits a time budget.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

import requests

from jarvis_books.llm.errors import InvalidStructuredOutput, LLMError
from jarvis_books.llm.hardware import available_ram, probe_hardware
from jarvis_books.llm.history import fit as fit_turns, window
from jarvis_books.llm.models import HardwareProfile, LLMRequest, LLMResponse, ModelBudget, RuntimeConfig
from jarvis_books.llm.policy import TEXT_ROLES
from jarvis_books.llm.prompt import _cut, fit_sections
from jarvis_books.llm.schema import parse_json, validate
from jarvis_books.llm.sizing import estimate_tokens

log = logging.getLogger(__name__)

DEFAULT_MODEL = "hf.co/LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"
DEFAULT_URL = "http://127.0.0.1:11434"
LABEL = "LFM3"


def to_json_schema(schema: dict) -> dict:
    """Gemini's upper-case schema subset to standard JSON Schema (what Ollama expects)."""
    out: dict = {}
    for key, value in schema.items():
        if key == "type":
            out["type"] = str(value).lower()
        elif key == "properties":
            out["properties"] = {k: to_json_schema(v) for k, v in value.items()}
        elif key == "items":
            out["items"] = to_json_schema(value)
        elif key in ("required", "enum", "description", "minItems", "maxItems"):
            out[key] = value
    return out


def to_messages(system: str, contents: list[dict]) -> list[dict]:
    """Gemini 'contents' turns (text, functionCall, functionResponse parts) to Ollama chat messages."""
    messages = [{"role": "system", "content": system}] if system else []
    for turn in contents:
        parts = turn.get("parts") or []
        text = "".join(p.get("text", "") for p in parts if "text" in p)
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        replies = [p["functionResponse"] for p in parts if "functionResponse" in p]
        if turn.get("role") == "model":
            msg: dict = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = [{"function": {"name": c.get("name", ""), "arguments": c.get("args") or {}}}
                                     for c in calls]
            messages.append(msg)
        else:
            for r in replies:
                messages.append({"role": "tool", "tool_name": r.get("name", ""),
                                 "content": json.dumps(r.get("response", {}), ensure_ascii=False)})
            if text:
                messages.append({"role": "user", "content": text})
    return messages


def to_tools(declarations: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": d["name"], "description": d.get("description", ""),
                                              "parameters": to_json_schema(d.get("parameters") or {"type": "OBJECT"})}}
            for d in declarations]


def _strip_thinking(text: str, partial: bool = False) -> str:
    """partial: an answer still being written, whose thinking may not have closed yet (none of it is shown)."""
    while "<think>" in text and "</think>" in text:
        start, end = text.index("<think>"), text.index("</think>") + len("</think>")
        text = text[:start] + text[end:]
    if partial and "<think>" in text:
        text = text[:text.index("<think>")]
    return text.strip()


def find_ollama() -> Optional[str]:
    found = shutil.which("ollama")
    if found:
        return found
    if sys.platform == "win32":
        exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if exe.exists():
            return str(exe)
    return None


def launch_command(exe: str) -> list[str]:
    """How to start Ollama. On Windows its tray app owns the server: a bare `ollama serve` started first holds
    the port, and the tray app then retries its own server every few seconds forever, eating CPU."""
    app = Path(exe).with_name("ollama app.exe")
    if sys.platform == "win32" and app.exists():
        return [str(app)]
    return [exe, "serve"]


class OllamaClient:
    """Ollama's REST API (/api/chat, /api/tags, /api/show, /api/pull)."""

    provider = "local"

    def __init__(self, base_url: str = DEFAULT_URL, session: Optional[requests.Session] = None,
                 timeout_s: float = 600.0, launcher: Optional[Callable[[], bool]] = None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout_s = timeout_s
        self.launcher = launcher if launcher is not None else self._launch
        self._launched = False

    def _launch(self) -> bool:
        """Start Ollama in the background if it is installed but not running (once per app run)."""
        exe = find_ollama()
        if not exe or self._launched:
            return False
        self._launched = True
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(launch_command(exe), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags)
        for _ in range(40):
            time.sleep(0.5)
            try:
                self.session.get(self.base_url + "/api/version", timeout=2)
                return True
            except requests.RequestException:
                continue
        return False

    def _call(self, method: str, path: str, payload: Optional[dict] = None, timeout: Optional[float] = None,
              stream: bool = False):
        for attempt in (1, 2):
            try:
                resp = self.session.request(method, self.base_url + path, json=payload, stream=stream,
                                            timeout=timeout or self.timeout_s)
                break
            except requests.Timeout as exc:
                raise LLMError(f"{LABEL} took too long to answer", kind="network") from exc
            except requests.RequestException as exc:
                if attempt == 1 and self.launcher():
                    continue
                raise LLMError(f"{LABEL} isn't running on this computer (Ollama is not answering)",
                               kind="unavailable") from exc
        if resp.status_code == 404:
            raise LLMError(f"{LABEL} isn't downloaded yet", kind="unavailable", status=404)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", resp.text)
            except ValueError:
                detail = resp.text
            kind = "unavailable" if "architecture" in str(detail) or "not found" in str(detail) else "unknown"
            raise LLMError(f"{LABEL} error: {str(detail)[:300]}", kind=kind, status=resp.status_code)
        return resp

    def running(self) -> list[dict]:
        """Models Ollama has in memory now, with the bytes each one holds."""
        return self._call("GET", "/api/ps", timeout=10).json().get("models", [])

    def version(self) -> str:
        return self._call("GET", "/api/version", timeout=5).json().get("version", "")

    def list_models(self) -> list[dict]:
        return self._call("GET", "/api/tags", timeout=10).json().get("models", [])

    def load(self, model: str, keep_alive) -> None:
        """Load a model into memory now (an empty chat), so the first real message doesn't wait for the disk."""
        self._call("POST", "/api/chat", {"model": model, "messages": [], "keep_alive": keep_alive, "stream": False},
                   timeout=600)

    def show(self, model: str) -> dict:
        return self._call("POST", "/api/show", {"model": model}, timeout=30).json()

    def chat(self, payload: dict, timeout: Optional[float] = None, idle_s: Optional[float] = None,
             first_token: Optional[Callable[[], None]] = None,
             on_text: Optional[Callable[[str], None]] = None) -> dict:
        """One answer. With idle_s the answer streams: it may take up to `timeout` in all, as long as the model
        never goes quiet for idle_s (reading the prompt counts as quiet, so idle_s must cover that too)."""
        if idle_s is None:
            return self._call("POST", "/api/chat", dict(payload, stream=False), timeout=timeout).json()
        deadline = time.monotonic() + (timeout or self.timeout_s)
        resp = self._call("POST", "/api/chat", dict(payload, stream=True), timeout=(10, idle_s), stream=True)
        text: list[str] = []
        calls: list[dict] = []
        try:
            for line in resp.iter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                if msg.get("error"):
                    raise LLMError(f"{LABEL} error: {str(msg['error'])[:300]}", kind="unknown")
                part = msg.get("message") or {}
                if first_token is not None and (part.get("content") or part.get("tool_calls")):
                    first_token()
                    first_token = None
                text.append(part.get("content") or "")
                calls.extend(part.get("tool_calls") or [])
                if on_text is not None and part.get("content"):
                    on_text(_strip_thinking("".join(text), partial=True))
                if msg.get("done"):
                    message = dict(part, role="assistant", content="".join(text))
                    if calls:
                        message["tool_calls"] = calls
                    return dict(msg, message=message)
                if time.monotonic() > deadline:
                    raise LLMError(f"{LABEL} took too long to answer", kind="network")
        except requests.RequestException as exc:
            raise LLMError(f"{LABEL} took too long to answer (nothing new for {idle_s / 60:.0f} min)",
                           kind="network") from exc
        finally:
            resp.close()
        raise LLMError(f"{LABEL} stopped before finishing its answer", kind="network")

    def pull(self, model: str, progress: Optional[Callable[[float, str], None]] = None,
             cancel: Optional[Callable[[], bool]] = None) -> None:
        resp = self._call("POST", "/api/pull", {"model": model, "stream": True}, timeout=3600, stream=True)
        for line in resp.iter_lines():
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("error"):
                raise LLMError(f"download failed: {msg['error']}", kind="unknown")
            if progress and msg.get("total"):
                progress(min(0.99, msg.get("completed", 0) / msg["total"]), msg.get("status", ""))
            if cancel and cancel():
                resp.close()
                raise LLMError("download cancelled", kind="unknown")
        if progress:
            progress(1.0, "done")


@dataclass
class LocalProfile:
    model: str
    context_window: int
    supports_tools: bool
    size_bytes: int
    supports_thinking: bool
    kv_bytes_per_token: int


@dataclass
class Speed:
    """Measured on this computer; starts at conservative CPU guesses and moves toward real numbers."""
    prompt_tps: float = 40.0
    output_tps: float = 6.0
    samples: int = 0

    def update(self, prompt_tps: Optional[float], output_tps: Optional[float]) -> None:
        a = 0.5 if self.samples < 3 else 0.25
        if prompt_tps:
            self.prompt_tps = (1 - a) * self.prompt_tps + a * prompt_tps
        if output_tps:
            self.output_tps = (1 - a) * self.output_tps + a * output_tps
        self.samples += 1


class LocalEngine:
    """Adaptive engine for a model served by Ollama. Same generate()/get_runtime_configuration() as Gemini's."""

    MIN_CTX, MAX_CTX = 4096, 65536
    HEADROOM = 1_500_000_000  # RAM left for Windows and the app itself

    def __init__(self, client: OllamaClient, model: str = DEFAULT_MODEL, state_path: Optional[Path] = None,
                 hardware_probe: Callable[[], HardwareProfile] = probe_hardware,
                 answer_budget_s: float = 90.0, prompt_budget_s: float = 60.0, label: str = LABEL,
                 fit_request: bool = False, ram_probe: Callable[[], Optional[int]] = available_ram,
                 stream_cap_s: Optional[float] = None, max_ctx: Optional[int] = None,
                 keep_alive: Optional[int | str] = None):
        self.client = client
        # keep_alive: how long Ollama keeps the model in memory after an answer (-1 = until it closes). Loading a
        # model from disk costs seconds on every message that comes after a quiet spell.
        self.keep_alive = keep_alive
        self._tools_sent: list[dict] = []
        # max_ctx: the most context this engine ever uses. Reading a long prompt slows down on a small CPU far
        # more than short requests show, so a request-sized context still needs a ceiling.
        self.max_ctx = max_ctx or self.MAX_CTX
        # stream_cap_s: stream answers and allow up to this long for one, as long as the model keeps writing.
        # A slow CPU's real speed on a long prompt is far below what short requests measure.
        self.stream_cap_s = stream_cap_s
        self.label = label
        # fit_request: size the context to each request (as far as the RAM free at that moment allows) instead of
        # to the time budget alone. For long, unattended jobs such as upgrades that read a lot of code.
        self.fit_request = fit_request
        self.ram_probe = ram_probe
        self.chars_per_token = 3.0 if fit_request else 4.0  # code packs more tokens per character than prose
        self.model = model
        self.state_path = state_path
        self.hardware_probe = hardware_probe
        self.answer_budget_s = answer_budget_s
        self.prompt_budget_s = prompt_budget_s
        self.profile: Optional[LocalProfile] = None
        self.hardware: Optional[HardwareProfile] = None
        self.speed = Speed()
        self.problem = "not checked yet"
        self.num_ctx = self.MIN_CTX
        self._lock = threading.Lock()
        self._load_state()

    # ---- state -------------------------------------------------------------------
    def _load_state(self) -> None:
        if self.state_path and self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.speed = Speed(**data.get("speed", {}))
            except (ValueError, TypeError, OSError):
                log.warning("local model state unreadable; measuring again")

    def _save_state(self) -> None:
        if self.state_path:
            try:
                self.state_path.write_text(json.dumps({"model": self.model, "speed": self.speed.__dict__}),
                                           encoding="utf-8")
            except OSError:
                log.debug("could not save local model state", exc_info=True)

    # ---- discovery and sizing -------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.profile is not None

    def installed(self) -> bool:
        names = {m.get("name") or m.get("model") for m in self.client.list_models()}
        return self.model in names or f"{self.model}:latest" in names

    def initialize(self) -> bool:
        """Find the model, read its real limits and size it to this computer. False (with .problem) if unusable."""
        try:
            self.hardware = self.hardware_probe()
            if not self.installed():
                self.profile, self.problem = None, f"{self.label} isn't downloaded yet"
                return False
            info = self.client.show(self.model)
        except LLMError as exc:
            self.profile, self.problem = None, str(exc)
            return False
        model_info = info.get("model_info") or {}
        ctx = next((int(v) for k, v in model_info.items() if k.endswith(".context_length")), None) or 32768
        caps = info.get("capabilities")
        tools = "tools" in caps if isinstance(caps, list) else "tools" in str(info.get("template", ""))
        size = next((int(m.get("size", 0)) for m in self.client.list_models()
                     if (m.get("name") or m.get("model")) in (self.model, f"{self.model}:latest")), 0)
        thinking = isinstance(caps, list) and "thinking" in caps
        self.profile = LocalProfile(self.model, ctx, tools, size, thinking, _kv_bytes(model_info))
        self.problem = ""
        self._size()
        log.info("local model %s ready: context %d (window %d), tools %s", self.model, self.num_ctx, ctx, tools)
        return True

    def warm(self) -> bool:
        """Load the model into memory ahead of the first message. False when Ollama can't."""
        if not self.available:
            return False
        try:
            self.client.load(self.model, self.keep_alive if self.keep_alive is not None else "5m")
            return True
        except LLMError as exc:
            log.info("couldn't preload %s: %s", self.model, exc)
            return False

    def _size(self, need: Optional[int] = None) -> None:
        p = self.profile
        free = (self.hardware.available_ram_bytes if self.hardware else None) or 4 * 1024 ** 3
        kv_room = max(0, free - p.size_bytes - self.HEADROOM)
        by_ram = kv_room // max(1, p.kv_bytes_per_token)
        by_time = int(self.speed.prompt_tps * self.prompt_budget_s) + self.output_tokens()
        ctx = min(p.context_window, self.max_ctx, by_ram, max(self.MIN_CTX, by_time))
        if need is not None:
            ctx = min(ctx, -(-need // 1024) * 1024)
        self.num_ctx = max(self.MIN_CTX, int(ctx) // 1024 * 1024)

    def _free_ram(self) -> Optional[int]:
        """Free RAM now, counting what this model already holds in Ollama as available to it."""
        free = self.ram_probe()
        if free is None:
            return None
        try:
            free += sum(int(m.get("size") or 0) for m in self.client.running()
                        if (m.get("name") or m.get("model")) in (self.model, f"{self.model}:latest"))
        except (LLMError, AttributeError, ValueError, TypeError):
            pass
        return free

    def _fit_context(self, request: LLMRequest) -> None:
        cpt = self.chars_per_token
        text = request.system + request.instruction + "".join(s.render() for s in request.sections)
        need = (estimate_tokens(text, cpt) + estimate_tokens(json.dumps(request.history or [], default=str), cpt)
                + estimate_tokens(json.dumps(request.local_tools if request.local_tools is not None
                                             else request.tools or [], default=str), 3.0)
                + self.output_tokens() + 256)
        free = self._free_ram()
        if free is not None and self.hardware is not None:
            self.hardware = replace(self.hardware, available_ram_bytes=free)
        self._size(need)

    def output_tokens(self) -> int:
        return max(256, min(4096, int(self.speed.output_tps * self.answer_budget_s)))

    def quiet_limit_s(self) -> float:
        """How long a streamed answer may go without a word: loading the model and reading the whole context at a
        third of the measured speed, and never less than the prompt budget."""
        return max(self.prompt_budget_s, 3 * self.num_ctx / max(1.0, self.speed.prompt_tps)) + 300

    def answer_tokens(self) -> int:
        """Room for the answer in the current context: never more than half of it."""
        return min(self.output_tokens(), self.num_ctx // 2)

    def input_tokens(self) -> int:
        return max(1024, self.num_ctx - self.answer_tokens() - 128)

    THINK_ROLES = ("planner", "editor", "writer")
    THINK_MIN_TPS = 20.0  # thinking costs ~10x the tokens; only worth it where this computer writes fast

    def thinks(self, role: str) -> bool:
        return bool(self.profile and self.profile.supports_thinking and role in self.THINK_ROLES
                    and self.speed.output_tps >= self.THINK_MIN_TPS)

    def get_runtime_configuration(self) -> RuntimeConfig:
        if not self.available:
            raise LLMError(self.problem or f"{self.label} is not ready", kind="unavailable")
        out = self.answer_tokens()
        budget = ModelBudget(self.model, self.input_tokens(), out, True, 60, None, None)
        return RuntimeConfig(version=0, routes={r: (self.model,) for r in TEXT_ROLES}, budgets={self.model: budget},
                             memory_budget_tokens=int(self.input_tokens() * 0.35), history_turns=8,
                             words_per_call=max(200, int(out / 1.35 * 0.6)), scene_words=max(200, int(out / 1.35 * 0.5)),
                             concurrency=1, tts_workers=1, embed_batch=1, max_retries=1, adaptive=True,
                             reason=f"{self.label} on this computer")

    def status(self) -> dict:
        return {"model": self.model, "ready": self.available, "problem": self.problem, "context": self.num_ctx,
                "tools": bool(self.profile and self.profile.supports_tools),
                "reads_tokens_per_s": round(self.speed.prompt_tps, 1),
                "writes_tokens_per_s": round(self.speed.output_tps, 1)}

    def download(self, progress=None, cancel=None) -> bool:
        self.client.pull(self.model, progress, cancel)
        return self.initialize()

    # ---- generation -------------------------------------------------------------------
    TOOL_SHARE = 0.4  # of the input room; tools past it are dropped from the end of the (most relevant first) list

    def _fit_tools(self, request: LLMRequest, budget_in: int) -> list[dict]:
        """Ollama refuses a request bigger than its context, and the tool list counts too: keep the tools
        that fit (the caller lists the most relevant first). JSON is dense, so it's counted at 3 chars a token."""
        if not (request.tools and self.profile and self.profile.supports_tools):
            return []
        tools = to_tools(request.tools)
        room = int(budget_in * self.TOOL_SHARE)
        while tools and estimate_tokens(json.dumps(tools), 3.0) > room:
            tools.pop()
        return tools

    SYSTEM_SHARE = 0.25  # of the context; it depends on nothing that changes between messages
    HISTORY_TURNS, HISTORY_STEP = 8, 4

    def _steady_tools(self, tools: list[dict], budget_in: int) -> list[dict]:
        """The tools sent last time, while they still cover every tool this request needs and fit: the tool list
        comes first in the prompt, so changing it makes the model reread the whole prompt."""
        names = {t["function"]["name"] for t in tools}
        last = self._tools_sent
        if tools and last and names <= {t["function"]["name"] for t in last} \
                and estimate_tokens(json.dumps(last), 3.0) <= int(budget_in * self.TOOL_SHARE):
            return last
        self._tools_sent = tools
        return tools

    def build(self, request: LLMRequest) -> dict:
        if self.fit_request:
            self._fit_context(request)
        cpt = self.chars_per_token
        budget_in, out = self.input_tokens(), self.answer_tokens()
        tools = self._steady_tools(self._fit_tools(request, budget_in), budget_in)
        # The system prompt is cut against the whole room, not what the tools leave: Ollama reuses its work on a
        # prompt's unchanged start, and a cut that moved with the tool list would make it reread everything.
        system, room = request.system, int(self.num_ctx * self.SYSTEM_SHARE)
        if estimate_tokens(system, cpt) > room:
            system = _cut(system, room, cpt, "start")
        if tools:
            budget_in = max(512, budget_in - estimate_tokens(json.dumps(tools), 3.0))
        answering = not (request.instruction or request.sections)
        history = window(request.history, self.HISTORY_TURNS + self.HISTORY_STEP, answering)
        room = int(budget_in * 0.8) - estimate_tokens(system, cpt)
        history = fit_turns(history, room, lambda t: estimate_tokens(json.dumps(t.get("parts", "")), cpt), answering)
        used = estimate_tokens(system, cpt) + sum(estimate_tokens(json.dumps(t.get("parts", "")), cpt) for t in history)
        contents = list(history)
        if request.instruction or request.sections:
            instruction = request.instruction
            if request.json_schema:
                instruction += "\n\nAnswer with JSON only."
            fit = fit_sections(instruction, request.sections, max(512, budget_in - used), cpt)
            # Optional parts are trimmed to fit; only what must come whole can make a request too big.
            must = estimate_tokens(instruction, cpt) + sum(estimate_tokens(s.render(), cpt)
                                                           for s in request.sections if s.required)
            if self.fit_request and used + must > budget_in:
                raise LLMError(f"{self.label} has room for about {budget_in} tokens with the memory free on this "
                               f"computer right now, and this needs about {used + must}; closing other "
                               f"programs frees more", kind="too_big")
            contents.append({"role": "user", "parts": [{"text": fit.text}]})
        if not contents:
            raise LLMError("empty request", kind="invalid")
        payload: dict = {"model": self.model, "messages": to_messages(system, contents),
                         "options": {"num_ctx": self.num_ctx,
                                     "num_predict": min(request.max_output_tokens or out, out)}}
        if request.temperature is not None:
            payload["options"]["temperature"] = float(request.temperature)
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive
        if self.profile and self.profile.supports_thinking:
            payload["think"] = self.thinks(request.role)
        if tools:
            payload["tools"] = tools
        if request.json_schema:
            payload["format"] = to_json_schema(request.json_schema)
        return payload

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self.available and not self.initialize():
            raise LLMError(self.problem, kind="unavailable")
        with self._lock:  # one request at a time: a small CPU gets slower, not faster, with two
            payload = self.build(request)
            for attempt in (1, 2):
                start = time.perf_counter()
                try:
                    if self.stream_cap_s or request.on_text is not None:
                        body = self.client.chat(payload, idle_s=self.quiet_limit_s(), on_text=request.on_text,
                                                timeout=self.stream_cap_s or
                                                self.prompt_budget_s + self.answer_budget_s + 120)
                    else:
                        body = self.client.chat(payload, timeout=self.prompt_budget_s + self.answer_budget_s + 120)
                except LLMError as exc:
                    if self.label == LABEL:
                        raise
                    raise LLMError(str(exc).replace(LABEL, self.label, 1), kind=exc.kind, status=exc.status) from exc
                latency = time.perf_counter() - start
                self._measure(body)
                msg = body.get("message") or {}
                text = _strip_thinking(msg.get("content") or "")
                calls = [{"name": c["function"]["name"], "args": c["function"].get("arguments") or {}}
                         for c in msg.get("tool_calls") or [] if c.get("function")]
                data = None
                if request.json_schema and not calls:
                    try:
                        data = parse_json(text)
                        errors = validate(data, request.json_schema)
                    except ValueError as exc:
                        errors = [str(exc)]
                    if errors:
                        if attempt == 1:
                            payload["messages"].append({"role": "user", "content": (
                                "That did not match the JSON format (" + "; ".join(errors)[:300] +
                                "). Return only valid JSON matching the schema.")})
                            continue
                        raise InvalidStructuredOutput("; ".join(errors)[:300])
                parts = ([{"text": text}] if text else []) + [{"functionCall": c} for c in calls]
                return LLMResponse(text=text, model=f"{self.label} ({self.model})", data=data, function_calls=calls,
                                   content={"role": "model", "parts": parts},
                                   finish_reason=body.get("done_reason"), prompt_tokens=body.get("prompt_eval_count"),
                                   output_tokens=body.get("eval_count"), latency_s=latency, attempts=attempt)
        raise LLMError("generation failed", kind="unknown")

    def _measure(self, body: dict) -> None:
        def rate(count, ns):
            return count / (ns / 1e9) if count and ns else None
        self.speed.update(rate(body.get("prompt_eval_count"), body.get("prompt_eval_duration")),
                          rate(body.get("eval_count"), body.get("eval_duration")))
        if self.profile:
            before = self.num_ctx
            self._size()
            # A new context size makes Ollama reload the model and forget the prompt it has read, so small
            # changes in measured speed wait until they add up.
            if before != self.num_ctx and abs(self.num_ctx - before) < max(2048, before // 4):
                self.num_ctx = before
        self._save_state()


def _kv_bytes(model_info: dict) -> int:
    """Bytes of KV cache per token: attention layers x kv heads x head size x 2 (K and V) x 2 bytes (f16)."""
    def get(suffix):
        return next((v for k, v in model_info.items() if k.endswith(suffix)), None)
    layers, kv, embed, heads = get(".block_count"), get(".attention.head_count_kv"), get(".embedding_length"), \
        get(".attention.head_count")
    try:
        if isinstance(kv, list):  # hybrid models: layers without attention have 0 kv heads
            kv_total = sum(int(x) for x in kv)
        else:
            kv_total = int(kv) * int(layers)
        head_dim = int(get(".attention.key_length") or (int(embed) // int(heads)))
        return max(1024, kv_total * head_dim * 2 * 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return 128 * 1024  # unknown layout: assume a generous 128 KB per token


__all__ = ["OllamaClient", "LocalEngine", "DEFAULT_MODEL", "LABEL", "find_ollama", "launch_command"]
