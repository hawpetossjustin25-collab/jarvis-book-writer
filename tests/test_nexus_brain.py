"""Jarvis's brain over NEXUS for Worldcraft and its games: status, generate, roles, checks, AI-made labels."""
from jarvis_books.llm.errors import LLMError
from jarvis_books.nexus.api import NexusHub, systems_for
from jarvis_books.nexus.contracts import NexusRequest


class FakeResp:
    text, model = "Maya waves.", "Groq (openai/gpt-oss-120b)"


class FakeBrain:
    current = "pool"

    def __init__(self, fail=False):
        self.fail, self.seen = fail, []

    def generate(self, request):
        self.seen.append(request)
        if self.fail:
            raise LLMError("every extra key is resting or refused", kind="rate")
        return FakeResp()

    def status(self):
        return {"extra_keys": "2 of 2 keys ready"}


def hub(brain):
    return NexusHub(systems_for(None, None, None, brain=brain), workers=1)


def ask(h, op, payload, role="operator"):
    return h.route(NexusRequest("jarvis", "brain", op, payload), role)


def test_generate_answers_and_says_it_is_ai_made():
    b = FakeBrain()
    r = ask(hub(b), "generate", {"prompt": "A line for Maya", "system": "game", "purpose": "dialogue",
                                 "max_tokens": 200})
    assert r.ok and r.results[0] == {"text": "Maya waves.", "model": "Groq (openai/gpt-oss-120b)", "ai_made": True,
                                     "brain": "pool"}
    assert b.seen[0].max_output_tokens == 200 and b.seen[0].purpose == "nexus:dialogue"


def test_status_is_open_to_readers_but_generate_is_not():
    h = hub(FakeBrain())
    assert ask(h, "status", {}, role="reader").results[0]["extra_keys"] == "2 of 2 keys ready"
    r = ask(h, "generate", {"prompt": "x"}, role="reader")
    assert not r.ok and r.error_code == "unauthorized"


def test_bad_requests_are_refused_before_the_brain_is_asked():
    b = FakeBrain()
    h = hub(b)
    for payload in ({}, {"prompt": ""}, {"prompt": "x" * 20001}, {"prompt": "x", "purpose": "hack"},
                    {"prompt": "x", "max_tokens": 99999}, {"prompt": "x", "max_tokens": True}, {"prompt": "x", "system": 5}):
        r = ask(h, "generate", payload)
        assert not r.ok and r.error_code == "invalid_request", payload
    assert ask(h, "explode", {}).error_code == "unknown_operation" and b.seen == []


def test_all_keys_out_is_reported_as_unavailable():
    r = ask(hub(FakeBrain(fail=True)), "generate", {"prompt": "x"})
    assert not r.ok and r.error_code == "unavailable" and "resting" in r.error_message
