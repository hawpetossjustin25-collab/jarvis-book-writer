"""Gemini Developer API client (REST). The API key is sent in a header and never logged."""
from __future__ import annotations

import base64
import io
import logging
import re
import time
import wave
from typing import Any, Optional

import requests

from jarvis_books.llm.errors import LLMError

log = logging.getLogger(__name__)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
LIVE_URL = ("wss://generativelanguage.googleapis.com/ws/"
            "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")


def _retry_delay(details: list[dict]) -> Optional[float]:
    for d in details:
        if str(d.get("@type", "")).endswith("RetryInfo"):
            m = re.match(r"([\d.]+)s", str(d.get("retryDelay", "")))
            if m:
                return float(m.group(1))
    return None


def _quota(details: list[dict]) -> tuple[Optional[str], Optional[int]]:
    for d in details:
        if str(d.get("@type", "")).endswith("QuotaFailure"):
            for v in d.get("violations", []):
                value = v.get("quotaValue")
                return v.get("quotaId"), int(value) if str(value or "").isdigit() else None
    return None, None


def classify_error(status: int, body: Any, headers: Optional[dict] = None) -> LLMError:
    err = body.get("error", {}) if isinstance(body, dict) else {}
    message = str(err.get("message") or body or f"HTTP {status}")[:500]
    details = err.get("details") or []
    retry_after = _retry_delay(details)
    if retry_after is None and headers and headers.get("Retry-After", "").isdigit():
        retry_after = float(headers["Retry-After"])
    quota_id, quota_value = _quota(details)
    lowered = message.lower()
    if status in (401, 403) or "api key" in lowered or "permission" in lowered:
        kind = "auth"
    elif status == 429:
        kind = "quota" if quota_id and "PerDay" in quota_id else "rate"
        if "limit: 0" in lowered or (quota_value == 0):
            kind = "unavailable"  # model not offered on this tier
    elif status == 404:
        kind = "unavailable"
    elif status in (500, 502, 503, 504):
        kind = "overload"
    elif status == 400 and ("token" in lowered and ("exceed" in lowered or "too long" in lowered or "limit" in lowered)):
        kind = "context"
    elif status == 400:
        kind = "invalid"
    else:
        kind = "unknown"
    return LLMError(message, kind=kind, status=status, retry_after_s=retry_after, quota_id=quota_id,
                    quota_value=quota_value)


