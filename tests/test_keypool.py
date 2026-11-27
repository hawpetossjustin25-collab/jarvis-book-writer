"""Extra API keys: sealed storage, shared-limit groups, the automatic key switcher, model picking, Brain wiring."""
import json
import logging

import pytest

from jarvis_books.llm.brain import GEMINI, POOL, Brain
from jarvis_books.llm.errors import LLMError
from jarvis_books.llm.groq import read_limits
from jarvis_books.llm.keypool import KeyPool, PoolEngine, next_day
from jarvis_books.llm.models import LLMRequest

DAY0 = 1_795_000_000.0


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


class Net:
    """A fake internet: per-key replies for chat, model lists and OpenRouter's key status."""

    def __init__(self):
        self.chat, self.models, self.key_info, self.posts = {}, {}, {}, []

    def post(self, url, json=None, timeout=None, headers=None):
        key = headers["Authorization"].split()[1]
        self.posts.append((key, url, json["model"]))
        r = self.chat[key].pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, headers=None, timeout=None):
        key = headers["Authorization"].split()[1]
        if url.endswith("/key"):
            return Resp(200, {"data": self.key_info[key]})
        return self.models.get(key, Resp(401, {"error": {"message": "bad key"}}))


def ok(text="hi", headers=None):
    return Resp(200, {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 5, "completion_tokens": 1}}, headers)


def limited(daily=False):
    msg = "Rate limit reached for requests per day (RPD)" if daily else "Rate limit reached, try again in 2s"
    return Resp(429, {"error": {"message": msg}}, {"retry-after": "2"})


class Clock:
    def __init__(self, t=DAY0):
        self.t = t

    def __call__(self):
        return self.t


def pool(tmp_path, clock=None):
    net = Net()
    return KeyPool(tmp_path, clock=clock or Clock(), session=net), net


REQ = LLMRequest(role="chat", system="s", instruction="hello")


def test_keys_are_sealed_never_listed_and_survive_a_restart(tmp_path):
    p, _ = pool(tmp_path)
    secret = "gsk_" + "A" * 40 + "wxyz"
    e = p.add("groq", secret, label="Main Groq", model="openai/gpt-oss-120b")
    assert e["key"] == "...wxyz" and secret not in json.dumps(p.listing())
    assert secret.encode() not in (tmp_path / "llm_keys.sealed").read_bytes()
    assert secret not in (tmp_path / "llm_keys_state.json").read_text()
    again, _ = pool(tmp_path)
    assert [k["label"] for k in again.listing()] == ["Main Groq"]
    with pytest.raises(ValueError, match="already saved"):
        again.add("groq", secret)
    with pytest.raises(ValueError):
        again.add("groq", "short")
    with pytest.raises(ValueError):
        again.add("nope", "x" * 30)
    with pytest.raises(ValueError, match="https"):
        again.add("custom", "k" * 30, base_url="http://example.com")
    assert again.remove(e["id"]) and again.listing() == []


def test_switches_key_when_one_is_getting_low_and_comes_back_after_the_reset(tmp_path):
    clock = Clock()
    p, net = pool(tmp_path, clock)
    a = "gsk_" + "a" * 40
    b = "sk-or-" + "b" * 40
    p.add("groq", a, model="m1")
    p.add("openrouter", b, model="m2:free")
    eng = PoolEngine(p)
    low = {"x-ratelimit-remaining-requests": "3", "x-ratelimit-limit-requests": "1000",
           "x-ratelimit-reset-requests": "2h0m0s"}
    net.chat[a] = [ok("one", {"x-ratelimit-remaining-requests": "500", "x-ratelimit-limit-requests": "1000"}),
                   ok("two", low), ok("five")]
    net.chat[b] = [ok("three"), ok("four")]
    assert eng.generate(REQ).text == "one"
    assert eng.generate(REQ).text == "two"         # Groq says only 3 left: its group rests before it runs dry
    assert "nearly used" in p.listing()[0]["status"]
    assert eng.generate(REQ).text == "three" and eng.generate(REQ).text == "four"
    clock.t += 2 * 3600 + 1                          # its reset time passes
    assert eng.generate(REQ).text == "five"


def test_counts_its_own_use_where_the_service_says_nothing(tmp_path):
    clock = Clock()
    p, net = pool(tmp_path, clock)
    a, b = "AIza" + "a" * 35, "AIza" + "b" * 35
    p.add("gemini", a, model="gemini-flash", account="project one", daily_limit=40)
    p.add("gemini", b, model="gemini-flash", account="project two")
    eng = PoolEngine(p)
    net.chat[a] = [ok(str(i)) for i in range(50)]
    net.chat[b] = [ok("other")]
    for i in range(38):
        assert eng.generate(REQ).text == str(i)
    assert eng.generate(REQ).text == "other"      # 2 of 40 left: switched before the limit
    clock.t = next_day("gemini", clock.t) + 5      # midnight Pacific: back to the first project
    assert eng.generate(REQ).text == "38"


