import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from fakes import FakeGemini  # noqa: E402
from jarvis_books.studio import Studio  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def pump(app, until, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if until():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def window(qapp, tmp_path):
    from conftest import hardware
    from jarvis_books.ui.main_window import MainWindow
    studio = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(),
                    engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None})
    studio.start(probe=False)
    book = studio.library.create_book("Glass City", "science fiction", "a city of glass")
    studio.library.set_outline(book.id, [{"number": 1, "title": "Shatter", "outline": "the first crack"},
                                         {"number": 2, "title": "Echo", "outline": "aftermath"}])
    studio.library.save_chapter_text(studio.library.get_chapter(book.id, 1).id, "The glass sang at dawn.")
    win = MainWindow(studio)
    win.show()
    pump(qapp, lambda: "Ready" in win.statusBar().currentMessage() or win.engine_view.toPlainText().strip())
    yield win, studio, book
    win.close()


def test_tabs_and_library(window, qapp):
    win, studio, book = window
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == \
        ["Jarvis", "Chat", "Library", "Write", "Listen", "Settings", "War Room", "Publish"]
    assert win.book_list.count() == 1 and win.selected_book_id() == book.id
    assert "Glass City" in win.book_info.toPlainText() and "Shatter" in win.book_info.toPlainText()
    assert "Config v" in win.engine_view.toPlainText()


def test_editor_autosaves_with_revision_history(window, qapp):
    win, studio, book = window
    win.write_book.setCurrentIndex(win.write_book.findData(book.id))
    win._load_chapters()
    assert win.editor.toPlainText() == "The glass sang at dawn."
    win.editor.setPlainText("The glass sang at dawn. Then it broke.")
    win._save_editor()
    ch = studio.library.get_chapter(book.id, 1)
    assert ch.text.endswith("Then it broke.")
    assert len(studio.library.revisions(ch.id)) >= 1  # the earlier version is kept


def test_chat_round_trip(window, qapp):
    win, studio, book = window
    win.chat_input.setText("hello jarvis")
    win._send_chat()
    assert pump(qapp, lambda: win.chat_send.isEnabled(), timeout=10)
    assert "hello jarvis" in win.chat_view.toPlainText()
    assert [m.role for m in studio.library.recent_messages("chat", 5)][-2:] == ["user", "assistant"]


def test_listen_loads_unfinished_book(window, qapp):
    win, studio, book = window
    win.listen_book.setCurrentIndex(win.listen_book.findData(book.id))
    assert win.player.load(book.id) == 2  # heading + one passage of the only written chapter


def test_voice_session_is_built_from_live_model(window, qapp):
    win, studio, book = window
    from jarvis_books.llm.gemini import LIVE_URL, GeminiClient
    real = GeminiClient("test-key")  # the real client's interface, not a stand-in
    studio.client.live_url = real.live_url
    studio.client.live_headers = real.live_headers
    session = win._make_live_session()
    assert session.url == LIVE_URL and session.headers == {"x-goog-api-key": "test-key"}
    assert session.model == studio.engine.live_model()
    assert {d["name"] for d in session.tools} >= {"list_books", "read_chapter", "write_chapters"}


def test_closed_window_stops_timers_and_storage_access(qapp, tmp_path):
    from conftest import hardware
    from jarvis_books.ui.main_window import MainWindow
    studio = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(),
                    engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None})
    studio.start(probe=False)
    win = MainWindow(studio)
    win.show()
    win.editor.setPlainText("unsaved")
    win.close()
    assert not win._refresh_timer.isActive() and not win._autosave.isActive()
    win.refresh_books()  # a late timer tick must not touch the closed database
    win._load_chapters()
    win._on_started(None)  # a late startup callback must not switch on the wake word (real mic on a laptop)
    assert win.voice.wake is None and not win.wake_box.isChecked()


def test_war_room_proposals_accept_and_undo_from_the_panel(window, qapp):
    win, studio, book = window
    assert win._war_book_id() == book.id and studio.war_tools.current_book_id == book.id
    p = studio.warroom.propose(book.id, "bible", "Glass oracle", "An oracle who hears the glass sing.",
                               target="character:Oracle")
    assert pump(qapp, lambda: win.war_props.count() == 1)
    assert "Jarvis's idea" in win.war_props.item(0).text()
    assert studio.library.list_bible(book.id, "character") == []
    win.war_props.setCurrentRow(0)
    assert "hears the glass sing" in win.war_detail.toPlainText()
    win._war_decide("accept")
    assert studio.library.list_bible(book.id, "character")[0].name == "Oracle"
    assert win.war_props.count() == 0  # filtered to "waiting for you"
    win.war_filter.setCurrentIndex(win.war_filter.findData(""))
    win.war_props.setCurrentRow(0)
    win._war_decide("undo")
    assert studio.library.list_bible(book.id, "character") == []
    assert studio.library.get_proposal(p.id).status == "undone"
    assert "accepted the idea" in win.war_view.toPlainText()


