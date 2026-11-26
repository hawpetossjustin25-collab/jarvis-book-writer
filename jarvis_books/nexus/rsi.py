"""OS1 self-improvement (RSI): measure, propose, test in isolation, keep only what is measurably better, watch
for regressions and roll back.

Search: the index makes its own exam. Each sampled page gives two known-item questions (its title, and a few of
its rarest words); the right answer is that page. Questions are split by URL hash into a tuning half and a sealed
half that candidates are never tuned on. A challenger set of ranking weights replaces the champion only if it beats
it on the tuning half *and* the sealed half, without losing recall or getting much slower.

Crawl: the number of pages fetched in parallel is tried on real crawls (champion/challenger over actual runs) and
kept only when pages per minute go up without more failures. Politeness (per-host delay, robots.txt) is never tuned.

Every candidate, decision and rollback is kept in rsi_versions, so history is complete and any change can be undone.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from jarvis_books.nexus.search import RankWeights, SearchEngine, parse_query

log = logging.getLogger(__name__)

STOP = set("""the and for that with this from have are was were been will would could should their there about
into which when what your more than then them they these those also some such only other over most very just
like does each many much where while page pages site home menu click here read more""".split())
_WORD = re.compile(r"[a-z][a-z]{3,}")
CONCURRENCY = (1, 8)


@dataclass(frozen=True)
class Question:
    query: str
    target: str  # the url that should come back first
    kind: str  # "title" or "content"
    sealed: bool


@dataclass
class Score:
    mrr: float
    hit1: float
    hit10: float
    mean_ms: float
    p95_ms: float
    n: int

    def to_dict(self) -> dict:
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in self.__dict__.items()}


def _sealed(url: str) -> bool:
    return hashlib.sha256(url.encode()).digest()[0] % 2 == 1


def build_exam(store, max_pages: int = 80) -> list[Question]:
    rows = store.db.query(
        "SELECT r.url, r.final_url, r.title, substr(r.text, 1, 20000) AS text FROM resources r "
        "WHERE r.status='fetched' AND r.duplicate_of IS NULL AND length(r.text) > 200 AND r.content_hash IN "
        "(SELECT content_hash FROM resources WHERE status='fetched' GROUP BY content_hash HAVING count(*) = 1) "
        "ORDER BY r.id")
    if len(rows) > max_pages:  # an even spread over the whole index, the same every time
        step = len(rows) / max_pages
        rows = [rows[int(i * step)] for i in range(max_pages)]
    docs = [(r, Counter(w for w in _WORD.findall((r["text"] or "").lower()) if w not in STOP)) for r in rows]
    df = Counter(w for _, words in docs for w in words)
    titles = Counter(clean_title(r["title"] or "") for r in rows)
    exam = []
    for row, words in docs:
        url = row["final_url"] or row["url"]
        sealed = _sealed(url)
        title = clean_title(row["title"] or "")
        if len(_WORD.findall(title.lower())) >= 2 and titles[title] == 1:
            exam.append(Question(title, url, "title", sealed))
        rare = sorted((w for w, c in words.items() if c >= 2 and df[w] <= max(1, len(docs) // 10)),
                      key=lambda w: (df[w], -words[w], w))[:4]
        if len(rare) >= 3:
            exam.append(Question(" ".join(rare), url, "content", sealed))
    return exam


def clean_title(title: str) -> str:
    return re.split(r"\s+[|\-–—]\s+", title.strip())[0].strip()


class QueryVectors:
    """The exam's question vectors, fetched once per improvement run in batches. Every candidate asks the same
    questions, so asking Gemini again for each would cost thousands of calls and the free quota. A question whose
    vector couldn't be fetched has no meaning search for any candidate, so the comparison stays fair."""

    def __init__(self, embedder, questions: list[Question], batch: int = 64):
        self.model = embedder.model
        self.floor = getattr(embedder, "floor", 0.3)
        self.vectors: dict[str, list[float]] = {}
        texts = list(dict.fromkeys(t for t in (parse_query(q.query).text for q in questions) if t))
        for i in range(0, len(texts), batch):
            part = texts[i:i + batch]
            try:
                self.vectors.update(zip(part, embedder.embed(part, "RETRIEVAL_QUERY")))
            except Exception as exc:
                log.info("rsi: %d question vectors unavailable (%s)", len(part), type(exc).__name__)

    def embed(self, texts: list[str], task: str = "RETRIEVAL_QUERY") -> list[list[float]]:
        missing = [t for t in texts if t not in self.vectors]
        if task != "RETRIEVAL_QUERY" or missing:
            raise LookupError(f"no stored vector for {len(missing) or len(texts)} text(s)")
        return [self.vectors[t] for t in texts]