def test_keys_on_one_account_share_a_limit(tmp_path):
    p, net = pool(tmp_path)
    a, b, c = ("gsk_" + ch * 40 for ch in "abc")
    p.add("groq", a, model="m")
    p.add("groq", b, model="m")                    # same organization: same limit
    p.add("groq", c, model="m", account="second org")
    eng = PoolEngine(p)
    net.chat[a] = [limited(daily=True)]
    net.chat[b] = [ok("never used")]
    net.chat[c] = [ok("other org")]
    assert eng.generate(REQ).text == "other org"
    assert [k for k, *_ in net.posts] == [a, c]
    assert all("used up" in k["status"] for k in p.listing()[:2])


def test_minute_limits_rest_briefly_and_refused_keys_are_set_aside(tmp_path):
    clock = Clock()
    p, net = pool(tmp_path, clock)
    a, b = "gsk_" + "a" * 40, "sk-or-" + "b" * 40
    p.add("groq", a, model="m")
    p.add("openrouter", b, model="m:free")
    eng = PoolEngine(p)
    net.chat[a] = [limited(), ok("back")]
    net.chat[b] = [Resp(401, {"error": {"message": "invalid key"}})]
    with pytest.raises(LLMError) as err:
        eng.generate(REQ)
    assert err.value.kind == "rate"
    assert p.listing()[1]["status"].startswith("refused")
    clock.t += 3
    assert eng.generate(REQ).text == "back"


def test_check_picks_a_free_model_and_reads_openrouters_allowance(tmp_path):
    clock = Clock()
    p, net = pool(tmp_path, clock)
    k = "sk-or-" + "k" * 40
    e = p.add("openrouter", k)
    net.models[k] = Resp(200, {"data": [{"id": "paid/big"}, {"id": "google/gemma-4-31b-it:free"}]})
    net.key_info[k] = {"free_model_daily_requests": {"used": 998, "limit": 1000, "remaining": 2},
                       "limit_remaining": None}
    out = p.check(e["id"])
    assert out["model"] == "google/gemma-4-31b-it:free" and out["models"] == 2
    assert "nearly used (2 left)" in out["status"] and p.ready() == []
    bad = p.add("groq", "gsk_" + "z" * 40)
    with pytest.raises(LLMError, match="refused"):
        p.check(bad["id"])


def test_brain_uses_the_extra_keys_after_gemini_and_never_logs_a_key(tmp_path, caplog):
    p, net = pool(tmp_path)
    k = "gsk_" + "s" * 40
    p.add("groq", k, model="m")
    net.chat[k] = [ok("from the pool")]

    class DownGemini:
        _config = object()

        def generate(self, request):
            raise LLMError("quota", kind="rate")

    brain = Brain(DownGemini(), None, lambda: {"brain": "gemini"}, pool=PoolEngine(p))
    assert brain.order()[:2] == [GEMINI, POOL]
    caplog.set_level(logging.DEBUG)
    assert brain.generate(REQ).text == "from the pool" and brain.current == POOL
    assert brain.status()["extra_keys"] == "1 of 1 keys ready"
    assert k not in caplog.text
    assert Brain(DownGemini(), None, lambda: {}, pool=PoolEngine(pool(tmp_path / "x")[0])).order()[:2] != [GEMINI, POOL]


def test_reads_groq_and_openrouter_limit_headers():
    assert read_limits({"x-ratelimit-remaining-requests": "14370", "x-ratelimit-limit-requests": "14400",
                        "x-ratelimit-reset-requests": "2m59.56s", "x-ratelimit-remaining-tokens": "17997",
                        "x-ratelimit-limit-tokens": "18000", "x-ratelimit-reset-tokens": "7.66s"}) == {
        "remaining_requests": 14370, "limit_requests": 14400, "remaining_tokens": 17997, "limit_tokens": 18000,
        "reset_requests_s": pytest.approx(179.56), "reset_tokens_s": pytest.approx(7.66)}
    assert read_limits({"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "50",
                        "X-RateLimit-Reset": "1795000000000"}) == {"remaining_requests": 0, "limit_requests": 50,
                                                                   "reset_at": 1795000000.0}
    assert read_limits({}) == {} and read_limits({"x-ratelimit-reset-requests": "soon"}) == {}