def test_war_room_briefing_and_recap_share_the_conversation(window, qapp):
    win, studio, book = window
    win._war_briefing()
    assert pump(qapp, lambda: win.war_state.text() == "Ready.")
    msgs = studio.library.recent_messages("warroom", 10, book_id=book.id)
    assert msgs and msgs[-1].role == "assistant"
    sid = studio.war_tools.war_session_id
    assert sid is not None
    win._war_end()
    assert pump(qapp, lambda: win.war_state.text() == "Session saved.")
    assert studio.library.get_war_session(sid).ended_at and win.war_recap.toPlainText().strip()
    assert studio.war_tools.war_session_id is None


def test_war_room_voice_uses_war_tools_and_shared_history(window, qapp):
    win, studio, book = window
    from jarvis_books.llm.gemini import GeminiClient
    real = GeminiClient("test-key")
    studio.client.live_url = real.live_url
    studio.client.live_headers = real.live_headers
    studio.library.add_message("warroom", "user", "Should the oracle be blind?", book.id)
    session = win._make_war_live_session()
    names = {d["name"] for d in session.tools}
    assert {"war_briefing", "find_gaps", "propose_idea", "decide_proposal"} <= names
    assert not names & {"update_story_bible", "change_outline"}
    assert "Glass City" in session.system and "Should the oracle be blind?" in session.system
    win._war_line = {"user": "make the oracle blind", "jarvis": "Pitched it."}
    win._on_war_transcript("turn", "")
    roles = [(m.role, m.text) for m in studio.library.recent_messages("warroom", 5, book_id=book.id)]
    assert roles[-2:] == [("user", "make the oracle blind"), ("assistant", "Pitched it.")]


def test_publish_tab_validates_and_lists_posts(window, qapp, monkeypatch):
    win, studio, book = window
    from PyQt6.QtWidgets import QMessageBox
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: warnings.append(a[2]))
    assert "client file" in win.pub_account.text() and not win.pub_connect_btn.isEnabled()
    assert win._parse_chapters("1-3, 5") == [1, 2, 3, 5]
    win.pub_book.setCurrentIndex(win.pub_book.findData(book.id))
    win._pub_post()
    assert warnings and "Connect" in warnings[-1]
    studio.library.add_post(book.id, "youtube", "Glass City", "unlisted", [1], {})
    win._pub_refresh_posts()
    assert win.pub_posts.count() == 1 and "unlisted" in win.pub_posts.item(0).text()


class _FakeLive:
    def __init__(self):
        self.sent = []

    def send_audio(self, pcm):
        self.sent.append(pcm)


def test_voice_bridge_drops_the_rest_of_an_answer_after_barge_in(qapp):
    from jarvis_books.ui.voice_bridge import VoiceBridge
    bridge = VoiceBridge(lambda **kw: None)
    bridge._queue_audio(b"\x01\x00" * 100)
    assert bridge._turn_open and bridge._is_playing()
    bridge._barge_in()
    assert not bridge._out and bridge._drop_turn
    bridge._queue_audio(b"\x01\x00" * 100)  # the old answer keeps streaming in: ignored
    assert not bridge._out
    bridge._on_session_state("listening")  # Gemini ended that turn
    bridge._queue_audio(b"\x02\x00" * 10)  # the new answer plays
    assert bytes(bridge._out) == b"\x02\x00" * 10


def test_both_jarvis_voices_follow_the_auto_mute_setting(window):
    win, studio, _ = window
    assert win.voice.auto_mute and win.war_voice.auto_mute
    win.auto_mute_box.setChecked(False)
    assert not win.voice.auto_mute and not win.war_voice.auto_mute
    assert studio.settings()["auto_mute"] == "0"


def test_approval_card_shows_everywhere_and_the_yes_button_runs_the_action(qapp, tmp_path):
    from conftest import hardware
    from jarvis_books.ui.main_window import MainWindow
    studio = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(),
                    engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None})
    studio.start(probe=False)
    win = MainWindow(studio)
    try:
        lib = studio.library
        b = lib.create_book("Glass City", "mystery", "x")
        lib.set_outline(b.id, [{"number": 1, "title": "Shards", "outline": "x"}])
        asked = studio.tools.call("change_outline", {"book": "Glass", "chapter": 1, "title": "Bells"}, source="voice")
        qapp.processEvents()
        assert asked["needs_approval"] and len(win.approval_bars) == 3
        assert all(bar.request_id == asked["request_id"] for bar in win.approval_bars)
        win._hear_voice("user", " yes")  # Jarvis hasn't finished asking yet
        assert lib.get_chapter(b.id, 1).title == "Shards"
        win._hear_voice("turn", "")
        win._hear_voice("user", " yes please")
        qapp.processEvents()
        assert lib.get_chapter(b.id, 1).title == "Bells"
        assert all(bar.request_id is None for bar in win.approval_bars)
        studio.tools.call("change_outline", {"book": "Glass", "chapter": 1, "title": "Towers"})
        qapp.processEvents()
        win.approval_bars[1].yes_btn.click()
        qapp.processEvents()
        assert lib.get_chapter(b.id, 1).title == "Towers"
        assert win.perm_list.count() == 2 and win.perm_list.item(0).text().startswith("Yes")
    finally:
        win.close()


