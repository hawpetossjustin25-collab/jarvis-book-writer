"""Multi-voice narration, stage 1: who says each line, and a voice cast with a narrator per book."""
import json

import pytest
from pathlib import Path

from jarvis_books.audio.script import Line, VoiceDirector, is_plain_tag, merge_lines, split_spans, tag_labels, \
    trim_tags
from jarvis_books.audio.voices import (GEMINI_CATALOG, NARRATOR, another_voice, assign_voices, default_style,
                                       parse_edge_voice, who)
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


def test_plain_speech_tags_are_recognised():
    for tag in ("said Tom.", "she whispered,", "Tom said quietly.", "asked Captain Reyes", ", the captain replied.",
                "he said to her.", "Lily muttered at Tom."):
        assert is_plain_tag(tag), tag
    for not_tag in ("Lily asked, wide-eyed.", "Tom said nothing for a long time.", "The ship creaked.",
                    "He laughed.", "said Tom, slamming the door."):
        assert not is_plain_tag(not_tag), not_tag


TOM_SCENE = "".join(f'"Line {i}," said Tom. ' for i in range(1, 6)) + '"Six." Tom grabbed the rope, said Tom.'


def test_said_tom_is_read_only_three_times_then_voices_carry_it():
    spans = split_spans(TOM_SCENE)
    q = sum(1 for k, _ in spans if k == "quote")
    lines = merge_lines(spans, {i: ("Tom", "") for i in range(1, q + 1)})
    [trimmed] = trim_tags([lines], limit=3)
    tags = [ln.text for ln in trimmed if ln.speaker == NARRATOR]
    assert tags.count("said Tom.") == 3
    assert "Tom grabbed the rope, said Tom." in tags  # tags with action are always read
    spoken = [ln.text for ln in trimmed if ln.speaker == "Tom"]
    assert '"Line 4," "Line 5," "Six."' in spoken  # one speech once the tags between are gone
    [everything] = trim_tags([lines], limit=99)
    assert everything == lines


def test_tag_count_carries_across_passages_but_restarts_each_chapter(library, make_engine):
    d, book, _ = make(library, make_engine)
    counts = {}
    one = [Line("Tom", '"Hi,"'), Line(NARRATOR, "said Tom."), Line("Tom", '"Again."')]
    passages = trim_tags([one, one, one, one], limit=3, counts=counts)
    assert [any(ln.text == "said Tom." for ln in p) for p in passages] == [True, True, True, False]
    assert counts["Tom"] == 4
    d.settings = lambda: {**SETTINGS, "dialogue_tags": "1"}
    assert d.tag_limit() == 1


def test_chapter_lines_drop_repeated_tags_in_audio_only(library, make_engine):
    labels = {"quotes": [{"n": 1, "speaker": "Lily"}, {"n": 2, "speaker": "Lily"}]}
    d, book, _ = make(library, make_engine, labels=labels)
    library.upsert_cast_member(book.id, "Lily", gender="female", age="child")
    passage = '"Hello," said Lily. "Hello again," said Lily.'
    d.settings = lambda: {**SETTINGS, "dialogue_tags": "1"}
    [lines] = d.chapter_lines(book.id, [passage])
    assert [ln.text for ln in lines if ln.speaker == NARRATOR] == ["said Lily."]
    assert passage.count("said Lily") == 2  # the book itself is unchanged


def test_dialogue_tags_setting_values():
    from jarvis_books.assistant.tools import ToolBox
    assert ToolBox._setting_value("dialogue_tags", "all") == "99"
    assert ToolBox._setting_value("dialogue_tags", "3") == "3"
    import pytest
    with pytest.raises(ValueError):
        ToolBox._setting_value("dialogue_tags", "lots")


def test_another_voice_fits_the_character_and_differs_from_the_cast():
    cast = [{"name": NARRATOR, "voice": "Kore", "gender": "unknown", "age": "adult"},
            {"name": "Lily", "voice": "Leda", "gender": "female", "age": "child"},
            {"name": "Mara", "voice": "Achernar", "gender": "female", "age": "adult"},
            {"name": "Reyes", "voice": "Algenib", "gender": "male", "age": "elder"}]
    lily = another_voice(cast[1], cast, "Kore")
    assert GEMINI_CATALOG[lily][0] == "female" and lily not in ("Leda", "Achernar", "Kore")
    reyes = another_voice(cast[3], cast, "Kore")
    assert GEMINI_CATALOG[reyes][0] == "male" and reyes != "Algenib"
    narrator = another_voice(cast[0], cast, "Kore")
    assert narrator not in ("Kore", "Leda", "Achernar", "Algenib")