class GeminiClient:
    provider = "gemini"

    def __init__(self, api_key: str, session: Optional[requests.Session] = None, base_url: str = BASE_URL,
                 timeout_s: float = 180.0):
        if not api_key:
            raise LLMError("no Gemini API key configured", kind="auth")
        self._key = api_key
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    @property
    def live_url(self) -> str:
        return LIVE_URL

    def live_headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._key}

    def _request(self, method: str, path: str, payload: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            resp = self.session.request(method, url, json=payload, timeout=timeout or self.timeout_s,
                                        headers={"x-goog-api-key": self._key, "Content-Type": "application/json"})
        except requests.Timeout as exc:
            raise LLMError(f"request timed out: {exc.__class__.__name__}", kind="network") from None
        except requests.RequestException as exc:
            raise LLMError(f"network error: {exc.__class__.__name__}", kind="network") from None
        try:
            body = resp.json()
        except ValueError:
            body = {"error": {"message": resp.text[:300]}}
        if resp.status_code >= 400:
            raise classify_error(resp.status_code, body, dict(resp.headers))
        return body

    # ---- discovery --------------------------------------------------------------
    def list_models(self) -> list[dict]:
        models, token = [], None
        for _ in range(20):
            path = "models?pageSize=200" + (f"&pageToken={token}" if token else "")
            body = self._request("GET", path, timeout=30)
            models.extend(body.get("models", []))
            token = body.get("nextPageToken")
            if not token:
                break
        return models

    def get_model(self, model_id: str) -> dict:
        return self._request("GET", f"models/{model_id}", timeout=30)

    # ---- generation ---------------------------------------------------------------
    def generate(self, model_id: str, payload: dict, timeout: Optional[float] = None) -> dict:
        return self._request("POST", f"models/{model_id}:generateContent", payload, timeout)

    def count_tokens(self, model_id: str, contents: list[dict]) -> int:
        body = self._request("POST", f"models/{model_id}:countTokens", {"contents": contents}, timeout=30)
        return int(body.get("totalTokens", 0))

    def embed(self, model_id: str, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT",
              dimensions: Optional[int] = 768) -> list[list[float]]:
        requests_ = []
        for text in texts:
            item: dict[str, Any] = {"model": f"models/{model_id}", "content": {"parts": [{"text": text}]},
                                    "taskType": task_type}
            if dimensions:
                item["outputDimensionality"] = dimensions
            requests_.append(item)
        body = self._request("POST", f"models/{model_id}:batchEmbedContents", {"requests": requests_}, timeout=120)
        return [e.get("values", []) for e in body.get("embeddings", [])]

    def tts(self, model_id: str, text: str, voice: str = "Kore", style: str = "") -> bytes:
        """Synthesize speech. Returns WAV bytes (wraps raw PCM if the API returns PCM)."""
        prompt = f"{style.strip()}: {text}" if style.strip() else text
        return self._speak(model_id, prompt, {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}})

    def tts_multi(self, model_id: str, transcript: str, speakers: list[tuple[str, str]], direction: str = "") -> bytes:
        """Two voices in one request. `transcript` has lines like 'Lily: text'; speakers is [(name, voice)] x 2."""
        if len(speakers) != 2 or speakers[0][0] == speakers[1][0]:
            raise ValueError("multi-speaker speech needs exactly two different speakers")
        prompt = f"{direction.strip()}\n\n{transcript}" if direction.strip() else transcript
        configs = [{"speaker": name, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}
                   for name, voice in speakers]
        return self._speak(model_id, prompt, {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": configs}})

    def _speak(self, model_id: str, prompt: str, speech_config: dict) -> bytes:
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": speech_config},
        }
        body = self.generate(model_id, payload)
        try:
            part = body["candidates"][0]["content"]["parts"][0]["inlineData"]
        except (KeyError, IndexError):
            raise LLMError("speech model returned no audio", kind="invalid_output") from None
        data = base64.b64decode(part["data"])
        mime = part.get("mimeType", "")
        if data[:4] == b"RIFF":
            return data
        rate = int(re.search(r"rate=(\d+)", mime).group(1)) if "rate=" in mime else 24000
        return pcm_to_wav(data, rate)


def pcm_to_wav(pcm: bytes, rate: int = 24000, channels: int = 1, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def build_payload(system: str, contents: list[dict], json_schema: Optional[dict], tools: list[dict],
                  max_output_tokens: Optional[int], temperature: Optional[float],
                  thinking_budget: Optional[int] = None) -> dict:
    payload: dict[str, Any] = {"contents": contents}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    config: dict[str, Any] = {}
    if json_schema:
        config["responseMimeType"] = "application/json"
        config["responseSchema"] = json_schema
    if max_output_tokens:
        config["maxOutputTokens"] = int(max_output_tokens)
    if temperature is not None:
        config["temperature"] = float(temperature)
    if thinking_budget is not None:
        config["thinkingConfig"] = {"thinkingBudget": int(thinking_budget)}
    if config:
        payload["generationConfig"] = config
    if tools:
        payload["tools"] = [{"functionDeclarations": tools}]
    # Fiction needs room for conflict, violence and dark themes; keep only the strictest blocks.
    payload["safetySettings"] = [
        {"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in (
            "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
    ]
    return payload


def parse_response(body: dict) -> dict:
    candidates = body.get("candidates") or []
    feedback = body.get("promptFeedback") or {}
    if not candidates:
        reason = feedback.get("blockReason", "no candidates")
        raise LLMError(f"model returned nothing ({reason})", kind="safety" if feedback.get("blockReason") else "invalid_output")
    cand = candidates[0]
    content = cand.get("content") or {"role": "model", "parts": []}
    parts = content.get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    calls = [p["functionCall"] for p in parts if "functionCall" in p]
    usage = body.get("usageMetadata") or {}
    finish = cand.get("finishReason")
    if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST") and not text and not calls:
        raise LLMError(f"response blocked ({finish})", kind="safety")
    return {
        "text": text, "function_calls": calls, "content": content, "finish_reason": finish,
        "prompt_tokens": usage.get("promptTokenCount"),
        "output_tokens": (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0) or None,
        "model_version": body.get("modelVersion"),
    }


def now() -> float:
    return time.time()
