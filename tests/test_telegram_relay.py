"""The Jarvis switcher on the one Telegram bot: Book Writer, Game Creator and In-Game Jarvis, and their NEXUS door."""
import pytest

from jarvis_books.nexus.api import NexusHub, TokenBook, systems_for
from jarvis_books.nexus.contracts import NexusRequest
from jarvis_books.telegram_relay import BOOK, CREATOR, GAME, TelegramRelay
from test_telegram import OWNER_ID, FakeAssistant, msg, paired, press


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def relay(tmp_path):
    return TelegramRelay(tmp_path, clock=Clock())


class Bridge:
    running, n = True, 500

    def owner(self):
        return OWNER_ID

    def send_owner(self, text, buttons=None):
        self.n += 1
        self.last = (text, buttons)
        return self.n


def test_names_replies_and_switches_pick_the_jarvis(relay, tmp_path):
    assert relay.route("game where is Maya?") == (GAME, "where is Maya?")
    assert relay.route("In-Game Jarvis: open the map") == (GAME, "open the map")
    assert relay.route("creator run the tests") == (CREATOR, "run the tests")
    assert relay.route("wc check steam") == (CREATOR, "check steam")
    assert relay.route("book: write chapter 3") == (BOOK, "write chapter 3")
    assert relay.route("gamers are great") == (BOOK, "gamers are great")   # a word starting with "game" isn't a name
    assert relay.route("game") == (BOOK, "game")                            # a name alone is just a message
    assert relay.route("switch to game creator Jarvis") == ("switch", CREATOR)
    assert relay.current() == CREATOR and relay.route("run the tests") == (CREATOR, "run the tests")
    assert relay.route("which jarvis am I talking to?") == ("ask", CREATOR)
    assert TelegramRelay(tmp_path).current() == CREATOR                     # survives a restart
    assert relay.route("go back to book jarvis") == ("switch", BOOK) and relay.current() == BOOK


def test_replies_go_back_to_whoever_wrote_the_message(relay):
    relay.bridge = b = Bridge()
    assert not relay.in_use()
    assert relay.send("Quest done", GAME, source="Aether") and b.last == ("[In-Game Jarvis · Aether] Quest done", None)
    assert relay.send("Build ready", CREATOR, buttons=[[{"text": "Approve", "callback_data": "wc:approve:abcdef"}]])
    assert b.last[1] == {"inline_keyboard": [[{"text": "Approve", "callback_data": "wc:approve:abcdef"}]]}
    assert relay.route("what next?", reply_to=501) == (GAME, "what next?")
    assert relay.route("ok", reply_to=502) == (CREATOR, "ok")
    assert relay.route("thanks", reply_to=77) == (BOOK, "thanks")         # any other bot message is the book Jarvis's
    assert not relay.send("x", BOOK) and relay.in_use()
    b.running = False
    assert not relay.send("offline", GAME)


def test_inbox_keeps_messages_until_read_and_drops_old_ones(relay, tmp_path):
    a = relay.put("game", OWNER_ID, OWNER_ID, text="hello game")
    relay.put("worldcraft", OWNER_ID, OWNER_ID, callback="wc:approve:abcdef")
    assert not relay.listening(GAME)
    assert [m["text"] for m in relay.take("game")] == ["hello game"]
    assert relay.take("game", after=a) == [] and relay.take("game")[0]["update_id"] == a   # nothing deleted
    assert relay.take("worldcraft")[0]["callback"] == "wc:approve:abcdef"
    assert relay.listening(GAME) and relay.listening(BOOK)
    relay.clock.t += 100
    assert not relay.listening(GAME)
    relay.clock.t += 8 * 24 * 3600
    relay.put("game", OWNER_ID, OWNER_ID, text="new")
    assert [m["text"] for m in relay.take("game")] == ["new"]
    with pytest.raises(ValueError):
        relay.put("book", 1, 1, text="x")