def test_recast_swap_and_hear_voices_by_command(tmp_path):
    from jarvis_books.studio import Studio
    from conftest import hardware
    client = FakeGemini(responder(profiles=PROFILES))
    studio = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=client,
                    engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None})
    try:
        studio.start(probe=False)
        book = studio.library.create_book("The Salt Road", "fantasy")
        studio.library.upsert_bible(book.id, "character", "Lily", "a seven-year-old stowaway")
        studio.library.upsert_bible(book.id, "character", "Captain Reyes", "old captain")
        t, calls = studio.tools, []
        t.current_book_id = book.id
        t.audio_handler = lambda action, args: calls.append((action, args)) or {"ok": True, "playing_sample": True}
        t.call("cast_voices")
        studio.gate.answer(True)
        assert ("recast", {"book_id": book.id}) in calls
        lily = studio.library.get_cast_member(book.id, "Lily")["voice"]
        reyes = studio.library.get_cast_member(book.id, "Captain Reyes")["voice"]

        r = t.call("set_voice", {"character": "lily", "voice": "different"})
        assert r["needs_approval"] and "different" not in r["asking"]
        studio.gate.answer(True)
        new = studio.library.get_cast_member(book.id, "Lily")
        assert new["voice"] != lily and new["voice"] in r["asking"] and new["locked"]
        assert GEMINI_CATALOG[new["voice"]][0] == "female"

        calls.clear()
        r = t.call("swap_voices", {"first": "Reyes", "second": "Lily"})
        assert r["needs_approval"] and "Captain Reyes" in r["asking"] and "Lily" in r["asking"]
        studio.gate.answer(True)
        assert studio.library.get_cast_member(book.id, "Captain Reyes")["voice"] == new["voice"]
        assert studio.library.get_cast_member(book.id, "Lily")["voice"] == reyes
        assert calls == [("recast", {"book_id": book.id})]
        t.run("undo_last_change")
        assert studio.library.get_cast_member(book.id, "Lily")["voice"] == new["voice"]
        assert "error" in t.call("swap_voices", {"first": "Lily", "second": "Lily"})
        assert "error" in t.call("swap_voices", {"first": "Lily", "second": "Nobody"})

        calls.clear()
        before = client.tts_calls
        r = t.call("hear_voice", {"character": "the captain"})
        assert r["playing_sample"] and r["character"] == "Captain Reyes" and "needs_approval" not in r
        action, args = calls[0]
        assert action == "preview" and Path(args["path"]).exists()
        assert client.tts_log[-1][1] == studio.library.get_cast_member(book.id, "Captain Reyes")["voice"]
        assert "Captain Reyes" in client.tts_log[-1][0]
        t.call("hear_voice", {"character": "Reyes"})
        assert client.tts_calls == before + 1  # the sample is recorded once
        assert "error" in t.call("hear_voice", {"character": "Nobody"})

        assert "swap_voices" in {d["name"] for d in t.select("swap the voices of Lily and Reyes")}
        assert "hear_voice" in {d["name"] for d in t.select("let me hear how Lily sounds")}
    finally:
        studio.close()


def test_spoken_names_with_the_find_the_character(library, make_engine):
    director, book, _ = make(library, make_engine)
    library.upsert_cast_member(book.id, "Captain Reyes", gender="male", age="elder")
    library.upsert_cast_member(book.id, "Lily", gender="female", age="child")
    assert director._resolve(book.id, "the captain") == "Captain Reyes"
    assert director._resolve(book.id, "little Lily") == "Lily"
    assert director._resolve(book.id, "the cook") is None


def test_speech_tags_name_the_speaker_when_the_ai_labeller_cannot():
    text = ('A small sound came from the crate. "Who is in there?" he growled. The lid lifted. "I am Lily," she '
            'said. "I want to see the sea." Reyes frowned and said, "The sea is no place for a child." "Please?"')
    names = {"he": "Captain Reyes", "she": "Lily", "reyes": "Captain Reyes"}
    labels = tag_labels(split_spans(text), lambda who: names.get(who.lower()))
    assert labels == [{"n": 1, "speaker": "Captain Reyes", "style": ""}, {"n": 2, "speaker": "Lily", "style": ""},
                      {"n": 3, "speaker": "Lily", "style": ""}, {"n": 4, "speaker": "Captain Reyes", "style": ""}]


@pytest.mark.parametrize("kind,kept", [("safety", True), ("network", False)])
def test_a_refused_passage_is_voiced_from_its_tags_and_kept(library, make_engine, kind, kept):
    d, book, engine = make(library, make_engine, profiles=PROFILES)
    d.build_cast(book.id)
    text = '"Who is in there?" he growled. "I am Lily," she said.'

    def refuse(request):
        raise LLMError("response blocked (PROHIBITED_CONTENT)", kind=kind)
    d.engine = type("Refuses", (), {"generate": staticmethod(refuse)})()
    lines = d.script(book.id, text)
    assert [ln.speaker for ln in lines if ln.speaker != NARRATOR] == ["Captain Reyes", "Lily"]
    assert (d.script(book.id, text, offline=True) is not None) is kept
