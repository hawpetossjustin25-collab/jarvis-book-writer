import os
import sys
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
    qapp.processEvents()
    win.deleteLater()
    studio.close()
    qapp.processEvents()


def test_tabs_and_library(window, qapp):
    win, studio, book = window
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == \
        ["Jarvis", "Chat", "Library", "Write", "Listen", "Settings", "War Room", "Publish",
         "Workshop", "Planner"]
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


def test_full_control_switch_in_settings(window, qapp):
    win, studio, book = window
    assert not win.full_control.isChecked() and studio.gate.decide("run_command") == "ask"
    win.full_control.setChecked(True)
    assert studio.settings()["full_control"] == "1" and studio.gate.decide("run_command") == "auto"
    studio.set_setting("full_control", "0")  # Jarvis turned it off: the box follows
    win._refresh_brain()
    assert not win.full_control.isChecked()


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


@pytest.mark.skipif(sys.platform == "win32", reason="Qt's offscreen Windows media backend hangs in this test on slow hardware; the speed step logic is covered in test_permissions")
def test_listen_tab_faster_and_slower_buttons(window):
    win, studio, _ = window
    win.speed.setCurrentIndex(win.speed.findData(1.0))
    win.faster_btn.click()
    assert studio.settings()["narration_speed"] == "1.1"
    assert abs(win.player.speed - 1.1) < 1e-6
    win.slower_btn.click()
    win.slower_btn.click()
    assert studio.settings()["narration_speed"] == "0.9"
    win._show_setting("narration_speed", "2.5")  # Jarvis changed it
    assert win.speed.currentData() == 2.5 and studio.settings()["narration_speed"] == "2.5"


class _QuietMedia:
    """Stands in for QMediaPlayer so the test never asks the OS to decode a fake file."""

    def __init__(self, real):
        self.real = real

    def setSource(self, url):
        pass

    def play(self):
        pass

    def __getattr__(self, name):
        return getattr(self.real, name)


class _Bridge:
    def __init__(self):
        self.active, self.sent = True, []

    def send_text(self, text):
        self.sent.append(text)


@pytest.mark.skipif(sys.platform == "win32", reason="Qt's offscreen Windows media backend fails fast in this test on slow hardware; playback logic is covered by test_play_reports_playing_only_with_audio_ready")
def test_jarvis_is_told_the_truth_about_playback(window, qapp, monkeypatch):
    win, studio, book = window
    empty = studio.library.create_book("Blank", "fantasy", "nothing yet")
    result = win._audio_do("play", {"book_id": empty.id, "chapter": 1})
    assert result["ok"] is False and result["playing"] is False and "no written chapters" in result["error"]

    monkeypatch.setattr(win.player.narrator, "cached_path", lambda seg: None)
    monkeypatch.setattr(win.player, "_fetch", lambda i: None)
    result = win._audio_do("play", {"book_id": book.id, "chapter": 2})
    assert result["ok"] and result["playing"] is False and result["recording"] is True
    assert result["chapter"] == 1 and "isn't written yet" in result["note"]

    bridge = _Bridge()
    monkeypatch.setattr(win, "voice", bridge)
    win.player._failed.emit(win.player.index, "Could not record audio: quota")
    pump(qapp, lambda: bridge.sent)
    assert not win.player.playing and not win.player._waiting
    assert "not playing" in bridge.sent[0] and "quota" in bridge.sent[0]
    assert "quota" in win.now_playing.text()


def test_play_reports_playing_only_with_audio_ready(window, qapp, monkeypatch, tmp_path):
    win, studio, book = window
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF")
    monkeypatch.setattr(win.player.narrator, "cached_path", lambda seg: clip)
    monkeypatch.setattr(win.player, "player", _QuietMedia(win.player.player))
    monkeypatch.setattr(win.player, "_fetch", lambda i: None)
    result = win._audio_do("play", {"book_id": book.id, "chapter": 1})
    assert result == {"ok": True, "chapter": 1, "playing": True}
    assert win._audio_do("stop", {}) == {"ok": True, "action": "stop"}


def test_brain_settings_switch_and_switch_notice(window, qapp):
    win, studio, book = window
    assert "LFM3" in win.brain_state.text() and not win.brain_download.isHidden()  # tests use an offline Ollama
    win.brain_choice.setCurrentIndex(win.brain_choice.findData("local"))
    assert studio.settings()["brain"] == "local"
    win.brain_auto.setChecked(False)
    assert studio.settings()["brain_auto"] == "0"
    studio.tools.call("switch_brain", {"use": "gemini", "automatic": "on"})
    win._refresh_settings()
    assert win.brain_choice.currentData() == "gemini" and win.brain_auto.isChecked()
    win._on_brain_switch("local", "gemini", "Gemini: quota used up")
    assert "switched to LFM3" in win.statusBar().currentMessage()
    assert "switched to LFM3" in win.chat_view.toPlainText()
    win._on_brain_switch("gemini", "local", "back")
    assert "working again" in win.statusBar().currentMessage()


