import json
import random

import pytest

from jarvis_books.nexus import rsi
from jarvis_books.nexus.os1 import OS1
from jarvis_books.nexus.rsi import Improver, Question, Score, build_exam, candidates, clean_title, evaluate
from jarvis_books.nexus.search import HashedEmbedder, RankWeights
from nexus_site import FAST, FakeWeb, page

HUB = "https://wiki.example/"
LETTERS = "bcdfghjklmnpqrstvwxz"


def word(i: int, salt: str) -> str:
    out = ""
    for _ in range(3):
        out += LETTERS[i % len(LETTERS)] + "aeiou"[i % 5]
        i //= len(LETTERS)
    return salt + out


def site(n: int = 40) -> dict:
    routes, links = {}, []
    filler = "the history of this subject is long and the details matter to everyone who studies it. "
    for i in range(n):
        url = f"{HUB}topic/{i}"
        a, b, c, d = word(i, "zo"), word(i, "ka"), word(i, "mi"), word(i, "ru")
        body = f"{filler} {a} {a} {b} {b} {c} {c} {d} {d} {filler} {filler}"
        routes[url] = page(f"Notes on {a} {b} - Wiki", body)
        links.append((url, f"page {i}"))
    # the hub mentions every word once, has the best link rank and would win under bad weights
    words_all = " ".join(word(i, s) for i in range(n) for s in ("zo", "ka"))
    routes[HUB] = page("Wiki index", filler * 3 + words_all, links=tuple(links))
    return routes


@pytest.fixture
def os1(tmp_path):
    o = OS1(tmp_path, FAST.with_(max_pages=100, max_depth=2), fetcher_factory=FakeWeb(site()).factory())
    assert o.call("crawl", {"seeds": [HUB]}).ok
    yield o
    o.close()


def test_exam_comes_from_the_index_with_a_sealed_half(os1):
    exam = build_exam(os1.store)
    assert {q.kind for q in exam} == {"title", "content"}
    assert len(exam) >= 70 and 0 < sum(q.sealed for q in exam) < len(exam)
    q = next(q for q in exam if q.kind == "title" and q.target.endswith("/topic/3"))
    assert q.query == "Notes on " + word(3, "zo") + " " + word(3, "ka")
    assert all(q.target != HUB for q in exam if q.kind == "content")  # the hub's words are not rare to it
    assert build_exam(os1.store) == exam  # the same exam every time for the same index
    assert clean_title("Python | Docs") == "Python" and clean_title("A - B - C") == "A"


def test_default_ranking_scores_well_and_searches_leave_no_trace(os1):
    before = os1.store.db.query_one("SELECT count(*) AS n FROM search_history")["n"]
    score = evaluate(os1.store, RankWeights(), build_exam(os1.store))
    assert score.mrr > 0.9 and score.hit10 == 1.0 and score.n >= 70
    assert os1.store.db.query_one("SELECT count(*) AS n FROM search_history")["n"] == before


def test_weights_are_clamped_and_candidates_are_new_neighbours():
    w = RankWeights.from_dict({"title_col": 999, "rrf_k": -5, "bogus": 1})
    assert w.title_col == 30.0 and w.rrf_k == 1.0
    found = candidates(RankWeights(), 8, random.Random(1))
    assert len(found) == 8 and len({json.dumps(c.to_dict()) for c in found}) == 8
    assert all(c != RankWeights() for c in found)
    for c in found:
        for key, (lo, hi) in RankWeights.LIMITS.items():
            assert lo <= getattr(c, key) <= hi


def test_a_bad_champion_never_gets_worse(os1):
    bad = RankWeights.from_dict({"title_col": 0.5, "text_col": 10, "link_rank": 3.0, "title_hits": 0.0,
                                 "words": 0.1, "meaning": 3.0, "rrf_k": 200})
    imp = os1.improver
    vid = imp._record("search", bad.to_dict(), {}, "candidate", "test", None)
    imp._promote("search", vid, "test start")
    imp.min_gain = 0.0  # monitoring would otherwise undo this deliberately bad start right away
    imp._monitor = lambda *a: None
    start = evaluate(os1.store, bad, [q for q in build_exam(os1.store) if q.sealed], os1.search_engine.embedder)
    out = imp.improve_search(rounds=4, per_round=8, seed=3)
    assert out["status"] in ("improved", "unchanged")
    now = evaluate(os1.store, os1.search_engine.weights, [q for q in build_exam(os1.store) if q.sealed], os1.search_engine.embedder)
    assert now.mrr >= start.mrr
    if out["kept"]:
        assert now.mrr > start.mrr
        hist = imp.history("search")
        assert hist[0]["status"] == "champion" and hist[0]["parent"] is not None
        assert any(h["status"] == "retired" for h in hist)


