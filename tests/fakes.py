"""A fake Gemini REST client that behaves like the real API surface the engine uses."""
from __future__ import annotations

import json
from typing import Callable, Optional

from jarvis_books.llm.errors import LLMError

MODELS = [
    {"name": "models/gemini-9.0-flash", "version": "1", "inputTokenLimit": 1_048_576, "outputTokenLimit": 65_536,
     "supportedGenerationMethods": ["generateContent", "countTokens"]},
    {"name": "models/gemini-9.0-flash-lite", "version": "1", "inputTokenLimit": 1_048_576, "outputTokenLimit": 65_536,
     "supportedGenerationMethods": ["generateContent", "countTokens"]},
    {"name": "models/gemini-8.0-flash-lite", "version": "1", "inputTokenLimit": 1_048_576, "outputTokenLimit": 65_536,
     "supportedGenerationMethods": ["generateContent", "countTokens"]},
    {"name": "models/gemini-9.0-flash-image", "version": "1", "inputTokenLimit": 32_768, "outputTokenLimit": 8_192,
     "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-embedding-9", "version": "1", "inputTokenLimit": 8192, "outputTokenLimit": 1,
     "supportedGenerationMethods": ["embedContent"]},
    {"name": "models/gemini-9.0-flash-tts", "version": "1", "inputTokenLimit": 8192, "outputTokenLimit": 16384,
     "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-9.0-live", "version": "1", "inputTokenLimit": 131072, "outputTokenLimit": 8192,
     "supportedGenerationMethods": ["bidiGenerateContent"]},
]


def body(text: str = "", calls: Optional[list[dict]] = None, finish: str = "STOP", prompt_tokens: int = 100,
         output_tokens: int = 50) -> dict:
    parts = [{"text": text}] if text else []
    parts += [{"functionCall": c} for c in (calls or [])]
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": prompt_tokens, "candidatesTokenCount": output_tokens}}


class FakeGemini:
    provider = "gemini"

    def __init__(self, responder: Optional[Callable[[str, dict], dict]] = None, models: Optional[list] = None):
        self.responder = responder or self.default
        self.models = list(MODELS if models is None else models)
        self.calls: list[tuple[str, dict]] = []
        self.errors: list[LLMError] = []  # raised (FIFO) before answering
        self.embed_calls = 0
        self.tts_calls = 0
        self.tts_log: list[tuple] = []
        self.multi_log: list[tuple] = []

    def list_models(self):
        return self.models

    def generate(self, model_id: str, payload: dict, timeout=None) -> dict:
        self.calls.append((model_id, payload))
        if self.errors:
            raise self.errors.pop(0)
        return self.responder(model_id, payload)

    def embed(self, model_id, texts, task_type="RETRIEVAL_DOCUMENT", dimensions=768):
        self.embed_calls += 1
        out = []
        for t in texts:
            words = set(t.lower().split())
            out.append([float(k in words) for k in ("vault", "rain", "dragon", "mara", "key", "fire", "storm", "ship")])
        return out

    def tts(self, model_id, text, voice="Kore", style=""):
        self.tts_calls += 1
        self.tts_log.append((text, voice, style))
        from jarvis_books.llm.gemini import pcm_to_wav
        return pcm_to_wav(b"\x00\x00" * 2400)

    def tts_multi(self, model_id, transcript, speakers, direction=""):
        self.tts_calls += 1
        self.multi_log.append((transcript, list(speakers), direction))
        from jarvis_books.llm.gemini import pcm_to_wav
        return pcm_to_wav(b"\x00\x00" * 4800)

    @staticmethod
    def default(model_id: str, payload: dict) -> dict:
        schema = (payload.get("generationConfig") or {}).get("responseSchema")
        if schema:
            props = schema.get("properties", {})
            if "chapters" in props and props["chapters"].get("type") == "INTEGER":
                return body(json.dumps({"title": "Probe", "chapters": 12}))
            return body(json.dumps(sample_for(schema)))
        if payload.get("tools"):
            return body(calls=[{"name": "lookup_character", "args": {"name": "Mara"}}])
        return body("Rain fell on the vault. Mara waited.\n\nShe turned the key.")


def sample_for(schema: dict):
    t = schema.get("type")
    if t == "OBJECT":
        return {k: sample_for(v) for k, v in schema.get("properties", {}).items()}
    if t == "ARRAY":
        return [sample_for(schema["items"]) for _ in range(max(1, schema.get("minItems", 1)))]
    if t == "INTEGER":
        return 1
    if "enum" in schema:
        return schema["enum"][0]
    return "x"


LFM3_SHOW = {
    "capabilities": ["completion", "tools", "thinking"],
    "model_info": {"lfm2.context_length": 262144, "lfm2.block_count": 16, "lfm2.embedding_length": 2048,
                   "lfm2.attention.head_count": 32,
                   "lfm2.attention.head_count_kv": [0, 0, 8, 0, 0, 8, 0, 0, 8, 0, 8, 0, 8, 0, 8, 0]},
}


class FakeOllama:
    """Ollama's API as the local engine sees it. online=False behaves like Ollama not running."""

    provider = "local"

    def __init__(self, online: bool = True, installed: bool = True, model: str = "hf.co/LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"):
        self.online, self.installed, self.model = online, installed, model
        self.replies: list = []
        self.chats: list[dict] = []
        self.pulls: list[str] = []
        self.loaded: list[dict] = []  # what /api/ps reports as in memory
        self.timeouts: list[tuple] = []  # (timeout, idle_s) of each chat

    def _check(self):
        if not self.online:
            raise LLMError("LFM3 isn't running on this computer (Ollama is not answering)", kind="unavailable")

    def version(self):
        self._check()
        return "0.12.0"

    def running(self):
        self._check()
        return list(self.loaded)

    def list_models(self):
        self._check()
        return [{"name": self.model, "size": 1_100_000_000}] if self.installed else []

    def show(self, model):
        self._check()
        if not self.installed:
            raise LLMError("LFM3 isn't downloaded yet", kind="unavailable", status=404)
        return LFM3_SHOW

    def reply(self, text="", calls=None, prompt_tokens=200, output_tokens=40):
        msg = {"role": "assistant", "content": text}
        if calls:
            msg["tool_calls"] = [{"function": c} for c in calls]
        self.replies.append({"message": msg, "done_reason": "stop", "prompt_eval_count": prompt_tokens,
                             "prompt_eval_duration": int(prompt_tokens / 50 * 1e9), "eval_count": output_tokens,
                             "eval_duration": int(output_tokens / 8 * 1e9)})

    def chat(self, payload, timeout=None, idle_s=None, first_token=None):
        self._check()
        if first_token is not None:
            first_token()
        self.chats.append(payload)
        self.timeouts.append((timeout, idle_s))
        if not self.replies:
            return {"message": {"role": "assistant", "content": "ok from LFM3"}, "prompt_eval_count": 100,
                    "prompt_eval_duration": int(2e9), "eval_count": 16, "eval_duration": int(2e9)}
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def pull(self, model, progress=None, cancel=None):
        self._check()
        self.pulls.append(model)
        if progress:
            progress(0.5, "pulling")
            progress(1.0, "done")
        self.installed = True
