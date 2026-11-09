"""Multi-voice narration, stage 1: who says each line, and a voice cast with a narrator per book."""
import json

from jarvis_books.audio.script import VoiceDirector, merge_lines, split_spans
from jarvis_books.audio.voices import (GEMINI_CATALOG, NARRATOR, assign_voices, default_style, parse_edge_voice,
                                       who)
from jarvis_books.llm.errors import LLMError
from fakes import FakeGemini, body, sample_for

SETTINGS = {"voice": "Charon", "edge_voice": "en-US-AndrewNeural"}
PASSAGE = ('Lily pressed her face to the glass. \u201cIs he coming back?\u201d she whispered.\n\n'
           '"Not tonight," said Captain Reyes. "Go to bed, little one."\n\nThe ship creaked.')


def responder(labels=None, profiles=None, seen=None):
    def respond(model_id, payload):
        schema = (payload.get("generationConfig") or {}).get("responseSchema") or {}
        props = schema.get("properties", {})
        if seen is not None:
            seen.append(json.dumps(payload))
        if "quotes" in props:
            return body(json.dumps(labels if labels is not None else {"quotes": []}))
        if "characters" in props and props["characters"].get("type") == "ARRAY" and profiles is not None:
            return body(json.dumps(profiles))
        return FakeGemini.default(model_id, payload) if not schema else body(json.dumps(sample_for(schema)))
    return respond


def make(library, make_engine, labels=None, profiles=None, seen=None):
    engine = make_engine(FakeGemini(responder(labels, profiles, seen)))
    book = library.create_book("The Salt Road", "fantasy", "a captain and a stowaway girl")
    library.upsert_bible(book.id, "character", "Lily", "a seven-year-old stowaway")
    library.upsert_bible(book.id, "character", "Captain Reyes", "the weary old captain of the Gull")
    return VoiceDirector(library, engine, lambda: dict(SETTINGS)), book, engine


PROFILES = {"characters": [
    {"name": "Lily", "aliases": ["little one"], "gender": "female", "age": "child", "description": "small, curious"},
    {"name": "Captain Reyes", "aliases": ["Reyes", "the captain"], "gender": "male", "age": "elder",
     "description": "gruff, weary"},
    {"name": "Invented Person", "gender": "male", "age": "adult"}]}


def test_spans_rebuild_the_passage_exactly():
    for text in (PASSAGE, "No dialogue at all.", '"Only a quote."', 'He said "unclosed\n\nNext.', ""):
        assert "".join(t for _, t in split_spans(text)) == text
    kinds = [k for k, _ in split_spans(PASSAGE)]
    assert kinds.count("quote") == 3


def test_merge_lines_gives_narration_to_the_narrator_and_merges_same_voice():
    lines = merge_lines(split_spans(PASSAGE), {1: ("Lily", "whispering"), 2: ("Captain Reyes", ""),
                                               3: ("Captain Reyes", "")})
    assert [ln.speaker for ln in lines] == [NARRATOR, "Lily", NARRATOR, "Captain Reyes", NARRATOR,
                                            "Captain Reyes", NARRATOR]
    assert lines[1].style == "whispering" and lines[-1].text == "The ship creaked."
    words = " ".join(ln.text for ln in lines).split()
    assert words == PASSAGE.split()  # nothing reworded, added or dropped


def test_voices_fit_gender_and_age_and_stay_distinct():
    members = [{"name": "Lily", "gender": "female", "age": "child"},
               {"name": "Tom", "gender": "male", "age": "child"},
               {"name": "Reyes", "gender": "male", "age": "elder"},
               {"name": "Ana", "gender": "female", "age": "adult"},
               {"name": "Ben", "gender": "male", "age": "adult"},
               {"name": "Kept", "gender": "male", "age": "adult", "voice": "Puck", "edge_voice": "x", "locked": True}]
    out = {m["name"]: m for m in assign_voices(members, "Charon", "en-US-AndrewNeural")}
    assert out["Lily"]["edge_voice"] == "en-US-AnaNeural" and "girl" in out["Lily"]["style"]
    assert "+40Hz" in out["Tom"]["edge_voice"] and "boy" in out["Tom"]["style"]
    assert GEMINI_CATALOG[out["Ana"]["voice"]][0] == "female" and GEMINI_CATALOG[out["Ben"]["voice"]][0] == "male"
    assert out["Reyes"]["voice"] in ("Algenib", "Rasalgethi", "Sadaltager", "Schedar")
    assert out["Kept"]["voice"] == "Puck"
    voices = [m["voice"] for m in out.values()]
    assert len(set(voices)) == len(voices) and "Charon" not in voices  # nobody shares the narrator's voice
    assert parse_edge_voice("en-US-BrianNeural|+40Hz|+8%") == ("en-US-BrianNeural", "+40Hz", "+8%")
    assert parse_edge_voice("en-US-AvaNeural") == ("en-US-AvaNeural", "+0Hz", "+0%")
    assert who("female", "child") == "Girl" and default_style("male", "adult") == ""