def test_the_bot_switches_between_the_three_jarvises(library, tmp_path):
    relay = TelegramRelay(tmp_path)
    assistant = FakeAssistant()
    b, api, gate = paired(library, assistant=assistant, relay=relay)
    relay.bridge = b
    b.handle(api, msg("game open the door", uid=2))
    assert relay.take("game")[0]["text"] == "open the door"
    assert "Sent to In-Game Jarvis." in api.sent[-1]["text"] and assistant.calls == []
    b.handle(api, msg("switch to creator jarvis", uid=3))
    assert "Now talking to Game Creator Jarvis" in api.sent[-1]["text"]
    b.handle(api, msg("which jarvis is on?", uid=4))
    assert api.sent[-1]["text"].startswith("You're talking to Game Creator Jarvis")
    b.handle(api, msg("run the tests", uid=5))
    assert relay.take("worldcraft")[-1]["text"] == "run the tests"
    assert api.sent[-1]["text"].startswith("Sent to Game Creator Jarvis.")
    b.handle(api, msg("book how is chapter 2?", uid=6))
    assert assistant.calls[-1][0] == "how is chapter 2?"
    assert api.edits[-1]["text"] == "[Book Writer Jarvis] Hi Justin."
    b.handle(api, press("wc:approve:abcdef", uid=7))
    assert relay.take("worldcraft")[-1]["callback"] == "wc:approve:abcdef"
    assert api.answers[-1][1] == "Sent to Game Creator Jarvis"


def test_without_a_relay_the_bot_is_unchanged(library):
    assistant = FakeAssistant()
    b, api, gate = paired(library, assistant=assistant)
    b.handle(api, msg("game open the door", uid=2))
    assert assistant.calls[-1][0] == "game open the door" and api.edits[-1]["text"] == "Hi Justin."


def door(relay, role, op, payload, destination="telegram"):
    hub = NexusHub(systems_for(None, None, None, telegram=relay), workers=1)
    return hub.route(NexusRequest("jarvis", destination, op, payload), role)


def test_nexus_door_limits_games_to_their_own_inbox(relay):
    relay.put("game", OWNER_ID, OWNER_ID, text="hi game")
    relay.put("worldcraft", OWNER_ID, OWNER_ID, text="hi creator")
    assert door(relay, "game", "inbox", {"channel": "game"}).results[0]["text"] == "hi game"
    assert door(relay, "game", "inbox", {"channel": "worldcraft"}).error_code == "unauthorized"
    assert door(relay, "operator", "inbox", {"channel": "worldcraft", "after": 0}).results[0]["text"] == "hi creator"
    assert door(relay, "reader", "inbox", {}).error_code == "unauthorized"
    assert door(relay, "reader", "status", {}).results == [{"connected": False}]
    assert door(relay, "operator", "inbox", {"after": -1}).error_code == "invalid_request"
    assert door(relay, "operator", "inbox", {"channel": "book"}).error_code == "invalid_request"
    assert door(relay, "operator", "send", {"text": "x"}).error_code == "unavailable"     # Telegram off
    bad = {"text": "x", "buttons": [[{"text": "Delete", "callback_data": "gate:1:yes"}]]}
    assert door(relay, "operator", "send", bad).error_code == "invalid_request"
    ok_buttons = [[{"text": "Approve", "callback_data": "wc:approve:abcdef"}]]
    assert door(relay, "game", "send", {"text": "x", "buttons": ok_buttons}).error_code == "invalid_request"
    assert door(relay, "game", "send", {"text": ""}).error_code == "invalid_request"
    relay.bridge = b = Bridge()
    assert door(relay, "game", "send", {"text": "hi", "source": "Aether", "channel": "worldcraft"}).ok
    assert b.last[0] == "[In-Game Jarvis · Aether] hi"                    # a game can't speak as the creator
    assert door(relay, "operator", "send", {"text": "built", "buttons": ok_buttons}).ok
    assert b.last[0] == "[Game Creator Jarvis] built"
    assert door(relay, "game", "search", {"q": "x"}, destination="os1").error_code == "unauthorized"


def test_app_tokens_are_made_once_with_their_role(tmp_path):
    book = TokenBook(tmp_path)
    game = book.app_token("game", "game")
    assert book.app_token("game", "game") == game and book.role_of(game) == "game"
    assert book.role_of(book.app_token("worldcraft", "operator")) == "operator"
    assert game not in (tmp_path / "nexus_api.json").read_text()
