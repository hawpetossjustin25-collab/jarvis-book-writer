"""Jarvis on Telegram: pairing, owner-only access, streamed replies, Yes/No buttons, reminders, the poll loop."""
import threading
import time

import pytest
import requests

from conftest import hardware
from fakes import FakeGemini, FakeOllama, body
from jarvis_books.assistant.permissions import PermissionGate
from jarvis_books.storage.db import Database
from jarvis_books.storage.library import Library
from jarvis_books.studio import Studio
from jarvis_books.telegram import (MESSAGE_LIMIT, OFFSET, PAIR_TRIES, STALE_S, THINKING, TelegramAPI, TelegramBridge,
                                   TelegramError, buttons_for, key_path, load_bot_key, save_bot_key, split_text,
                                   telegram_line)

KEY = "1234567890:AAH" + "x" * 32
OWNER_ID = 5551234
STRANGER = 777


class FakeTelegram:
    """Stands in for Telegram's Bot API; records every call."""

    def __init__(self, batches=None, username="jarvis_test_bot"):
        self.batches = list(batches or [])
        self.username = username
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.cleared: list[tuple] = []
        self.answers: list[tuple] = []
        self.offsets: list = []
        self.errors: list[TelegramError] = []  # raised (FIFO) by get_updates
        self.edit_errors: list[TelegramError] = []
        self.drained = threading.Event()
        self._next = 100

    def get_me(self):
        return {"username": self.username}

    def get_updates(self, offset, timeout=50):
        self.offsets.append(offset)
        if self.errors:
            raise self.errors.pop(0)
        if self.batches:
            return self.batches.pop(0)
        self.drained.set()
        time.sleep(0.01)
        return []

    def send_message(self, chat_id, text, buttons=None):
        self._next += 1
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons, "message_id": self._next})
        return {"message_id": self._next}

    def edit_message(self, chat_id, message_id, text, buttons=None):
        if self.edit_errors:
            raise self.edit_errors.pop(0)
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "buttons": buttons})
        return {"message_id": message_id}

    def clear_buttons(self, chat_id, message_id):
        self.cleared.append((chat_id, message_id))

    def answer_button(self, callback_id, text=""):
        self.answers.append((callback_id, text))

    def typing(self, chat_id):
        pass

    def texts(self, chat_id=None):
        """What the chat shows: each sent message with its latest edit."""
        shown = {m["message_id"]: m["text"] for m in self.sent if chat_id is None or m["chat_id"] == chat_id}
        for e in self.edits:
            if e["message_id"] in shown:
                shown[e["message_id"]] = e["text"]
        return list(shown.values())


class FakeAssistant:
    def __init__(self, reply="Hi Justin.", partials=(), during=None):
        self.reply, self.partials, self.during = reply, list(partials), during
        self.calls: list[tuple] = []

    def send(self, text, on_tool=None, on_text=None, decided=None):
        self.calls.append((text, decided))
        for p in self.partials:
            on_text(p)
        if self.during:
            self.during()
        return self.reply


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def msg(text, chat=OWNER_ID, sender=None, date=None, kind="private", uid=1):
    return {"update_id": uid, "message": {"message_id": uid, "date": date if date is not None else 1_000_000,
                                          "chat": {"id": chat, "type": kind}, "from": {"id": sender or chat},
                                          "text": text}}


def press(data, sender=OWNER_ID, message_id=101, uid=9):
    return {"update_id": uid, "callback_query": {"id": f"cb{uid}", "from": {"id": sender}, "data": data,
                                                 "message": {"message_id": message_id, "chat": {"id": sender}}}}


@pytest.fixture
def library(tmp_path):
    return Library(Database(tmp_path / "lib.db"))


def bridge_for(library, assistant=None, gate=None, clock=None, api=None, key=KEY, **kw):
    gate = gate or PermissionGate(library)
    api = api or FakeTelegram()
    b = TelegramBridge(library, assistant or FakeAssistant(), gate, lambda: key, api_factory=lambda k: api,
                       clock=clock or Clock(), **kw)
    return b, api, gate


def paired(library, **kw):
    b, api, gate = bridge_for(library, **kw)
    b.handle(api, msg(b.pair_code()))
    api.sent.clear()
    return b, api, gate