def test_challengers_must_win_the_sealed_half_too(os1, monkeypatch):
    def fake_eval(store, weights, questions, embedder=None):
        sealed = questions[0].sealed
        better = weights != RankWeights()
        return Score(0.5 if sealed or not better else 0.9, 0.5, 1.0, 1.0, 2.0, len(questions))

    monkeypatch.setattr(rsi, "evaluate", fake_eval)
    out = os1.improver.improve_search(rounds=1, per_round=3, seed=1)
    assert out["status"] == "unchanged" and out["kept"] == 0
    assert os1.improver.history("search")[0]["status"] == "rejected"
    assert "sealed" in os1.improver.history("search")[0]["reason"]
    assert os1.search_engine.weights == RankWeights()


def test_slower_challengers_are_rejected(os1, monkeypatch):
    def fake_eval(store, weights, questions, embedder=None):
        better = weights != RankWeights()
        return Score(0.9 if better else 0.5, 0.5, 1.0, 1.0, 500.0 if better else 2.0, len(questions))

    monkeypatch.setattr(rsi, "evaluate", fake_eval)
    out = os1.improver.improve_search(rounds=1, per_round=3, seed=1)
    assert out["kept"] == 0 and os1.improver.history("search")[0]["reason"] == "slower"


def test_winning_challenger_is_kept_persisted_and_can_be_rolled_back(os1, tmp_path, monkeypatch):
    def fake_eval(store, weights, questions, embedder=None):
        return Score(0.9 if weights.title_col != 10.0 else 0.5, 0.5, 1.0, 1.0, 2.0, len(questions))

    monkeypatch.setattr(rsi, "evaluate", fake_eval)
    monkeypatch.setattr(rsi, "candidates", lambda champ, n, rng: [RankWeights(title_col=12.0)])
    out = os1.improver.improve_search(seed=1)
    assert out["status"] == "improved" and os1.search_engine.weights.title_col == 12.0
    again = OS1(tmp_path, FAST)
    assert again.search_engine.weights.title_col == 12.0  # a restarted Jarvis keeps the winner
    again.close()
    resp = os1.call("rsi", {"action": "rollback", "kind": "search"})
    assert resp.ok and resp.results[0]["rolled_back"] and os1.search_engine.weights == RankWeights()
    assert os1.improver.history("search")[0]["status"] == "rolled_back"
    assert os1.call("rsi", {"action": "rollback", "kind": "search"}).results[0]["rolled_back"] is False


def test_monitor_rolls_back_a_champion_that_regressed(os1, monkeypatch):
    imp = os1.improver
    imp._promote("search", imp._record("search", RankWeights(title_col=12.0).to_dict(), {}, "candidate", "", None),
                 "start")
    assert os1.search_engine.weights.title_col == 12.0

    def fake_eval(store, weights, questions, embedder=None):  # newer pages: the old champion now loses
        return Score(0.5 if weights.title_col == 12.0 else 0.9, 0.5, 1.0, 1.0, 2.0, len(questions))

    monkeypatch.setattr(rsi, "evaluate", fake_eval)
    out = imp.improve_search(seed=1)
    assert out["status"] == "rolled_back" and os1.search_engine.weights == RankWeights()
    assert "regressed" in imp.history("search")[0]["reason"]


def test_real_round_on_real_pages_never_makes_search_worse(os1):
    emb = os1.search_engine.embedder
    sealed = [q for q in build_exam(os1.store) if q.sealed]
    before = evaluate(os1.store, RankWeights(), sealed, emb)
    out = os1.improver.improve_search(rounds=2, per_round=6, seed=5)
    assert out["status"] in ("improved", "unchanged") and out["candidates_tried"] == 12
    assert evaluate(os1.store, os1.search_engine.weights, sealed, emb).mrr >= before.mrr


def test_not_enough_pages(tmp_path):
    o = OS1(tmp_path, FAST)
    out = o.call("improve", {"target": "search"}).results[0]
    assert out["status"] == "not_enough_data"
    o.close()


def add_run(os1, concurrency, ppm, fetched=20, failed=0, status="done"):
    rid = os1.store.start_run("t", [HUB], {"concurrency": concurrency})
    os1.store.finish_run(rid, status, {"pages_fetched": fetched, "failed": failed, "pages_per_minute": ppm})