def evaluate(store, weights: RankWeights, questions: list[Question], embedder=None) -> Score:
    engine = SearchEngine(store, embedder)
    engine.weights = weights
    rr, hit1, hit10, times = [], 0, 0, []
    for q in questions:
        started = time.perf_counter()
        results = engine.search(q.query, 10, use_cache=False, record=False)["results"]
        times.append((time.perf_counter() - started) * 1000)
        urls = [r["url"] for r in results]
        if q.target in urls:
            pos = urls.index(q.target) + 1
            rr.append(1.0 / pos)
            hit1 += pos == 1
            hit10 += 1
        else:
            rr.append(0.0)
    n = len(questions) or 1
    times.sort()
    return Score(sum(rr) / n, hit1 / n, hit10 / n, statistics.fmean(times) if times else 0.0,
                 times[min(len(times) - 1, int(0.95 * len(times)))] if times else 0.0, len(questions))


def candidates(champion: RankWeights, count: int, rng: random.Random) -> list[RankWeights]:
    """Neighbours of the champion: one to three weights each scaled by 0.5x to 2x (kept inside safe limits)."""
    keys = list(RankWeights.LIMITS)
    out, seen = [], {json.dumps(champion.to_dict(), sort_keys=True)}
    for _ in range(count * 4):
        d = champion.to_dict()
        for key in rng.sample(keys, rng.randint(1, 3)):
            d[key] = d[key] * rng.choice((0.5, 0.7, 1.4, 2.0)) if d[key] else rng.choice((0.2, 0.5))
        cand = RankWeights.from_dict(d)
        sig = json.dumps(cand.to_dict(), sort_keys=True)
        if sig not in seen:
            seen.add(sig)
            out.append(cand)
        if len(out) >= count:
            break
    return out


