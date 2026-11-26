"""Voice notes on Telegram: hearing (Gemini, then Groq's Whisper), speaking (Edge + Opus), and the bridge turn."""
import base64
import subprocess
import time

import pytest
import requests

from conftest import hardware
from fakes import FakeGemini, FakeOllama, body
from jarvis_books.audio.export import ffmpeg_path
from jarvis_books.studio import Studio
from jarvis_books.telegram import TelegramAPI, TelegramError
from jarvis_books.voice.notes import HEAR_PROMPT, MAX_NOTE_BYTES, WHISPER_MODEL, HearingError, VoiceNotes, speakable
from test_telegram import KEY, OWNER_ID, STRANGER, Clock, FakeAssistant, FakeTelegram, library, paired  # noqa: F401

GROQ_KEY = "gsk_" + "y" * 40
NOTE = b"OggS fake opus bytes"


def voice_msg(file_id="f1", chat=OWNER_ID, sender=None, date=1_000_000, size=1200, uid=2):
    return {"update_id": uid, "message": {"message_id": uid, "date": date, "chat": {"id": chat, "type": "private"},
                                          "from": {"id": sender or chat},
                                          "voice": {"file_id": file_id, "file_size": size, "mime_type": "audio/ogg",
                                                    "duration": 3}}}


class Resp:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code, self._data, self.content = status, data, content

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class Groq:
    def __init__(self, resp=None, boom=None):
        self.resp, self.boom, self.posts = resp or Resp(200, {"text": " add a reminder at five "}), boom, []

    def post(self, url, **kw):
        self.posts.append((url, kw))
        if self.boom:
            raise self.boom
        return self.resp


def notes(hear=None, groq_key=lambda: GROQ_KEY, session=None, synth=None, ffmpeg=lambda: None):
    return VoiceNotes(hear, groq_key, lambda: "en-US-AndrewNeural", synth=synth or (lambda t, v: b"ID3mp3"),
                      ffmpeg=ffmpeg, session=session or Groq())


class FakeVoice:
    def __init__(self, heard="what's on my planner", fail=None):
        self.heard, self.fail, self.heard_audio, self.said = heard, fail, [], []

    def hear(self, audio, mime="audio/ogg"):
        self.heard_audio.append((audio, mime))
        if self.fail:
            raise self.fail
        return self.heard

    def speak(self, text):
        self.said.append(text)
        return b"OggS reply", "audio/ogg"


# ---- what gets said ------------------------------------------------------------------------------------------
def test_replies_are_spoken_without_markdown_links_or_code():
    said = speakable("## Plan\n- **Buy** [the house](https://x.com/a)\n- see https://y.com now\n```py\nprint(1)\n```\nDone.")
    assert said == "Plan Buy the house see the link in the message now (The code is in the message.) Done."
    long = ("This is one sentence. " * 200).strip()
    cut = speakable(long, limit=300)
    assert len(cut) < 340 and cut.endswith(". The rest is in the message.")
    assert speakable("") == ""


# ---- hearing -------------------------------------------------------------------------------------------------
def test_gemini_hears_the_note_first():
    seen = []

    def hear(system, parts):
        seen.append((system, parts))
        return '  "Add 8000 to the game."  '

    groq = Groq()
    assert notes(hear, session=groq).hear(NOTE, "audio/ogg; codecs=opus") == "Add 8000 to the game."
    system, parts = seen[0]
    assert system == HEAR_PROMPT and parts[0]["inlineData"]["mimeType"] == "audio/ogg"
    assert base64.b64decode(parts[0]["inlineData"]["data"]) == NOTE and groq.posts == []


def test_groq_whisper_hears_it_when_gemini_is_out():
    def hear(system, parts):
        raise RuntimeError("quota")

    groq = Groq()
    assert notes(hear, session=groq).hear(NOTE) == "add a reminder at five"
    url, kw = groq.posts[0]
    assert url == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert kw["headers"]["Authorization"] == f"Bearer {GROQ_KEY}"
    assert kw["data"]["model"] == WHISPER_MODEL and kw["files"]["file"] == ("voice.ogg", NOTE, "audio/ogg")


