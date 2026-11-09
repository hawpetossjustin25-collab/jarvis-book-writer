import json

import pytest

from fakes import FakeGemini, body
from jarvis_books.errors import ValidationError
from jarvis_books.studio import Studio


def make_studio(tmp_path, responder=None):
    from conftest import hardware
    studio = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(responder),
                    engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None})
    studio.start(probe=False)
    return studio


@pytest.fixture
def studio(tmp_path):
    s = make_studio(tmp_path)
    yield s
    s.close()


@pytest.fixture
def book(studio):
    lib = studio.library
    b = lib.create_book("The Iron Gale", "fantasy", "A storm-witch hunts the ship that killed her brother.")
    one = lib.add_chapter(b.id, title="Landfall", outline="Mara reaches the port.")
    lib.save_chapter_text(one.id, "Rain fell on the docks. Mara watched the ship.")
    lib.add_chapter(b.id, title="The Vault", outline="Mara breaks into the vault.")
    lib.upsert_bible(b.id, "character", "Mara", "A storm-witch, grieving and reckless.")
    return b


def test_briefing_separates_written_from_planned(studio, book):
    out = studio.warroom.briefing(book.id)
    assert out["briefing"]
    assert [c["chapter"] for c in out["written"]] == [1]
    assert [c["chapter"] for c in out["planned"]] == [2]


def test_proposal_is_not_canon_until_accepted_and_undo_restores(studio, book):
    lib, wr = studio.library, studio.warroom
    p = wr.propose(book.id, "bible", "Mara's secret", "A storm-witch who caused her brother's death.",
                   target="character:mara", rationale="Raises the stakes")
    assert p.status == "proposed"
    assert lib.list_bible(book.id, "character")[0].summary == "A storm-witch, grieving and reckless."
    wr.accept(p.id)
    assert lib.list_bible(book.id, "character")[0].summary == "A storm-witch who caused her brother's death."
    wr.undo(p.id)
    assert lib.list_bible(book.id, "character")[0].summary == "A storm-witch, grieving and reckless."
    assert lib.get_proposal(p.id).status == "undone"
    with pytest.raises(ValidationError):
        wr.accept(p.id)


def test_new_bible_entry_undo_removes_it(studio, book):
    wr, lib = studio.warroom, studio.library
    p = wr.propose(book.id, "bible", "The harbormaster", "Old Tobin, who sold the ship's route.",
                   target="character:Tobin")
    wr.accept(p.id, content="Old Tobin, who secretly sold the ship's route.")
    summaries = {e.name: e.summary for e in lib.list_bible(book.id, "character")}
    assert summaries["Tobin"].startswith("Old Tobin, who secretly")
    wr.undo(p.id)
    assert "Tobin" not in {e.name for e in lib.list_bible(book.id, "character")}


def test_outline_proposals_edit_and_add_chapters_reversibly(studio, book):
    wr, lib = studio.warroom, studio.library
    edit = wr.propose(book.id, "outline", "Vault twist", "Mara finds her brother alive in the vault.", target="2")
    added = wr.propose(book.id, "outline", "Aftermath", "The crew mutinies.", target="0")
    wr.accept(edit.id)
    wr.accept(added.id)
    assert lib.get_chapter(book.id, 2).outline == "Mara finds her brother alive in the vault."
    assert lib.get_chapter(book.id, 3).title == "Aftermath"
    wr.undo(edit.id)
    wr.undo(added.id)
    assert lib.get_chapter(book.id, 2).outline == "Mara breaks into the vault."
    assert len(lib.list_chapters(book.id)) == 2


def test_rejected_proposal_changes_nothing(studio, book):
    wr, lib = studio.warroom, studio.library
    p = wr.propose(book.id, "outline", "Cut the vault", "Skip the vault entirely.", target="2")
    wr.reject(p.id)
    assert lib.get_chapter(book.id, 2).outline == "Mara breaks into the vault."
    assert lib.get_proposal(p.id).status == "rejected"


def test_bad_targets_are_refused(studio, book):
    with pytest.raises(ValidationError):
        studio.warroom.propose(book.id, "bible", "x", "y", target="Mara")
    with pytest.raises(ValidationError):
        studio.warroom.propose(book.id, "outline", "x", "y", target="two")


def test_gaps_and_recap_are_saved_to_the_session(studio, book):
    wr, lib = studio.warroom, studio.library
    assert wr.find_gaps(book.id)[0]["options"]
    session = lib.start_war_session(book.id)
    p = wr.propose(book.id, "note", "Theme", "Grief turns to storms.", session_id=session.id)
    wr.accept(p.id)
    ended = wr.recap(session.id, "user: what is the theme?\njarvis: grief.")
    assert ended.ended_at and ended.summary and ended.next_steps
    assert wr.facts(book.id)["last_session"]["summary"] == ended.summary
    assert any("Grief turns to storms" in n.text for n in lib.list_notes(book.id))


def test_war_room_chat_proposes_through_tools_without_touching_canon(tmp_path):
    def responder(model_id, payload):
        contents = payload.get("contents", [])
        answered = any("functionResponse" in part for c in contents for part in c.get("parts", []))
        if payload.get("tools") and not answered:
            return body(calls=[{"name": "propose_idea", "args": {
                "kind": "bible", "title": "New rival", "content": "Captain Vey hunts Mara.",
                "target": "character:Vey"}}])
        schema = (payload.get("generationConfig") or {}).get("responseSchema")
        return body(json.dumps({}) if schema else "I pitched Captain Vey. Accept it if you like it.")

    studio = make_studio(tmp_path, responder)
    try:
        b = studio.library.create_book("The Iron Gale", "fantasy", "A storm-witch hunts a ship.")
        wt = studio.war_tools
        wt.current_book_id = b.id
        wt.war_session_id = studio.library.start_war_session(b.id).id
        assert {"war_briefing", "propose_idea", "decide_proposal"} <= {d["name"] for d in wt.select("hello")}
        reply = studio.war_assistant.send("Give Mara a rival.")
        assert "Vey" in reply
        [p] = studio.library.list_proposals(b.id)
        assert p.status == "proposed" and p.session_id == wt.war_session_id
        assert studio.library.list_bible(b.id, "character") == []
        assert [m.role for m in studio.library.recent_messages("warroom")] == ["user", "assistant"]
        assert studio.library.recent_messages("chat") == []
        assert wt.call("decide_proposal", {"proposal_id": p.id, "decision": "accept"})["status"] == "accepted"
        assert studio.library.list_bible(b.id, "character")[0].name == "Vey"
    finally:
        studio.close()
