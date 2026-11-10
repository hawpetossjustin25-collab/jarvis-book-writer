import base64
import json
import queue
import sys
import threading
import time

import pytest

from fakes import FakeGemini
from jarvis_books.keystore import load_key, save_key
from jarvis_books.studio import Studio
from jarvis_books.voice import LiveSession


class FakeSocket:
    """Plays a scripted Gemini Live server."""

    def __init__(self, script):
        self.sent = []
        self.incoming = queue.Queue()
        self.script = script
        self.closed = False

    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        for reply in self.script(msg):
            self.incoming.put(json.dumps(reply))

    def __iter__(self):
        while not self.closed:
            try:
                item = self.incoming.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is None:
                return
            yield item

    def close(self):
        self.closed = True


def server(msg):
    if "setup" in msg:
        return [{"setupComplete": {}}, {"sessionResumptionUpdate": {"resumable": True, "newHandle": "h1"}}]
    if "realtimeInput" in msg and "text" in msg["realtimeInput"]:
        return [{"serverContent": {"inputTranscription": {"text": "what books"}}},
                {"toolCall": {"functionCalls": [{"id": "c1", "name": "list_books", "args": {}}]}}]
    if "toolResponse" in msg:
        audio = base64.b64encode(b"\x01\x00" * 100).decode()
        return [{"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000",
                                                                         "data": audio}}]},
                                   "outputTranscription": {"text": "You have one book."}}},
                {"serverContent": {"interrupted": True}}, {"serverContent": {"turnComplete": True}}]
    return []


def test_live_session_tools_audio_transcripts_and_barge_in():
    sock = FakeSocket(server)
    audio, transcripts, tools, interrupts = [], [], [], []
    s = LiveSession("wss://x", {}, "gemini-9.0-live", "You are Jarvis", [{"name": "list_books"}],
                    lambda n, a: {"books": [{"title": "Vault"}]}, context_tokens=131072,
                    on_audio=audio.append, on_transcript=lambda r, t: transcripts.append((r, t)),
                    on_tool=lambda n, a, r: tools.append(n), on_interrupted=lambda: interrupts.append(1),
                    connect=lambda url, headers: sock)
    assert s.start(wait_s=5)
    setup = sock.sent[0]["setup"]
    assert setup["model"] == "models/gemini-9.0-live"
    assert setup["contextWindowCompression"]["triggerTokens"] == int(131072 * 0.8)
    s.send_audio(b"\x00\x00" * 160)
    assert sock.sent[1]["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"
    s.send_text("what books do I have")
    deadline = time.time() + 5
    while not interrupts and time.time() < deadline:
        time.sleep(0.02)
    response = next(m for m in sock.sent if "toolResponse" in m)["toolResponse"]["functionResponses"][0]
    assert response["id"] == "c1" and response["response"]["books"][0]["title"] == "Vault"
    assert tools == ["list_books"] and audio and audio[0] == b"\x01\x00" * 100
    assert ("jarvis", "You have one book.") in transcripts and ("user", "what books") in transcripts
    assert interrupts and s.resume_handle == "h1"
    s.stop()
    assert s.state == "idle"


def test_resumption_handle_is_sent_on_reconnect():
    s = LiveSession("wss://x", {}, "m", "sys", [], lambda n, a: {})
    s.resume_handle = "abc"
    assert s.setup_message()["setup"]["sessionResumption"] == {"handle": "abc"}


def test_keystore_roundtrip_without_plaintext_on_windows(tmp_path, monkeypatch):
    for var in ("JARVIS_GEMINI_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert load_key(tmp_path) is None
    path = save_key("  secret-value  ", tmp_path)
    assert load_key(tmp_path) == "secret-value"
    if sys.platform == "win32":
        assert b"secret-value" not in path.read_bytes()
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    assert load_key(tmp_path) == "env-key"


def test_studio_wires_everything(tmp_path, monkeypatch):
    from conftest import hardware
    studio = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(),
                    engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None})
    studio.start()
    assert studio.ready.is_set() and studio.start_error is None
    book = studio.library.create_book("Wired", "fantasy")
    studio.library.set_outline(book.id, [{"number": 1, "title": "One", "outline": "x"}])
    studio.library.save_chapter_text(studio.library.get_chapter(book.id, 1).id, "The dragon slept on gold.")
    out = studio.export(book.id, "docx", tmp_path / "out")
    assert out["saved"].endswith(".docx")
    studio.set_setting("voice", "Kore")
    assert studio.narrator.voice == "Kore" and studio.settings()["voice"] == "Kore"
    deadline = time.time() + 5
    while time.time() < deadline and not studio.index.search("dragon gold", book.id):
        time.sleep(0.05)
    assert studio.index.search("dragon gold", book.id)  # chapter text is indexed automatically
    result = studio.tools.run("export_book", {"book": "wired", "format": "epub"})
    assert result.get("saved", "").endswith(".epub")
    studio.close()


def test_studio_without_key_reports_clearly(tmp_path, monkeypatch):
    for var in ("JARVIS_GEMINI_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    studio = Studio(home=tmp_path / "h", mirror=tmp_path / "m")
    studio.start()
    assert "API key" in studio.start_error
    studio.close()


def test_live_setup_pins_english():
    s = LiveSession("wss://x", {}, "gemini-2.0-flash-live", "You are Jarvis", [], lambda n, a: {})
    setup = s.setup_message()["setup"]
    assert setup["generationConfig"]["speechConfig"]["languageCode"] == "en-US"
    text = setup["systemInstruction"]["parts"][0]["text"]
    assert text.startswith("LANGUAGE: always speak and answer in English") and "You are Jarvis" in text
    native = LiveSession("wss://x", {}, "gemini-2.5-flash-native-audio", "sys", [], lambda n, a: {})
    native_setup = native.setup_message()["setup"]
    assert "languageCode" not in native_setup["generationConfig"]["speechConfig"]  # Google rejects it there
    assert "in English" in native_setup["systemInstruction"]["parts"][0]["text"]


class ExpiredSocket(FakeSocket):
    def __iter__(self):
        raise RuntimeError("received 1008 (policy violation) BidiGenerateContent session not found or expired")
        yield


def test_expired_resume_handle_is_dropped_and_a_fresh_session_starts(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    stale, fresh = ExpiredSocket(server), FakeSocket(server)
    sockets = [stale, fresh]
    s = LiveSession("wss://x", {}, "m", "sys", [], lambda n, a: {}, connect=lambda url, headers: sockets.pop(0))
    s.resume_handle = "stale"
    assert s.start(wait_s=5)
    assert stale.sent[0]["setup"]["sessionResumption"] == {"handle": "stale"}
    assert fresh.sent[0]["setup"]["sessionResumption"] == {}
    assert s.resume_handle == "h1"
    s.stop()


def test_failed_sessions_do_not_retry_forever(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    made = []

    def connect(url, headers):
        made.append(1)
        return ExpiredSocket(server)
    s = LiveSession("wss://x", {}, "m", "sys", [], lambda n, a: {}, connect=connect)
    s.resume_handle = "stale"
    assert not s.start(wait_s=2)
    deadline = time.time() + 5
    while s.state != "offline" and time.time() < deadline:
        time.sleep(0.02)
    assert s.state == "offline" and len(made) == s.max_reconnects + 1 and s.resume_handle is None