class Improver:
    def __init__(self, os1, min_questions: int = 12, min_gain: float = 0.01, clock=time.time):
        self.os1, self.store, self.db = os1, os1.store, os1.store.db
        self.min_questions, self.min_gain, self.clock = min_questions, min_gain, clock
        self.default_concurrency = os1.config.concurrency

    # ---- version history
    def _champion(self, kind: str):
        return self.db.query_one("SELECT * FROM rsi_versions WHERE kind=? AND status='champion' ORDER BY id DESC "
                                 "LIMIT 1", (kind,))

    def _record(self, kind: str, params: dict, metrics: dict, status: str, reason: str,
                parent: Optional[int]) -> int:
        now = self.clock()
        return self.db.execute("INSERT INTO rsi_versions(kind, params, metrics, status, reason, parent, created_at, "
                               "decided_at) VALUES (?,?,?,?,?,?,?,?)",
                               (kind, json.dumps(params, sort_keys=True), json.dumps(metrics), status, reason,
                                parent, now, None if status == "trial" else now)).lastrowid

    def _promote(self, kind: str, version_id: int, reason: str) -> None:
        with self.db.transaction() as c:
            c.execute("UPDATE rsi_versions SET status='retired' WHERE kind=? AND status='champion'", (kind,))
            c.execute("UPDATE rsi_versions SET status='champion', reason=?, decided_at=? WHERE id=?",
                      (reason, self.clock(), version_id))
        self.apply()

    def apply(self) -> None:
        """Puts the current champions to work in OS1 (called at startup and after every decision)."""
        row = self._champion("search")
        self.os1.search_engine.weights = RankWeights.from_dict(json.loads(row["params"])) if row else RankWeights()
        trial = self._trial("crawl")
        row = trial or self._champion("crawl")
        if row:
            c = int(json.loads(row["params"])["concurrency"])
            self.os1.config = self.os1.config.with_(concurrency=min(CONCURRENCY[1], max(CONCURRENCY[0], c)))

    def _trial(self, kind: str):
        return self.db.query_one("SELECT * FROM rsi_versions WHERE kind=? AND status='trial' ORDER BY id DESC LIMIT 1",
                                 (kind,))

    def rollback(self, kind: str, reason: str = "rolled back by request") -> dict:
        current = self._champion(kind)
        if current is None:
            return {"kind": kind, "rolled_back": False, "reason": "already on the built-in settings"}
        previous = self.db.query_one("SELECT * FROM rsi_versions WHERE kind=? AND status='retired' AND id<? "
                                     "ORDER BY id DESC LIMIT 1", (kind, current["id"]))
        with self.db.transaction() as c:
            c.execute("UPDATE rsi_versions SET status='rolled_back', reason=?, decided_at=? WHERE id=?",
                      (reason, self.clock(), current["id"]))
            c.execute("UPDATE rsi_versions SET status='rejected', reason='trial ended by rollback', decided_at=? "
                      "WHERE kind=? AND status='trial'", (self.clock(), kind))
            if previous is not None:
                c.execute("UPDATE rsi_versions SET status='champion', decided_at=? WHERE id=?",
                          (self.clock(), previous["id"]))
        if kind == "crawl" and previous is None:
            self.os1.config = self.os1.config.with_(concurrency=self.default_concurrency)
        self.apply()
        return {"kind": kind, "rolled_back": True, "from": current["id"],
                "to": previous["id"] if previous is not None else "built-in settings"}

    def history(self, kind: Optional[str] = None, limit: int = 20) -> list[dict]:
        rows = self.db.query("SELECT * FROM rsi_versions " + ("WHERE kind=? " if kind else "") +
                             "ORDER BY id DESC LIMIT ?", ((kind, limit) if kind else (limit,)))
        return [{**dict(r), "params": json.loads(r["params"]), "metrics": json.loads(r["metrics"])} for r in rows]

    def status(self) -> dict:
        out = {}
        for kind in ("search", "crawl"):
            row = self._champion(kind)
            trial = self._trial(kind)
            out[kind] = {"champion": json.loads(row["params"]) if row else "built-in",
                         "champion_metrics": json.loads(row["metrics"]) if row else {},
                         "trial": json.loads(trial["params"]) if trial else None,
                         "versions": self.db.query_one("SELECT count(*) AS n FROM rsi_versions WHERE kind=?",
                                                       (kind,))["n"]}
        return out

    # ---- search ranking
    def improve_search(self, rounds: int = 1, per_round: int = 6, seed: Optional[int] = None) -> dict:
        exam = build_exam(self.store)
        tune = [q for q in exam if not q.sealed]
        sealed = [q for q in exam if q.sealed]
        if len(tune) < self.min_questions or len(sealed) < self.min_questions:
            return {"kind": "search", "status": "not_enough_data", "questions": len(exam),
                    "needed": 2 * self.min_questions}
        embedder = self.os1.search_engine.embedder
        if embedder is not None:
            embedder = QueryVectors(embedder, exam)
        row = self._champion("search")
        champ = RankWeights.from_dict(json.loads(row["params"])) if row else RankWeights()
        champ_id = row["id"] if row else None
        champ_tune = evaluate(self.store, champ, tune, embedder)
        champ_sealed = evaluate(self.store, champ, sealed, embedder)
        monitor = self._monitor(row, champ_sealed, sealed, embedder)
        if monitor:
            return monitor
        rng = random.Random(seed if seed is not None else int(self.clock()))
        tried = kept = 0
        log_rows = []
        for _ in range(max(1, rounds)):
            best, best_tune = None, champ_tune
            for cand in candidates(champ, per_round, rng):
                tried += 1
                score = evaluate(self.store, cand, tune, embedder)
                if score.mrr > best_tune.mrr + self.min_gain and score.hit10 >= best_tune.hit10:
                    best, best_tune = cand, score
            if best is None:
                log_rows.append("no candidate beat the champion on the tuning questions")
                continue
            check = evaluate(self.store, best, sealed, embedder)
            metrics = {"tune": best_tune.to_dict(), "sealed": check.to_dict(),
                       "champion_tune": champ_tune.to_dict(), "champion_sealed": champ_sealed.to_dict()}
            slower = check.p95_ms > champ_sealed.p95_ms * 1.5 + 20
            if check.mrr >= champ_sealed.mrr + self.min_gain / 2 and check.hit10 >= champ_sealed.hit10 and not slower:
                vid = self._record("search", best.to_dict(), metrics, "candidate", "", champ_id)
                self._promote("search", vid, f"sealed MRR {champ_sealed.mrr:.3f} -> {check.mrr:.3f}")
                champ, champ_id, champ_tune, champ_sealed = best, vid, best_tune, check
                kept += 1
                log_rows.append(f"kept: sealed MRR {metrics['champion_sealed']['mrr']} -> {check.mrr:.4f}")
            else:
                why = "slower" if slower else "didn't hold up on the sealed questions"
                self._record("search", best.to_dict(), metrics, "rejected", why, champ_id)
                log_rows.append(f"rejected: {why}")
        return {"kind": "search", "status": "improved" if kept else "unchanged", "questions": len(exam),
                "candidates_tried": tried, "kept": kept, "champion": champ.to_dict(),
                "sealed": champ_sealed.to_dict(), "notes": log_rows}

    def _monitor(self, row, champ_sealed: Score, sealed: list[Question], embedder) -> Optional[dict]:
        """If the index grew and the champion now does worse than what it replaced, undo it."""
        if row is None:
            return None
        parent = self.db.query_one("SELECT params FROM rsi_versions WHERE id=?", (row["parent"],)) \
            if row["parent"] else None
        before = RankWeights.from_dict(json.loads(parent["params"])) if parent else RankWeights()
        old = evaluate(self.store, before, sealed, embedder)
        if old.mrr > champ_sealed.mrr + self.min_gain:
            done = self.rollback("search", f"regressed on newer pages: MRR {champ_sealed.mrr:.3f} vs "
                                           f"{old.mrr:.3f} for the settings it replaced")
            return {"kind": "search", "status": "rolled_back", **done, "sealed_before": old.to_dict(),
                    "sealed_champion": champ_sealed.to_dict()}
        return None

    # ---- crawl speed
    def _runs_since(self, since: float, concurrency: int) -> list[dict]:
        out = []
        for r in self.db.query("SELECT config, stats FROM crawl_runs WHERE status IN ('done', 'limit', 'stopped') "
                               " AND started_at >= ? ORDER BY id", (since,)):
            cfg, stats = json.loads(r["config"] or "{}"), json.loads(r["stats"] or "{}")
            tried = stats.get("pages_fetched", 0) + stats.get("failed", 0)
            if cfg.get("concurrency") == concurrency and tried >= 5:
                out.append({"ppm": float(stats.get("pages_per_minute", 0.0)),
                            "fail": stats.get("failed", 0) / tried, "pages": tried})
        return out

    @staticmethod
    def _summary(runs: list[dict]) -> dict:
        pages = sum(r["pages"] for r in runs) or 1
        return {"runs": len(runs), "pages": pages,
                "pages_per_minute": round(sum(r["ppm"] * r["pages"] for r in runs) / pages, 3),
                "failure_rate": round(sum(r["fail"] * r["pages"] for r in runs) / pages, 4)}

    def improve_crawl(self) -> dict:
        trial = self._trial("crawl")
        champ = self._champion("crawl")
        base_c = int(json.loads(champ["params"])["concurrency"]) if champ else self.default_concurrency
        if trial is not None:
            c = int(json.loads(trial["params"])["concurrency"])
            runs = self._runs_since(trial["created_at"], c)
            if not runs:
                return {"kind": "crawl", "status": "trial_running", "trial": c,
                        "note": "waiting for a finished crawl with the trial setting"}
            got = self._summary(runs)
            base = json.loads(trial["metrics"]).get("baseline", {})
            better = got["pages_per_minute"] >= base.get("pages_per_minute", 0) * 1.1
            safe = got["failure_rate"] <= base.get("failure_rate", 0) + 0.05
            metrics = {"baseline": base, "trial": got}
            self.db.execute("UPDATE rsi_versions SET metrics=? WHERE id=?", (json.dumps(metrics), trial["id"]))
            if better and safe:
                self._promote("crawl", trial["id"], f"{base.get('pages_per_minute')} -> "
                                                    f"{got['pages_per_minute']} pages a minute")
                return {"kind": "crawl", "status": "improved", "concurrency": c, **metrics}
            reason = "more failures" if not safe else "not faster"
            self.db.execute("UPDATE rsi_versions SET status='rejected', reason=?, decided_at=? WHERE id=?",
                            (reason, self.clock(), trial["id"]))
            self.os1.config = self.os1.config.with_(concurrency=base_c)
            self.apply()
            return {"kind": "crawl", "status": "rolled_back", "concurrency": base_c, "reason": reason, **metrics}
        since = champ["decided_at"] if champ else 0.0
        runs = self._runs_since(since, base_c)
        if not runs:
            return {"kind": "crawl", "status": "not_enough_data",
                    "note": f"needs a finished crawl of 5+ pages at {base_c} at a time"}
        base = self._summary(runs)
        if base["failure_rate"] > 0.25 and base_c > CONCURRENCY[0]:
            nxt = base_c - 1
        elif base["failure_rate"] < 0.10 and base_c < CONCURRENCY[1]:
            nxt = base_c + 1
        else:
            return {"kind": "crawl", "status": "unchanged", "concurrency": base_c, "baseline": base}
        rejected = self.db.query_one("SELECT count(*) AS n FROM rsi_versions WHERE kind='crawl' AND status="
                                     "'rejected' AND params=? AND decided_at > ?",
                                     (json.dumps({"concurrency": nxt}), self.clock() - 7 * 86400))["n"]
        if rejected:
            return {"kind": "crawl", "status": "unchanged", "concurrency": base_c, "baseline": base,
                    "note": f"{nxt} at a time was already tried this week and lost"}
        self._record("crawl", {"concurrency": nxt}, {"baseline": base}, "trial",
                     f"trying {nxt} pages at a time", champ["id"] if champ else None)
        self.apply()
        return {"kind": "crawl", "status": "trial_started", "from": base_c, "trial": nxt, "baseline": base}

