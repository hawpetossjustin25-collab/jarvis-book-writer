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