def test_hearing_failures_are_plain_and_never_show_a_key():
    with pytest.raises(HearingError, match="no Groq key"):
        notes(None, groq_key=lambda: None).hear(NOTE)
    for groq in (Groq(Resp(401, {"error": GROQ_KEY})), Groq(boom=requests.ConnectionError(GROQ_KEY)),
                 Groq(Resp(200, None))):
        with pytest.raises(HearingError) as caught:
            notes(None, session=groq).hear(NOTE)
        assert GROQ_KEY not in str(caught.value)
    with pytest.raises(HearingError, match="empty"):
        notes().hear(b"")
    with pytest.raises(HearingError, match="too big"):
        notes().hear(b"x" * (MAX_NOTE_BYTES + 1))


# ---- speaking ------------------------------------------------------------------------------------------------
def test_reply_becomes_an_opus_voice_note():
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        pytest.skip("no FFmpeg here")
    mp3 = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                          "-c:a", "libmp3lame", "-f", "mp3", "pipe:1"], capture_output=True, check=True).stdout
    asked = []
    audio, mime = notes(synth=lambda t, v: asked.append((t, v)) or mp3, ffmpeg=ffmpeg_path).speak("**Hi** Justin.")
    assert mime == "audio/ogg" and audio.startswith(b"OggS") and b"OpusHead" in audio[:200]
    assert asked == [("Hi Justin.", "en-US-AndrewNeural")]


def test_phone_replies_skip_opus_so_every_phone_can_play_them():
    ran = []
    assert notes(ffmpeg=lambda: ran.append(1) or "/usr/bin/ffmpeg").speak("Hello.", opus=False) == (b"ID3mp3",
                                                                                                    "audio/mpeg")
    assert ran == []  # FFmpeg isn't even looked up


def test_without_ffmpeg_the_reply_goes_as_mp3():
    assert notes(ffmpeg=lambda: None).speak("Hello.") == (b"ID3mp3", "audio/mpeg")
    assert notes(ffmpeg=lambda: "/no/such/ffmpeg").speak("Hello.") == (b"ID3mp3", "audio/mpeg")
    with pytest.raises(ValueError):
        notes().speak("   ")


# ---- the Bot API calls ---------------------------------------------------------------------------------------
def test_api_downloads_notes_and_sends_voice_without_leaking_the_key():
    calls = []

    class Session:
        def post(self, url, json=None, data=None, files=None, timeout=None):
            calls.append((url.rsplit("/", 1)[1], json, data, files))
            return Resp(200, {"ok": True, "result": {"file_path": "voice/file_7.oga"} if url.endswith("getFile")
                              else {"message_id": 4}})

        def get(self, url, timeout=None):
            calls.append(("GET", url))
            return Resp(200, content=NOTE)

    api = TelegramAPI(KEY, session=Session())
    assert api.download("abc") == NOTE
    assert calls[0][:2] == ("getFile", {"file_id": "abc"})
    assert calls[1] == ("GET", f"https://api.telegram.org/file/bot{KEY}/voice/file_7.oga")
    api.send_voice(OWNER_ID, b"OggS", "audio/ogg")
    assert calls[2] == ("sendVoice", None, {"chat_id": str(OWNER_ID)}, {"voice": ("jarvis.ogg", b"OggS", "audio/ogg")})

    class Down(Session):
        def get(self, url, timeout=None):
            raise requests.ConnectionError(url)

    with pytest.raises(TelegramError) as caught:
        TelegramAPI(KEY, session=Down()).download("abc")
    assert KEY not in str(caught.value) and KEY not in caught.value.description

    class Huge(Session):
        def get(self, url, timeout=None):
            return Resp(200, content=b"x" * 11)

    with pytest.raises(TelegramError, match="too big"):
        TelegramAPI(KEY, session=Huge()).download("abc", limit=10)


# ---- the bridge ----------------------------------------------------------------------------------------------
def test_a_voice_note_is_heard_answered_and_spoken_back(library):
    voice, assistant = FakeVoice(), FakeAssistant(reply="You have 2 reminders today.")
    b, api, _ = paired(library, assistant=assistant, voice=voice)
    api.files["f1"] = NOTE
    b.handle(api, voice_msg())
    assert voice.heard_audio == [(NOTE, "audio/ogg")]
    assert assistant.calls == [("what's on my planner", None)]
    assert api.texts() == ["You said: what's on my planner", "You have 2 reminders today."]
    assert voice.said == ["You have 2 reminders today."]
    assert api.voices == [{"chat_id": OWNER_ID, "audio": b"OggS reply", "mime": "audio/ogg"}]
    assert "record_voice" in api.actions