# ---- key ----------------------------------------------------------------------------------------------------
def test_bot_key_is_sealed_and_checked(tmp_path, monkeypatch):
    for var in ("JARVIS_TELEGRAM_KEY", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert load_bot_key(tmp_path) is None
    save_bot_key("  " + KEY + "\n", tmp_path)
    assert load_bot_key(tmp_path) == KEY
    assert KEY.encode() not in key_path(tmp_path).read_bytes()
    for bad in ("96293", "", "hello:world", "123:short"):
        with pytest.raises(ValueError):
            save_bot_key(bad, tmp_path)
    assert load_bot_key(tmp_path) == KEY


def test_api_errors_never_carry_the_key():
    class Boom:
        def post(self, url, json=None, timeout=None):
            raise requests.ConnectionError(f"failed to reach {url}")

    with pytest.raises(TelegramError) as caught:
        TelegramAPI(KEY, session=Boom()).get_me()
    assert KEY not in str(caught.value) and caught.value.status == 0

    class Busy:
        def post(self, url, json=None, timeout=None):
            class R:
                status_code = 429

                def json(self):
                    return {"ok": False, "error_code": 429, "description": "Too Many Requests",
                            "parameters": {"retry_after": 7}}
            return R()

    with pytest.raises(TelegramError) as caught:
        TelegramAPI(KEY, session=Busy()).send_message(1, "hi")
    assert caught.value.status == 429 and caught.value.retry_after == 7.0


def test_api_sends_the_right_requests():
    seen = []

    class Ok:
        def post(self, url, json=None, timeout=None):
            seen.append((url.rsplit("/", 1)[1], json, timeout))

            class R:
                status_code = 200

                def json(self):
                    return {"ok": True, "result": [] if url.endswith("getUpdates") else {"message_id": 3}}
            return R()

    api = TelegramAPI(KEY, session=Ok())
    assert api.get_updates(42, timeout=30) == []
    api.send_message(9, "hello", buttons_for(4))
    method, payload, timeout = seen[0]
    assert method == "getUpdates" and payload["offset"] == 42 and payload["timeout"] == 30 and timeout > 30
    assert seen[1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "gate:4:yes"


# ---- pairing and who may talk -----------------------------------------------------------------------------
def test_only_the_paired_owner_is_answered(library):
    assistant = FakeAssistant()
    b, api, _ = bridge_for(library, assistant)
    code = b.pair_code()
    assert len(code) == 8 and code.isdigit()
    b.handle(api, msg("hello", chat=STRANGER))
    assert assistant.calls == [] and b.owner() is None and "private" in api.sent[-1]["text"]
    b.handle(api, msg(f"/start {code}"))
    assert b.owner() == OWNER_ID and "Paired" in api.sent[-1]["text"]
    assert b.status()["paired"] and "pair_code" not in b.status()
    b.handle(api, msg(code, chat=STRANGER))  # the old code is no use to anyone else now
    b.handle(api, msg("what's on my planner", chat=STRANGER))
    assert assistant.calls == [] and b.owner() == OWNER_ID
    b.handle(api, msg("what's on my planner"))
    assert assistant.calls == [("what's on my planner", None)]


def test_wrong_pairing_codes_make_a_new_code(library):
    b, api, _ = bridge_for(library)
    first = b.pair_code()
    wrong = f"{(int(first) + 1) % 10 ** 8:08d}"
    for _ in range(PAIR_TRIES):
        b.handle(api, msg(wrong, chat=STRANGER))
    assert b.owner() is None and b.pair_code() != first


def test_groups_and_forwarded_senders_are_ignored(library):
    assistant = FakeAssistant()
    b, api, _ = paired(library, assistant=assistant)
    b.handle(api, msg("hi", chat=-100, kind="group", sender=OWNER_ID))
    b.handle(api, msg("hi", chat=OWNER_ID, sender=STRANGER))
    assert assistant.calls == []


def test_old_messages_are_not_acted_on(library):
    clock = Clock()
    assistant = FakeAssistant()
    b, api, _ = paired(library, assistant=assistant, clock=clock)
    b.handle(api, msg("delete my notes", date=clock.t - STALE_S - 5))
    assert assistant.calls == [] and "Send it again" in api.sent[-1]["text"]


# ---- replies ------------------------------------------------------------------------------------------------
def test_reply_streams_into_one_message(library):
    clock = Clock()
    words = ["Once", "Once upon", "Once upon a", "Once upon a time"]

    class Ticking(FakeAssistant):
        def send(self, text, on_tool=None, on_text=None, decided=None):
            self.calls.append((text, decided))
            for p in words:
                clock.t += 1.0
                on_text(p)
            return "Once upon a time, the end."

    b, api, _ = paired(library, assistant=Ticking(), clock=clock, edit_every_s=1.5)
    b.handle(api, msg("tell me a story", date=clock.t))
    assert api.sent[0]["text"] == THINKING and len(api.sent) == 1
    partial = [e["text"] for e in api.edits[:-1]]
    assert partial == ["Once upon", "Once upon a time"]  # at most one edit every 1.5 s
    assert api.texts() == ["Once upon a time, the end."]


def test_long_replies_are_split(library):
    long = "\n\n".join(f"Paragraph {i} " + "word " * 300 for i in range(8))
    b, api, _ = paired(library, assistant=FakeAssistant(reply=long))
    b.handle(api, msg("write a lot"))
    shown = api.texts()
    assert len(shown) > 1 and all(len(t) <= MESSAGE_LIMIT for t in shown)
    assert " ".join(" ".join(shown).split()) == " ".join(long.split())


def test_split_text_cuts_at_spaces():
    pieces = split_text("abc " * 3000, limit=1000)
    assert all(len(p) <= 1000 for p in pieces) and all(not p.endswith(" ") for p in pieces)
    assert split_text("") == [""]


def test_a_failed_edit_still_delivers_the_reply(library):
    b, api, _ = paired(library, assistant=FakeAssistant(reply="Done."))
    api.edit_errors = [TelegramError(400, "message to edit not found")]
    b.handle(api, msg("hi"))
    assert api.sent[-1]["text"] == "Done."


def test_a_crash_inside_jarvis_is_told_not_hidden(library):
    class Broken(FakeAssistant):
        def send(self, *a, **k):
            raise RuntimeError("boom")

    b, api, _ = paired(library, assistant=Broken())
    b.handle(api, msg("hi"))
    assert "went wrong" in api.texts()[-1]


# ---- the yes/no gate ----------------------------------------------------------------------------------------
def gated(library):
    ran = []
    gate = PermissionGate(library)
    assistant = FakeAssistant(reply="Should I delete chapter 3? Yes or no?")
    assistant.during = lambda: gate.request("delete_path", {"path": "ch3"}, "delete ch3",
                                            lambda: ran.append("deleted") or {"deleted": True})
    b, api, _ = paired(library, assistant=assistant, gate=gate)
    return b, api, gate, assistant, ran


def test_questions_come_with_yes_no_buttons_and_yes_runs_the_action(library):
    b, api, gate, assistant, ran = gated(library)
    b.handle(api, msg("delete chapter 3"))
    final = api.edits[-1]
    rid = gate.pending.id
    assert final["buttons"] == buttons_for(rid) and ran == []
    assistant.during, assistant.reply = None, "Deleted."
    b.handle(api, press(f"gate:{rid}:yes", message_id=final["message_id"]))
    assert ran == ["deleted"] and gate.pending is None
    text, decided = assistant.calls[-1]
    assert text == "yes" and decided["approved"] and decided["status"] == "approved"
    assert api.cleared == [(OWNER_ID, final["message_id"])] and api.texts()[-1] == "Deleted."


def test_no_button_declines(library):
    b, api, gate, assistant, ran = gated(library)
    b.handle(api, msg("delete chapter 3"))
    rid = gate.pending.id
    assistant.during = None
    b.handle(api, press(f"gate:{rid}:no"))
    assert ran == [] and assistant.calls[-1][1]["status"] == "denied"


def test_old_or_foreign_buttons_do_nothing(library):
    b, api, gate, assistant, ran = gated(library)
    b.handle(api, msg("delete chapter 3"))
    rid = gate.pending.id
    b.handle(api, press(f"gate:{rid}:yes", sender=STRANGER))
    assert ran == [] and gate.pending is not None and "private" in api.answers[-1][1]
    b.handle(api, press(f"gate:{rid + 50}:yes"))
    assert ran == [] and "closed" in api.answers[-1][1]
    b.handle(api, press("gate:abc:yes"))
    assert ran == [] and gate.pending is not None


def test_questions_asked_elsewhere_get_no_buttons(library):
    gate = PermissionGate(library)
    b, api, _ = paired(library, gate=gate)
    t = threading.Thread(target=lambda: gate.request("run_command", {}, "run", lambda: {}))
    t.start()
    t.join()
    b.handle(api, msg("hi"))
    assert api.edits[-1]["buttons"] is None


def test_reminders_ring_on_the_phone(library):
    from jarvis_books.planner import Planner

    planner = Planner(library.db)
    gate = PermissionGate(library)
    api = FakeTelegram()
    b = TelegramBridge(library, FakeAssistant(), gate, lambda: KEY, planner=planner, api_factory=lambda k: api)
    b.handle(api, msg(b.pair_code()))
    b.start()
    try:
        assert api.drained.wait(5)
        planner.add_reminder("take the trash out", due_at=time.time() - 1)
        planner.fire_due()
        assert any(m["text"] == "Reminder: take the trash out" for m in api.sent)
    finally:
        b.stop()


# ---- the poll loop --------------------------------------------------------------------------------------------
def wait_until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_loop_answers_and_remembers_where_it_stopped(library):
    assistant = FakeAssistant()
    b, api, _ = bridge_for(library, assistant, clock=time.time)
    code = b.pair_code()
    now = time.time()
    api.batches = [[msg(code, uid=10, date=now)], [msg("hello", uid=11, date=now)]]
    assert b.start()
    try:
        assert wait_until(lambda: len(assistant.calls) == 1 and library.get_meta(OFFSET) == "12")
        assert b.status()["bot"] == "jarvis_test_bot" and b.running
    finally:
        b.stop()
    assert wait_until(lambda: not b._thread.is_alive())
    api2 = FakeTelegram()
    b2, _, _ = bridge_for(library, assistant, api=api2)
    b2.start()
    try:
        assert wait_until(lambda: api2.offsets) and api2.offsets[0] == 12
    finally:
        b2.stop()


def test_loop_waits_out_network_trouble_and_stops_on_a_bad_key(library):
    b, api, _ = bridge_for(library, poll_s=1)
    api.errors = [TelegramError(0, "no connection")]
    b.start()
    try:
        assert wait_until(lambda: api.drained.is_set() and b.error is None, 8)
    finally:
        b.stop()
    bad = FakeTelegram()
    bad.errors = [TelegramError(401, "Unauthorized")]
    b2, _, _ = bridge_for(library, api=bad)
    b2.start()
    assert wait_until(lambda: not b2._thread.is_alive())
    assert "wrong" in b2.error and not b2.running
    assert "wrong" in telegram_line(b2.status())


def test_no_key_no_thread(library):
    b, _, _ = bridge_for(library, key=None)
    assert b.start() is False and not b.running
    assert "BotFather" in telegram_line(b.status())


def test_settings_line():
    assert "send @jb_bot this code: 12345678" in telegram_line(
        {"key_saved": True, "running": True, "bot": "jb_bot", "paired": False, "pair_code": "12345678"})
    assert "paired" in telegram_line({"key_saved": True, "running": True, "bot": "jb_bot", "paired": True})
    assert "turned off" in telegram_line({"key_saved": True, "running": False, "bot": None, "paired": True})


# ---- inside the whole app -----------------------------------------------------------------------------------
def test_studio_answers_on_telegram_with_the_same_jarvis(tmp_path, monkeypatch):
    for var in ("JARVIS_TELEGRAM_KEY", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    gemini = FakeGemini(lambda model, payload: body("Hello from Jarvis."))
    s = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=gemini,
               engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None}, local_client=FakeOllama())
    api = FakeTelegram()
    s.telegram.api_factory = lambda k: api
    s.start(probe=False)
    try:
        assert not s.telegram.running and not s.telegram.status()["key_saved"]
        status = s.set_telegram_key(KEY)
        assert status["key_saved"] and len(status["pair_code"]) == 8
        assert wait_until(lambda: api.drained.is_set())
        s.telegram.handle(api, msg(status["pair_code"], date=time.time()))
        s.telegram.handle(api, msg("hi jarvis", date=time.time()))
        assert api.texts()[-1] == "Hello from Jarvis."
        said = [(m.channel, m.role, m.text) for m in s.library.recent_messages(("telegram",))]
        assert said == [("telegram", "user", "hi jarvis"), ("telegram", "assistant", "Hello from Jarvis.")]
        assert "telegram" in s.assistant.history_channels  # the app's chat remembers what was said on the phone
        s.set_setting("telegram", "0")
        assert not s.telegram.running
        s.set_telegram_key(KEY)  # the same key again keeps the pairing
        assert s.telegram.owner() == OWNER_ID
        s.remove_telegram()
        assert s.telegram.owner() is None and not s.telegram.status()["key_saved"]
    finally:
        s.close()


def test_a_button_answer_reaches_the_brain_as_the_outcome(tmp_path):
    seen = []

    def responder(model, payload):
        seen.append(payload)
        return body("Okay, done.")

    s = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(responder),
               engine_kwargs={"hardware_probe": hardware, "sleep": lambda s: None}, local_client=FakeOllama())
    s.start(probe=False)
    try:
        ran = []
        req = s.gate.request("run_command", {"command": "dir"}, "run dir", lambda: ran.append(1) or {"ok": True})
        outcome = s.gate.answer(True, req.id, via="telegram")
        assert s.telegram_assistant.send("yes", decided=outcome) == "Okay, done."
        said = [p.get("text", "") for turn in seen[-1]["contents"] for p in turn.get("parts", [])]
        assert ran == [1] and any("said YES to 'run dir'" in t for t in said)
    finally:
        s.close()
