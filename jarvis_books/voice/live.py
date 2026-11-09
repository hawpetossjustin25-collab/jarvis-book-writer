"""Gemini Live voice session: streams mic audio up, plays Jarvis's voice back, and runs book tools.

Transport only (no Qt): the UI feeds 16 kHz mono PCM from the microphone and plays the 24 kHz PCM
it receives. Supports barge-in (server 'interrupted'), tool calls, transcripts, session resumption
after the server's goAway, and context-window compression sized from the live model's limit.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

IN_RATE = 16000
OUT_RATE = 24000


def _default_connect(url: str, headers: dict):
    from websockets.sync.client import connect
    return connect(url, additional_headers=headers, max_size=None, open_timeout=20)


class LiveSession:
    def __init__(self, url: str, headers: dict, model: str, system: str, tools: list[dict],
                 call_tool: Callable[[str, dict], dict], voice: str = "Charon", context_tokens: Optional[int] = None,
                 on_audio: Callable[[bytes], None] = lambda b: None,
                 on_transcript: Callable[[str, str], None] = lambda role, text: None,
                 on_state: Callable[[str], None] = lambda s: None,
                 on_interrupted: Callable[[], None] = lambda: None,
                 on_tool: Callable[[str, dict, dict], None] = lambda n, a, r: None,
                 connect: Callable = _default_connect, max_reconnects: int = 3):
        self.url, self.headers, self.model = url, headers, model
        self.system, self.tools, self.call_tool, self.voice = system, tools, call_tool, voice
        self.context_tokens = context_tokens
        self.on_audio, self.on_transcript, self.on_state = on_audio, on_transcript, on_state
        self.on_interrupted, self.on_tool = on_interrupted, on_tool
        self._connect = connect
        self.max_reconnects = max_reconnects
        self._ws = None
        self._send_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.resume_handle: Optional[str] = None
        self.state = "idle"

    def setup_message(self) -> dict:
        setup: dict = {
            "model": self.model if self.model.startswith("models/") else f"models/{self.model}",
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}}},
            "systemInstruction": {"parts": [{"text": self.system}]},
            "tools": [{"functionDeclarations": self.tools}] if self.tools else [],
            "inputAudioTranscription": {}, "outputAudioTranscription": {},
            # Low start sensitivity: leftover speaker echo and room noise should not count as the user talking.
            "realtimeInputConfig": {"automaticActivityDetection": {
                "startOfSpeechSensitivity": "START_SENSITIVITY_LOW", "prefixPaddingMs": 200,
                "silenceDurationMs": 800}},
            "sessionResumption": {"handle": self.resume_handle} if self.resume_handle else {},
        }
        if self.context_tokens:
            # Compress the oldest turns before the live model's verified context limit is reached.
            setup["contextWindowCompression"] = {"triggerTokens": int(self.context_tokens * 0.8),
                                                 "slidingWindow": {"targetTokens": int(self.context_tokens * 0.5)}}
        return {"setup": setup}

    # ---- lifecycle -------------------------------------------------------------------
    def start(self, wait_s: float = 20.0) -> bool:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-live", daemon=True)
        self._thread.start()
        return self._ready.wait(wait_s)

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._set_state("idle")

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.on_state(state)

    def _run(self) -> None:
        attempts = 0
        while not self._stop.is_set():
            try:
                self._set_state("connecting")
                self._ws = self._connect(self.url, self.headers)
                self._send(self.setup_message())
                attempts = 0
                reconnect = self._receive_loop()
            except Exception as exc:
                if self._stop.is_set():
                    break
                attempts += 1
                log.warning("live session error (%s), attempt %d", exc, attempts)
                reconnect = attempts <= self.max_reconnects
                if reconnect:
                    time.sleep(min(8, 2 ** attempts))
            finally:
                self._ready.clear()
                try:
                    if self._ws is not None:
                        self._ws.close()
                except Exception:
                    pass
                self._ws = None
            if not reconnect:
                break
        self._set_state("idle" if self._stop.is_set() else "offline")

    def _receive_loop(self) -> bool:
        """Returns True when the session should reconnect (server goAway)."""
        for raw in self._ws:
            if self._stop.is_set():
                return False
            msg = json.loads(raw)
            if "setupComplete" in msg:
                self._ready.set()
                self._set_state("listening")
            if "sessionResumptionUpdate" in msg:
                upd = msg["sessionResumptionUpdate"]
                if upd.get("resumable") and upd.get("newHandle"):
                    self.resume_handle = upd["newHandle"]
            if "toolCall" in msg:
                self._handle_tools(msg["toolCall"].get("functionCalls", []))
            if "serverContent" in msg:
                self._handle_content(msg["serverContent"])
            if "goAway" in msg:
                log.info("server asked to reconnect (%s)", msg["goAway"].get("timeLeft"))
                return True
        return not self._stop.is_set() and self.resume_handle is not None

    def _handle_content(self, content: dict) -> None:
        if content.get("interrupted"):
            self.on_interrupted()
            self._set_state("listening")
        for part in (content.get("modelTurn") or {}).get("parts", []):
            data = (part.get("inlineData") or {}).get("data")
            if data:
                self._set_state("speaking")
                self.on_audio(base64.b64decode(data))
        if (content.get("inputTranscription") or {}).get("text"):
            self.on_transcript("user", content["inputTranscription"]["text"])
        if (content.get("outputTranscription") or {}).get("text"):
            self.on_transcript("jarvis", content["outputTranscription"]["text"])
        if content.get("turnComplete"):
            self.on_transcript("turn", "")
            self._set_state("listening")

    def _handle_tools(self, calls: list[dict]) -> None:
        self._set_state("working")
        responses = []
        for call in calls:
            name, args = call.get("name", ""), call.get("args") or {}
            try:
                result = self.call_tool(name, args)
            except Exception as exc:
                result = {"error": str(exc)}
            self.on_tool(name, args, result)
            raw = json.dumps(result, ensure_ascii=False)
            if len(raw) > 30000:
                result = {"truncated": True, "content": raw[:30000]}
            responses.append({"id": call.get("id"), "name": name, "response": result})
        self._send({"toolResponse": {"functionResponses": responses}})

    # ---- input -----------------------------------------------------------------------
    def _send(self, msg: dict) -> None:
        ws = self._ws
        if ws is None:
            return
        with self._send_lock:
            ws.send(json.dumps(msg))

    def send_audio(self, pcm16: bytes) -> None:
        if self._ready.is_set() and pcm16:
            self._send({"realtimeInput": {"audio": {"data": base64.b64encode(pcm16).decode(),
                                                    "mimeType": f"audio/pcm;rate={IN_RATE}"}}})

    def send_text(self, text: str) -> None:
        if self._ready.is_set() and text.strip():
            self._send({"realtimeInput": {"text": text}})

    def end_audio(self) -> None:
        if self._ready.is_set():
            self._send({"realtimeInput": {"audioStreamEnd": True}})