def test_typed_messages_get_no_voice_reply(library):
    voice = FakeVoice()
    b, api, _ = paired(library, voice=voice)
    from test_telegram import msg
    b.handle(api, msg("hello"))
    assert voice.said == [] and api.voices == []


def test_strangers_voice_notes_are_never_downloaded(library):
    voice = FakeVoice()
    b, api, _ = paired(library, voice=voice)
    api.files["f1"] = NOTE
    b.handle(api, voice_msg(chat=STRANGER))
    assert api.downloads == [] and voice.heard_audio == [] and api.texts() == ["This Jarvis is private."]


def test_voice_notes_that_cannot_be_heard_get_a_plain_answer(library):
    assistant = FakeAssistant()
    b, api, _ = paired(library, assistant=assistant, voice=FakeVoice(fail=HearingError("there is no Groq key")))
    api.files["f1"] = NOTE
    b.handle(api, voice_msg())
    assert api.texts() == ["I couldn't hear that voice note (there is no Groq key). Try again, or type it."]
    b.handle(api, voice_msg(file_id="missing", uid=3))
    assert "file not found" in api.texts()[-1]
    b.voice = FakeVoice(heard="")
    b.handle(api, voice_msg(uid=4))
    assert api.texts()[-1] == "I didn't hear any words in that voice note."
    b.handle(api, voice_msg(size=MAX_NOTE_BYTES + 1, uid=5))
    assert "too big" in api.texts()[-1]
    b.voice = None
    b.handle(api, voice_msg(uid=6))
    assert api.texts()[-1] == "I can't listen to voice notes right now. Please type it."
    assert assistant.calls == [] and api.voices == []


def test_old_voice_notes_are_not_acted_on(library):
    voice = FakeVoice()
    b, api, _ = paired(library, voice=voice, clock=Clock(1_000_000 + 7200))
    api.files["f1"] = NOTE
    b.handle(api, voice_msg())
    assert api.texts() == ["I was closed when you sent a voice note. Send it again if you still want it."]
    assert api.downloads == []


def test_a_failed_spoken_reply_keeps_the_text_answer(library):
    class Mute(FakeVoice):
        def speak(self, text):
            raise RuntimeError("edge is down")

    b, api, _ = paired(library, assistant=FakeAssistant(reply="Done."), voice=Mute())
    api.files["f1"] = NOTE
    assert b.handle(api, voice_msg()) is None
    assert api.texts()[-1] == "Done." and api.voices == []


def test_studio_hears_with_gemini_and_speaks_with_the_settings_voice(tmp_path, monkeypatch):
    for var in ("JARVIS_TELEGRAM_KEY", "TELEGRAM_BOT_TOKEN", "GROQ_API_KEY", "JARVIS_GROQ_KEY"):
        monkeypatch.delenv(var, raising=False)

    def responder(model, payload):
        parts = payload["contents"][-1]["parts"]
        if any("inlineData" in p for p in parts):
            return body("Read me my reminders.")
        return body("No reminders today.")

    s = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(responder),
               engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None}, local_client=FakeOllama())
    s.start(probe=False)
    try:
        spoken = []
        s.telegram.voice.synth = lambda text, voice: spoken.append((text, voice)) or b"ID3mp3"
        s.telegram.voice.ffmpeg = lambda: None
        s.set_setting("edge_voice", "en-GB-RyanNeural")
        api = FakeTelegram()
        api.files["f1"] = NOTE
        s.telegram.handle(api, {"update_id": 1, "message": {"message_id": 1, "date": time.time(),
                                                           "chat": {"id": OWNER_ID, "type": "private"},
                                                           "from": {"id": OWNER_ID}, "text": s.telegram.pair_code()}})
        s.telegram.handle(api, voice_msg(date=time.time()))
        assert api.texts()[-2:] == ["You said: Read me my reminders.", "No reminders today."]
        assert spoken == [("No reminders today.", "en-GB-RyanNeural")]
        assert api.voices[0]["mime"] == "audio/mpeg"
    finally:
        s.close()
