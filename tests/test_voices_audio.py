"""Multi-voice narration, stage 2: the audiobook speaks with the narrator and each character's own voice."""
import base64
import io
import json
import wave

import pytest

from fakes import FakeGemini, body
from jarvis_books.audio.narrator import Narrator, chunk_lines, join_wavs, speech_text
from jarvis_books.audio.script import Line, VoiceDirector
from jarvis_books.audio.voices import NARRATOR
from jarvis_books.llm.errors import LLMError
from jarvis_books.llm.gemini import GeminiClient, pcm_to_wav

SETTINGS = {"voice": "Charon", "edge_voice": "en-US-AndrewNeural", "dialogue_tags": "3"}
DIALOGUE = 'Lily looked up. "Is he back?" she whispered.'


def labeller(labels):
    def respond(model_id, payload):
        props = ((payload.get("generationConfig") or {}).get("responseSchema") or {}).get("properties", {})
        if "quotes" in props:
            return body(json.dumps(labels))
        return FakeGemini.default(model_id, payload)
    return respond


@pytest.fixture
def setup(library, make_engine, tmp_path):
    def build(text=DIALOGUE, labels=None, cast=(("Lily", "female", "child", "Leda", "en-US-AnaNeural"),),
              edge=None):
        fake = FakeGemini(labeller(labels or {"quotes": [{"n": 1, "speaker": "Lily", "style": "whispering"}]}))
        engine = make_engine(fake)
        book = library.create_book("The Salt Road", "fantasy")
        library.set_outline(book.id, [{"number": 1, "title": "Stowaway", "outline": "x"}])
        library.save_chapter_text(library.get_chapter(book.id, 1).id, text)
        director = VoiceDirector(library, engine, lambda: dict(SETTINGS))
        for name, gender, age, voice, edge_voice in cast:
            library.upsert_cast_member(book.id, name, gender=gender, age=age, voice=voice, edge_voice=edge_voice,
                                       style="a girl about eight years old" if age == "child" else "")
        narrator = Narrator(library, engine, tmp_path / "audio", voice="Charon",
                            edge_synth=edge or (lambda t, v: b"ID3" + v.encode()))
        narrator.director = director
        return narrator, book, fake
    return build


def test_dialogue_passage_uses_narrator_and_character_in_one_request(setup):
    narrator, book, fake = setup()
    heading, passage = narrator.book_segments(book.id)
    narrator.ensure(passage)
    [(transcript, speakers, direction)] = fake.multi_log
    assert speakers == [("Narrator", "Charon"), ("Lily", "Leda")]
    assert transcript.splitlines() == ["Narrator: Lily looked up.", "Lily: [a girl about eight years old, whispering] Is he back?",
                                       "Narrator: she whispered."]
    assert "girl about eight" in direction
    narrator.ensure(heading)
    assert fake.tts_log[-1][1] == "Charon"  # the chapter title is the narrator's alone


def test_cached_audio_and_recast_rerecords_only_that_characters_passages(setup):
    narrator, book, fake = setup()
    segs = narrator.book_segments(book.id)
    for sg in segs:
        narrator.ensure(sg)
    calls = fake.tts_calls
    assert all(narrator.cached_path(sg) for sg in segs) and fake.tts_calls == calls
    narrator.library.upsert_cast_member(book.id, "Lily", voice="Kore")
    assert narrator.cached_path(segs[0]) is not None  # narrator-only title untouched
    assert narrator.cached_path(segs[1]) is None
    narrator.ensure(segs[1])
    assert fake.multi_log[-1][1][1] == ("Lily", "Kore")


def test_player_cache_lookup_never_calls_gemini(setup):
    narrator, book, fake = setup()
    passage = narrator.book_segments(book.id)[1]
    before = len(fake.calls)
    assert narrator.cached_path(passage) is None and len(fake.calls) == before