def test_jarvis_controls_the_window_from_a_worker_thread(window, qapp):
    import threading
    win, studio, _book = window
    results = {}

    def jarvis():
        results["tab"] = studio.tools.call("open_tab", {"tab": "war room"}, source="voice")
        results["mute"] = studio.tools.call("app_action", {"action": "mute_mic"}, source="voice")
        results["status"] = studio.tools.call("app_status", {}, source="voice")
    t = threading.Thread(target=jarvis)
    t.start()
    while t.is_alive():
        qapp.processEvents()
    assert results["tab"]["ok"] and win.tabs.tabText(win.tabs.currentIndex()) == "War Room"
    assert win.mute_box.isChecked() and win.war_mute.isChecked()
    assert results["status"]["tab"] == "War Room" and results["status"]["mic_muted"]

    asked = studio.tools.call("change_setting", {"key": "auto_mute", "value": "off"})
    qapp.processEvents()
    assert asked["needs_approval"] and win.auto_mute_box.isChecked()
    win.approval_bars[0].yes_btn.click()
    qapp.processEvents()
    assert not win.auto_mute_box.isChecked() and not win.voice.auto_mute and not win.war_voice.auto_mute
    assert studio.settings()["auto_mute"] == "0"
    studio.tools.call("undo_last_change")
    studio.gate.answer(True)
    qapp.processEvents()
    assert win.auto_mute_box.isChecked() and studio.settings()["auto_mute"] == "1"


def test_listen_tab_cast_table_changes_a_voice(window, qapp):
    win, studio, book = window
    win.listen_book.setCurrentIndex(win.listen_book.findData(book.id))
    win._refresh_cast()
    assert win.cast_table.rowCount() == 1 and win.cast_table.item(0, 0).text() == "Narrator"
    studio.library.upsert_cast_member(book.id, "Mara", gender="female", age="child", voice="Leda",
                                      edge_voice="en-US-AnaNeural")
    win._refresh_cast()
    assert win.cast_table.rowCount() == 2
    assert win.cast_table.cellWidget(1, 1).currentText() == "Girl"
    voice_box = win.cast_table.cellWidget(1, 2)
    voice_box.setCurrentIndex(voice_box.findData("Kore"))
    assert pump(qapp, lambda: studio.library.get_cast_member(book.id, "Mara")["voice"] == "Kore")
    mara = studio.library.get_cast_member(book.id, "Mara")
    assert mara["locked"] and mara["age"] == "child"


class _MicSession:
    def __init__(self):
        self.sent = []

    def send_audio(self, pcm):
        self.sent.append(pcm)


def _frames(level: int, ms: int) -> bytes:
    import array
    return array.array("h", [level, -level] * (16 * ms // 2)).tobytes()


def test_audiobook_holds_the_mic_and_talking_over_it_pauses_it(qapp):
    from jarvis_books.ui.voice_bridge import VoiceBridge
    bridge = VoiceBridge(lambda **kw: None)
    bridge.session = session = _MicSession()
    playing = [True]
    bridge.external_playing = lambda: playing[0]
    paused = []
    bridge.external_barge.connect(lambda: paused.append(True))
    for i in range(50):  # a second of the audiobook leaking into the mic: Gemini hears none of it
        bridge._route_mic(_frames(900, 20), i * 0.02)
    assert not session.sent and not paused
    for i in range(50, 100):  # the author talks over it
        bridge._route_mic(_frames(9000, 20), i * 0.02)
    assert paused and session.sent  # the audiobook pauses and what they said goes to Jarvis
    playing[0] = False
    bridge._route_mic(_frames(900, 20), 3.0)
    bridge._route_mic(_frames(900, 20), 3.5)
    sent = len(session.sent)
    bridge._route_mic(_frames(900, 20), 3.6)
    assert len(session.sent) > sent  # after the audiobook stops the mic is open again


def test_listen_tab_faster_and_slower_buttons(window):
    win, studio, _ = window
    win.speed.setCurrentIndex(win.speed.findData(1.0))
    win.faster_btn.click()
    assert studio.settings()["narration_speed"] == "1.1"
    assert abs(win.player.player.playbackRate() - 1.1) < 1e-6
    win.slower_btn.click()
    win.slower_btn.click()
    assert studio.settings()["narration_speed"] == "0.9"
    win._show_setting("narration_speed", "2.5")  # Jarvis changed it
    assert win.speed.currentData() == 2.5 and studio.settings()["narration_speed"] == "2.5"