def test_crawl_speed_trial_kept_when_faster(tmp_path):
    clock = [1000.0]
    o = OS1(tmp_path, FAST.with_(concurrency=3))
    o.store.clock = o.improver.clock = lambda: clock[0]
    imp = o.improver
    assert imp.improve_crawl()["status"] == "not_enough_data"
    add_run(o, 3, 60.0)
    started = imp.improve_crawl()
    assert started["status"] == "trial_started" and started["trial"] == 4 and o.config.concurrency == 4
    assert imp.improve_crawl()["status"] == "trial_running"
    clock[0] += 10
    add_run(o, 4, 80.0)
    kept = imp.improve_crawl()
    assert kept["status"] == "improved" and o.config.concurrency == 4
    assert imp.status()["crawl"]["champion"] == {"concurrency": 4}
    again = OS1(tmp_path, FAST.with_(concurrency=3))
    assert again.config.concurrency == 4
    again.close()
    o.close()


def test_crawl_speed_trial_rolled_back_when_not_faster_or_more_failures(tmp_path):
    clock = [1000.0]
    o = OS1(tmp_path, FAST.with_(concurrency=3))
    o.store.clock = o.improver.clock = lambda: clock[0]
    imp = o.improver
    add_run(o, 3, 60.0)
    imp.improve_crawl()
    clock[0] += 10
    add_run(o, 4, 120.0, fetched=10, failed=10)
    out = imp.improve_crawl()
    assert out["status"] == "rolled_back" and out["reason"] == "more failures" and o.config.concurrency == 3
    clock[0] += 10
    add_run(o, 3, 60.0)
    assert "already tried this week" in imp.improve_crawl()["note"]  # a loser isn't retried at once
    o.close()


def test_crawl_slows_down_when_sites_fail(tmp_path):
    o = OS1(tmp_path, FAST.with_(concurrency=3))
    add_run(o, 3, 30.0, fetched=10, failed=10)
    assert o.improver.improve_crawl()["trial"] == 2
    o.close()


def test_rsi_contract_operations(os1):
    assert os1.call("rsi", {"action": "status"}).results[0]["search"]["champion"] == "built-in"
    assert os1.call("rsi", {"action": "rollback"}).error_code == "invalid_request"
    assert os1.call("improve", {"target": "warp"}).error_code == "invalid_request"
    both = os1.call("improve", {"target": "both", "seed": 2})
    assert both.ok and [r["kind"] for r in both.results] == ["search", "crawl"]
    assert isinstance(os1.call("rsi", {"action": "history"}).results, list)


class CountingEmbedder(HashedEmbedder):
    def __init__(self):
        super().__init__()
        self.calls = {}

    def embed(self, texts, task=""):
        self.calls[task] = self.calls.get(task, 0) + 1
        return super().embed(texts, task)


def test_a_round_fetches_each_question_vector_once(tmp_path):
    emb = CountingEmbedder()
    o = OS1(tmp_path, FAST.with_(max_pages=100, max_depth=2), embedder=emb,
            fetcher_factory=FakeWeb(site()).factory())
    try:
        assert o.call("crawl", {"seeds": [HUB]}).ok and o.call("embed", {}).ok
        assert o.search_engine.embedder is emb
        emb.calls.clear()
        out = o.improver.improve_search(rounds=2, per_round=6, seed=5)
        assert out["candidates_tried"] == 12
        exam = build_exam(o.store)
        assert len(exam) > 64
        assert emb.calls == {"RETRIEVAL_QUERY": -(-len(exam) // 64)}  # batches of 64 once, not one per search
    finally:
        o.close()


def test_question_vectors_that_could_not_be_fetched_mean_no_meaning_search():
    class Down(HashedEmbedder):
        def embed(self, texts, task=""):
            raise ConnectionError("offline")

    qv = rsi.QueryVectors(Down(), [Question("alpha beta", "https://x/1", "title", False)])
    assert qv.model == HashedEmbedder.model and qv.floor == HashedEmbedder.floor
    with pytest.raises(LookupError):
        qv.embed(["alpha beta"], "RETRIEVAL_QUERY")
    ok = rsi.QueryVectors(HashedEmbedder(), [Question("alpha beta", "https://x/1", "title", False)])
    assert ok.embed(["alpha beta"], "RETRIEVAL_QUERY") == HashedEmbedder().embed(["alpha beta"])
    with pytest.raises(LookupError):
        ok.embed(["something else"], "RETRIEVAL_QUERY")