def test_three_voices_split_into_two_voice_requests(setup):
    text = '"Bed," said Reyes. "No!" Lily cried. "Supper first," Marta said.'
    labels = {"quotes": [{"n": 1, "speaker": "Reyes"}, {"n": 2, "speaker": "Lily"}, {"n": 3, "speaker": "Marta"}]}
    cast = (("Lily", "female", "child", "Leda", "en-US-AnaNeural"),
            ("Reyes", "male", "elder", "Algenib", "en-US-ChristopherNeural|-6Hz|-8%"),
            ("Marta", "female", "adult", "Kore", "en-US-AvaNeural"))
    narrator, book, fake = setup(text, labels, cast)
    path = narrator.ensure(narrator.book_segments(book.id)[1])
    assert len(fake.multi_log) >= 2
    assert all(len(speakers) == 2 for _, speakers, _ in fake.multi_log)
    voices = {v for _, speakers, _ in fake.multi_log for _, v in speakers}
    assert {"Charon", "Algenib", "Leda", "Kore"} <= voices
    with wave.open(str(path)) as w:
        assert w.getnframes() > 4800 * 2  # the pieces were joined into one passage


def test_edge_backup_keeps_each_character_voice(setup, monkeypatch):
    seen = []
    narrator, book, fake = setup(edge=lambda t, v: seen.append((t, v)) or b"ID3x")

    def no_quota(*a, **k):
        raise LLMError("quota", kind="quota")
    monkeypatch.setattr(narrator.engine, "tts_multi", no_quota)
    monkeypatch.setattr(narrator.engine, "tts", no_quota)
    path = narrator.ensure(narrator.book_segments(book.id)[1])
    assert path.suffix == ".mp3" and narrator.last_backend == "edge"
    assert seen == [("Lily looked up.", "en-US-AndrewNeural"), ("Is he back?", "en-US-AnaNeural"),
                    ("she whispered.", "en-US-AndrewNeural")]


def test_book_narrator_voice_is_used_without_characters(setup):
    narrator, book, fake = setup(cast=())
    narrator.library.upsert_cast_member(book.id, NARRATOR, voice="Sulafat", edge_voice="en-US-AvaNeural")
    narrator.ensure(narrator.book_segments(book.id)[1])
    assert fake.tts_log[-1][1] == "Sulafat" and not fake.multi_log


def test_repeated_said_tags_are_left_out_of_the_audio(setup):
    text = " ".join(f'"Step {i}," said Lily.' for i in range(1, 6))
    labels = {"quotes": [{"n": i, "speaker": "Lily"} for i in range(1, 6)]}
    narrator, book, fake = setup(text, labels)
    narrator.ensure(narrator.book_segments(book.id)[1])
    spoken = "\n".join(t for t, _, _ in fake.multi_log)
    assert spoken.count("said Lily") == 3
    assert narrator.library.get_chapter(book.id, 1).text == text


def test_helpers():
    a, b = pcm_to_wav(b"\x01\x00" * 100), pcm_to_wav(b"\x02\x00" * 50)
    with wave.open(io.BytesIO(join_wavs([a, b], gap_ms=10))) as w:
        assert w.getnframes() == 150 + 240
    with pytest.raises(LLMError):
        join_wavs([a, pcm_to_wav(b"\x00\x00", rate=16000)])
    lines = [Line(NARRATOR, "a"), Line("A", '"b"'), Line(NARRATOR, "c"), Line("B", '"d"'), Line("A", '"e"')]
    assert [len(c) for c in chunk_lines(lines)] == [3, 2]
    assert speech_text(Line("A", '\u201cHi,\u201d "there."')) == "Hi, there."


def test_gemini_multi_speaker_request_format(monkeypatch):
    client = GeminiClient("test-key")
    sent = []
    audio = base64.b64encode(b"\x00\x00" * 10).decode()

    def generate(model_id, payload, timeout=None):
        sent.append(payload)
        return {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16;rate=24000",
                                                                      "data": audio}}]}}]}
    monkeypatch.setattr(client, "generate", generate)
    wav = client.tts_multi("m", "Narrator: hi\nLily: yo", [("Narrator", "Charon"), ("Lily", "Leda")], "Read it.")
    assert wav[:4] == b"RIFF"
    speech = sent[0]["generationConfig"]["speechConfig"]["multiSpeakerVoiceConfig"]["speakerVoiceConfigs"]
    assert speech == [{"speaker": "Narrator", "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Charon"}}},
                      {"speaker": "Lily", "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Leda"}}}]
    assert sent[0]["contents"][0]["parts"][0]["text"].startswith("Read it.")
    with pytest.raises(ValueError):
        client.tts_multi("m", "x", [("A", "Kore")])


