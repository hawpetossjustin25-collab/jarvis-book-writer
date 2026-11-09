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
