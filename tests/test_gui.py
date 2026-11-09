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
        ["Jarvis", "Chat", "Library", "Write", "Listen", "Settings"]
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
    studio.client.live_url = lambda: "wss://example/live"
    studio.client.live_headers = lambda: {"x-goog-api-key": "k"}
    session = win._make_live_session()
    assert session.url == "wss://example/live" and isinstance(session.headers, dict)
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