def test_speed_waits_for_audio_then_applies(window, tmp_path):
    win, studio, _ = window
    player = win.player
    player.set_speed(1.5)
    assert player.speed == 1.5  # nothing loaded: Qt's rate isn't touched
    wav = tmp_path / "a.wav"
    from jarvis_books.llm.gemini import pcm_to_wav
    wav.write_bytes(pcm_to_wav(b"\x00\x00" * 2400))
    player._set_source(wav)
    assert abs(player.player.playbackRate() - 1.5) < 1e-6
    player.set_speed(2.0)
    assert abs(player.player.playbackRate() - 2.0) < 1e-6
    player.player.setSource(__import__("PyQt6.QtCore", fromlist=["QUrl"]).QUrl())


def _gemini_answers(studio, data):
    import json
    from fakes import body
    studio.engine.client.responder = lambda model, payload: body(json.dumps(data))


# ---- Workshop -------------------------------------------------------------------------------------------
def _project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "hello.py").write_text("print('hello')\n")
    (proj / "test_hello.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    return proj


def test_workshop_opens_edits_saves_with_backup_and_undo(window, qapp, tmp_path):
    win, studio, _ = window
    ws, proj = win.workshop, _project(tmp_path)
    assert ws.open_folder(str(proj)) and ws.open_file(str(proj / "hello.py"))
    assert ws.editor.toPlainText() == "print('hello')\n" and not ws.save_btn.isEnabled()
    ws.editor.setPlainText("print('bye')\n")
    assert ws.dirty and ws.save_btn.isEnabled() and ws.file_label.text().startswith("●")
    assert not ws.open_file(str(proj / "test_hello.py"))  # unsaved edits block switching files
    assert ws.save() and (proj / "hello.py").read_text() == "print('bye')\n" and not ws.dirty
    studio.tools.run("undo_last_change")
    assert (proj / "hello.py").read_text() == "print('hello')\n"
    (proj / ".env").write_text("KEY=1")
    assert not ws.open_file(str(proj / ".env")) and "passwords" in ws.status.text()


def test_workshop_treats_crlf_files_as_unchanged(window, qapp, tmp_path):
    """Windows text files carry \r\n; Qt shows \n — that must not read as unsaved edits."""
    win, studio, _ = window
    ws, proj = win.workshop, _project(tmp_path)
    (proj / "crlf.py").write_bytes(b"print('hi')\r\n")
    assert ws.open_file(str(proj / "crlf.py"))
    assert not ws.dirty and not ws.save_btn.isEnabled()
    studio.tools.run("write_file", {"path": str(proj / "crlf.py"), "content": "print('new')\n"})
    assert pump(qapp, lambda: ws.editor.toPlainText() == "print('new')\n", timeout=15)


def test_workshop_reloads_a_file_jarvis_changed(window, qapp, tmp_path):
    win, studio, _ = window
    ws, proj = win.workshop, _project(tmp_path)
    ws.open_file(str(proj / "hello.py"))
    studio.tools.run("write_file", {"path": str(proj / "hello.py"), "content": "print('from jarvis')\n"})
    assert pump(qapp, lambda: ws.editor.toPlainText() == "print('from jarvis')\n", timeout=15)


def test_workshop_runs_files_commands_and_tests(window, qapp, tmp_path):
    win, studio, _ = window
    ws, proj = win.workshop, _project(tmp_path)
    ws.open_folder(str(proj))
    ws.open_file(str(proj / "hello.py"))
    results = []
    ws.command_finished.connect(results.append)
    ws.run_file()
    assert pump(qapp, lambda: results, timeout=20)
    assert results[-1]["ok"] and "hello" in ws.output.toPlainText()
    ws.run_tests()
    assert pump(qapp, lambda: len(results) == 2, timeout=60)
    assert results[-1]["ok"] and "1 passed" in results[-1]["output"]
    ws.run_command("python3 -c \"import sys; sys.exit(4)\"" if sys.platform != "win32" else "exit 4")
    assert pump(qapp, lambda: len(results) == 3, timeout=20)
    assert results[-1]["exit_code"] == 4 and "failed: exit code 4" in ws.output.toPlainText()


@pytest.mark.skipif(sys.platform == "win32", reason="bash command")
def test_workshop_stop_button_ends_a_long_command(window, qapp, tmp_path):
    win, studio, _ = window
    ws = win.workshop
    results = []
    ws.command_finished.connect(results.append)
    ws.run_command("sleep 30")
    assert ws.stop_btn.isEnabled() and not ws.run_command("echo second")  # one command at a time
    ws.stop()
    assert pump(qapp, lambda: results, timeout=10) and results[0]["stopped"] == "cancelled"


def test_jarvis_proposes_code_as_a_diff_to_apply_or_discard(window, qapp, tmp_path):
    win, studio, _ = window
    ws, proj = win.workshop, _project(tmp_path)
    ws.open_file(str(proj / "hello.py"))
    _gemini_answers(studio, {"summary": "Greets by name.", "content": "name = 'Justin'\nprint('hello', name)\n"})
    ws.ask_edit.setText("greet me by name")
    assert ws.ask_jarvis()
    assert pump(qapp, lambda: ws.pending is not None, timeout=10)
    assert "Greets by name." in ws.change_label.text() and "+print('hello', name)" in ws.change_view.toPlainText()
    assert (proj / "hello.py").read_text() == "print('hello')\n"  # nothing changes before Apply
    assert ws.apply_change() and (proj / "hello.py").read_text() == "name = 'Justin'\nprint('hello', name)\n"
    _gemini_answers(studio, {"summary": "x", "content": "```python\nprint(1)\n```"})
    ws.ask_jarvis()
    assert pump(qapp, lambda: ws.pending is not None, timeout=10)
    assert ws.pending["new"] == "print(1)"
    ws.discard_change()
    assert ws.pending is None and ws.change_box.isHidden() and "Justin" in (proj / "hello.py").read_text()


def test_workshop_code_needs_gemini_not_lfm3(window, qapp, tmp_path, monkeypatch):
    win, studio, _ = window
    ws, proj = win.workshop, _project(tmp_path)
    ws.open_file(str(proj / "hello.py"))
    monkeypatch.setattr(studio.engine, "_config", None)
    ws.ask_edit.setText("anything")
    assert not ws.ask_jarvis() and "needs Gemini" in ws.status.text()


def test_api_keys_are_saved_in_settings_and_never_shown(window, qapp):
    win, studio, _ = window
    win.api_name_edit.setText("openweather")
    win.api_value_edit.setText("SECRET-abcdef")
    win._save_api_key()
    assert studio.api_keys.names() == ["openweather"] and win.api_value_edit.text() == ""
    assert "openweather" in win.api_keys_label.text() and "SECRET" not in win.api_keys_label.text()
    win.api_name_edit.setText("openweather")
    win._remove_api_key()
    assert studio.api_keys.names() == [] and "No API keys" in win.api_keys_label.text()


def test_planner_tab_adds_items_reminders_and_a_writing_schedule(window, qapp):
    from PyQt6.QtCore import QDate, Qt
    win, studio, book = window
    tab = win.planner_tab
    tomorrow = QDate.currentDate().addDays(1)
    tab.calendar.setSelectedDate(tomorrow)
    tab.item_title.setText("Edit chapter 2")
    tab.item_remind.setChecked(True)
    tab._add_item()
    assert "Edit chapter 2" in tab.items.item(0).text() and "9:00 AM" in tab.items.item(0).text()
    assert any("Edit chapter 2" in tab.reminders.item(i).text() for i in range(tab.reminders.count()))
    tab.items.item(0).setCheckState(Qt.CheckState.Checked)
    pump(qapp, lambda: studio.planner.items(start=0, end=None)[0].done)
    assert studio.planner.reminders() == []  # done tasks don't ring
    tab.rem_text.setText("Stretch")
    tab._add_reminder()
    assert "Reminder set for" in tab.status.text() and studio.planner.reminders()[0].text == "Stretch"
    tab.sched_book.setCurrentIndex(tab.sched_book.findData(book.id))
    tab._make_schedule()
    assert "Planned 2 writing sessions" in tab.status.text()
    assert "planner" in [win.tabs.tabText(i).lower() for i in range(win.tabs.count())]


def test_a_due_reminder_rings_the_alarm_until_snoozed_or_stopped(window, qapp):
    import time as _time
    win, studio, _ = window
    told = []
    win.alarms.tell_jarvis = told.append
    studio.planner.add_reminder("Send the draft", _time.time())
    studio.planner.fire_due()
    assert pump(qapp, lambda: win.alarms.dialogs)
    rid, dlg = next(iter(win.alarms.dialogs.items()))
    assert dlg.isVisible() and dlg.text.text() == "Send the draft" and win.alarms.sound_on
    assert "Send the draft" in told[0] and win.alarms.notified == ["Reminder: Send the draft"]
    dlg.snooze_btn.click()
    assert pump(qapp, lambda: not win.alarms.dialogs) and not win.alarms.sound_on
    assert studio.planner.get_reminder(rid).status == "pending"
    studio.planner.update_reminder(rid, due_at=_time.time())
    studio.planner.fire_due()
    assert pump(qapp, lambda: win.alarms.dialogs)
    win.alarms.dialogs[rid].stop_btn.click()
    assert pump(qapp, lambda: not win.alarms.dialogs)
    assert studio.planner.get_reminder(rid).status == "done" and not win.alarms.sound_on
    studio.planner.add_reminder("Quiet one", _time.time(), alarm=False)
    studio.planner.fire_due()
    assert pump(qapp, lambda: win.alarms.dialogs) and not win.alarms.sound_on
    assert studio.tools.call("stop_alarm")["stopped"] == [{"text": "Quiet one"}]
    assert pump(qapp, lambda: not win.alarms.dialogs)


def test_alarm_sound_file_is_a_real_wav(tmp_path):
    import wave
    from jarvis_books.ui.planner import make_alarm_sound
    path = make_alarm_sound(tmp_path / "alarm.wav")
    with wave.open(str(path)) as w:
        assert w.getframerate() == 22050 and 1.5 < w.getnframes() / w.getframerate() < 3