def test_newer_speech_models_get_one_part_per_speaker(monkeypatch):
    client = GeminiClient("test-key")
    sent = []
    audio = base64.b64encode(pcm_to_wav(b"\x00\x00" * 10)).decode()

    def generate(model_id, payload, timeout=None):
        sent.append(payload)
        if "speech_metadata" not in payload["contents"][0]["parts"][0]:
            raise LLMError("Multi-speaker generation requests must specify speech_metadata.speaker for each "
                           "text part in the contents.", kind="invalid")
        return {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/wav", "data": audio}}]}}]}
    monkeypatch.setattr(client, "generate", generate)
    speakers = [("Narrator", "Charon"), ("Lily", "Leda")]
    client.tts_multi("new", "Narrator: She looked up.\nLily: [whispering] Back?", speakers, "Read it.")
    assert sent[-1]["contents"][0]["parts"] == [
        {"text": "She looked up.", "speech_metadata": {"speaker": "Narrator"}},
        {"text": "[whispering] Back?", "speech_metadata": {"speaker": "Lily"}}]
    client.tts_multi("new", "Lily: Hi.\nNarrator: she said.", speakers)
    assert len(sent) == 3  # the model's format is remembered, so no second failed attempt


def test_speech_routing_moves_on_when_a_model_rejects_the_request(make_engine):
    def respond(model_id, payload):
        return FakeGemini.default(model_id, payload)
    fake = FakeGemini(respond)
    engine = make_engine(fake)
    tried = []

    def picky(model_id, transcript, speakers, direction=""):
        tried.append(model_id)
        if len(tried) == 1:
            raise LLMError("bad request for this model", kind="invalid")
        return pcm_to_wav(b"\x00\x00" * 10)
    fake.tts_multi = picky
    engine.client = fake
    routes = engine.get_runtime_configuration().routes
    routes["tts"] = (list(routes["tts"]) + ["gemini-2.5-flash-preview-tts"])[:2]
    assert engine.tts_multi("A: x\nB: y", [("A", "Kore"), ("B", "Puck")])[:4] == b"RIFF"
    assert len(tried) == len(set(tried)) == 2


def test_stray_punctuation_is_not_sent_and_backup_errors_are_reported(setup, monkeypatch):
    seen = []

    def edge(text, voice):
        seen.append(text)
        if voice == "en-US-AnaNeural":
            raise RuntimeError("No audio was received")
        return b"ID3x"
    narrator, book, fake = setup('"Is he back?", "Yes."', {"quotes": [{"n": 1, "speaker": "Lily"},
                                                                     {"n": 2, "speaker": "Lily"}]}, edge=edge)

    def no_quota(*a, **k):
        raise LLMError("quota", kind="quota")
    monkeypatch.setattr(narrator.engine, "tts_multi", no_quota)
    monkeypatch.setattr(narrator.engine, "tts", no_quota)
    with pytest.raises(LLMError):
        narrator.ensure(narrator.book_segments(book.id)[1])
    assert seen and all(any(c.isalnum() for c in t) for t in seen)


def test_speech_uses_a_free_model_before_waiting_for_a_busy_one(make_engine, monkeypatch):
    fake = FakeGemini()
    engine = make_engine(fake)
    routes = engine.get_runtime_configuration().routes
    routes["tts"] = (list(routes["tts"]) + ["gemini-2.5-flash-preview-tts"])[:2]
    busy, free = routes["tts"]
    waits = []

    def acquire(model, max_wait_s=65.0):
        waits.append((model, max_wait_s))
        return model == free
    monkeypatch.setattr(engine.limiter, "acquire", acquire)
    used = []
    fake.tts = lambda model, *a, **k: used.append(model) or pcm_to_wav(b"\x00\x00")
    engine.tts("hello", "Kore")
    assert used == [free] and waits == [(busy, 0.0), (free, 0.0)]