def test_every_book_has_its_own_narrator(library, make_engine):
    d, book, _ = make(library, make_engine)
    assert d.narrator(book.id)["voice"] == "Charon"
    library.upsert_cast_member(book.id, NARRATOR, voice="Sulafat")
    other = library.create_book("Second")
    assert d.narrator(book.id)["voice"] == "Sulafat" and d.narrator(other.id)["voice"] == "Charon"


def test_build_cast_profiles_bible_characters_only(library, make_engine):
    d, book, _ = make(library, make_engine, profiles=PROFILES)
    ordered = d.build_cast(book.id)
    assert ordered[0]["name"] == NARRATOR
    cast = {m["name"]: m for m in ordered}
    assert set(cast) == {NARRATOR, "Lily", "Captain Reyes"}  # Gemini cannot invent cast members here
    assert cast["Lily"]["age"] == "child" and cast["Lily"]["voice"] and cast["Lily"]["edge_voice"]
    assert "Reyes" in cast["Captain Reyes"]["aliases"]


def test_script_labels_speakers_and_caches(library, make_engine):
    labels = {"quotes": [{"n": 1, "speaker": "Lily", "style": "whispering"},
                         {"n": 2, "speaker": "the captain", "style": "gentle but firm"},
                         {"n": 3, "speaker": "Reyes"}]}
    seen = []
    d, book, _ = make(library, make_engine, labels=labels, profiles=PROFILES, seen=seen)
    d.build_cast(book.id)
    lines = d.script(book.id, PASSAGE)
    assert [ln.speaker for ln in lines if ln.speaker != NARRATOR] == ["Lily", "Captain Reyes", "Captain Reyes"]
    assert lines[1].style == "whispering"
    calls = len(seen)
    assert d.script(book.id, PASSAGE) == lines and len(seen) == calls  # cached: no second Gemini call
    assert d.script(book.id, "The sea was calm. Nobody spoke.")[0].speaker == NARRATOR and len(seen) == calls


def test_unknown_speakers_join_the_cast_and_bad_labels_fall_back_to_narrator(library, make_engine):
    labels = {"quotes": [{"n": 1, "speaker": "Cook", "style": "laughing"}, {"n": 2, "speaker": "Nobody Known"},
                         {"n": 99, "speaker": "Lily"}],
              "new_characters": [{"name": "Cook", "gender": "female", "age": "adult", "description": "loud"}]}
    d, book, _ = make(library, make_engine, labels=labels)
    lines = d.script(book.id, '"Ha!" "Who?"')
    assert [ln.speaker for ln in lines] == ["Cook", NARRATOR]
    cook = library.get_cast_member(book.id, "Cook")
    assert cook and cook["voice"] and GEMINI_CATALOG[cook["voice"]][0] == "female"


def test_gemini_failure_means_narrator_reads_and_retries_later(library, make_engine, monkeypatch):
    d, book, engine = make(library, make_engine)

    def busy(request):
        raise LLMError("busy", kind="unavailable")
    monkeypatch.setattr(engine, "generate", busy)
    lines = d.script(book.id, PASSAGE)
    assert {ln.speaker for ln in lines} == {NARRATOR}
    assert library.get_voice_script(book.id, __import__("jarvis_books.text", fromlist=["x"]).text_hash(PASSAGE)) is None


def test_voice_tools_ask_first_and_undo(library, make_engine, tmp_path):
    from jarvis_books.studio import Studio
    from conftest import hardware
    studio = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(responder(profiles=PROFILES)),
                    engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None})
    try:
        studio.start(probe=False)
        book = studio.library.create_book("The Salt Road", "fantasy")
        studio.library.upsert_bible(book.id, "character", "Lily", "a seven-year-old stowaway")
        studio.library.upsert_bible(book.id, "character", "Captain Reyes", "old captain")
        t = studio.tools
        t.current_book_id = book.id
        assert "Narrator" in json.dumps(t.call("voice_cast"))  # reading needs no yes
        r = t.call("cast_voices")
        assert r["needs_approval"] and studio.library.get_cast_member(book.id, "Lily") is None
        assert studio.gate.answer(True) is not None
        lily = studio.library.get_cast_member(book.id, "Lily")
        assert lily["voice"]
        r = t.call("set_voice", {"character": "the narrator", "voice": "sulafat"})
        assert r["needs_approval"] and "Sulafat" in r["asking"]
        studio.gate.answer(True)
        assert studio.director.narrator(book.id)["voice"] == "Sulafat"
        r = t.call("set_voice", {"character": "Reyes", "age": "child", "gender": "male"})
        studio.gate.answer(True)
        reyes = studio.library.get_cast_member(book.id, "Captain Reyes")
        assert reyes["locked"] and "boy" in reyes["style"] and "+" in reyes["edge_voice"]
        assert t.call("set_voice", {"character": "Nobody"})["error"]
        assert "error" in t.call("set_voice", {"character": "Lily", "voice": "NotAVoice"})
        t.run("undo_last_change")
        assert studio.library.get_cast_member(book.id, "Captain Reyes")["age"] != "child"
        assert {"voice_cast", "set_voice"} <= {d["name"] for d in t.select("make the narrator sound deeper")}
    finally:
        studio.close()
