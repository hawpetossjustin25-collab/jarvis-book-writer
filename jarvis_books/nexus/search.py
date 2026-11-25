"""OS1 search: BM25 full-text over the resource graph, optional meaning (vector) search, link rank, filters,
query expansion, deduplication, caching and history."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol

import numpy as np

from jarvis_books.nexus.dedup import NEAR_BITS, hamming, to_unsigned
from jarvis_books.nexus.store import GraphStore
from jarvis_books.nexus.urls import in_domain

log = logging.getLogger(__name__)

_TOKEN = re.compile(r'(-?)(?:(site|type):(\S+)|"([^"]+)"|(\S+))')
_WORD = re.compile(r"\w+", re.UNICODE)
RRF_K = 60


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str], task: str) -> list[list[float]]: ...


@dataclass
class ParsedQuery:
    terms: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    site: Optional[str] = None
    resource_type: Optional[str] = None

    @property
    def text(self) -> str:
        return " ".join(self.phrases + self.terms)


def parse_query(raw: str) -> ParsedQuery:
    """Understands plain words, "exact phrases", -excluded words, site:example.com and type:article."""
    q = ParsedQuery()
    for neg, key, value, phrase, word in _TOKEN.findall(raw or ""):
        if key == "site":
            q.site = value.lower().lstrip(".")
        elif key == "type":
            q.resource_type = value.lower()
        elif phrase:
            words = _WORD.findall(phrase.lower())
            if words:
                (q.excluded if neg else q.phrases).append(" ".join(words))
        else:
            for w in _WORD.findall(word.lower()):
                (q.excluded if neg else q.terms).append(w)
    return q


def _quote(term: str) -> str:
    return '"' + term.replace('"', "") + '"'


def _edits1(word: str) -> set[str]:
    letters = "abcdefghijklmnopqrstuvwxyz0123456789"
    splits = [(word[:i], word[i:]) for i in range(len(word) + 1)]
    return ({a + b[1:] for a, b in splits if b} | {a + b[1] + b[0] + b[2:] for a, b in splits if len(b) > 1} |
            {a + c + b[1:] for a, b in splits if b for c in letters} | {a + c + b for a, b in splits for c in letters})


class SearchEngine:
    def __init__(self, store: GraphStore, embedder: Optional[Embedder] = None, cache_ttl_s: float = 900.0):
        self.store, self.embedder, self.cache_ttl_s = store, embedder, cache_ttl_s
        self.cache_hits = self.cache_misses = 0

    # ---- query expansion
    def _known(self, term: str) -> bool:
        return self.store.db.query_one("SELECT 1 FROM resource_vocab WHERE term=?", (term,)) is not None

    def _spelling(self, term: str) -> Optional[str]:
        """The most common indexed word one edit away from `term`, when `term` itself is not indexed."""
        if len(term) < 4 or term.isdigit() or self._known(term):
            return None
        options = list(_edits1(term))
        best, best_docs = None, 0
        for i in range(0, len(options), 500):
            chunk = options[i:i + 500]
            marks = ",".join("?" * len(chunk))
            row = self.store.db.query_one(f"SELECT term, doc FROM resource_vocab WHERE term IN ({marks}) "
                                          f"ORDER BY doc DESC LIMIT 1", tuple(chunk))
            if row and row[1] > best_docs:
                best, best_docs = row[0], row[1]
        return best

    def expand(self, q: ParsedQuery) -> tuple[str, list[str]]:
        """The FTS5 MATCH expression, plus a note of each spelling fix made."""
        fixes, parts = [], []
        for term in q.terms:
            alt = self._spelling(term)
            options = [_quote(term)]
            if len(term) >= 4:
                options.append(_quote(term) + "*")
            if alt:
                options.append(_quote(alt))
                fixes.append(f"{term} -> {alt}")
            parts.append("(" + " OR ".join(options) + ")" if len(options) > 1 else options[0])
        parts += [_quote(p) for p in q.phrases]
        expr = " AND ".join(parts)
        if expr and q.excluded:
            expr = f"({expr}) " + " ".join(f"NOT {_quote(x)}" for x in q.excluded)
        return expr, fixes

    # ---- the search
    def search(self, raw: str, max_results: int = 10, site: Optional[str] = None,
               resource_type: Optional[str] = None, use_cache: bool = True) -> dict:
        started = time.perf_counter()
        q = parse_query(raw)
        site = (site or q.site or "").lower().lstrip(".") or None
        kind = (resource_type or q.resource_type or "").lower() or None
        max_results = max(1, min(int(max_results), 100))
        filters = {"site": site, "type": kind, "max_results": max_results}
        key = hashlib.sha256(json.dumps([raw.strip().lower(), filters], sort_keys=True).encode()).hexdigest()
        if use_cache:
            hit = self.store.cached(key, self.cache_ttl_s)
            if hit is not None:
                self.cache_hits += 1
                elapsed = (time.perf_counter() - started) * 1000
                self.store.log_search(raw, filters, len(hit["results"]), elapsed, True)
                return {**hit, "cached": True, "elapsed_ms": round(elapsed, 2)}
        self.cache_misses += 1
        expr, fixes = self.expand(q)
        lexical = self._lexical(expr) if expr else []
        semantic = self._semantic(q.text) if q.text else []
        banned = set(self._lexical(" OR ".join(_quote(x) for x in q.excluded), 100_000)) if q.excluded else set()
        scores: dict[int, float] = {}
        for ranked in (lexical, semantic):
            for pos, rid in enumerate(r for r in ranked if r not in banned):
                scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + pos + 1)
        boost = ParsedQuery(q.terms + [fix.split(" -> ")[1] for fix in fixes], q.phrases)
        results = self._assemble(scores, boost, site, kind, max_results, set(lexical))
        out = {"query": raw, "expanded": expr, "spelling": fixes, "results": results,
               "matched": len(scores), "semantic": bool(semantic)}
        self.store.cache(key, out)
        elapsed = (time.perf_counter() - started) * 1000
        self.store.log_search(raw, filters, len(results), elapsed, False)
        return {**out, "cached": False, "elapsed_ms": round(elapsed, 2)}

    def _lexical(self, expr: str, limit: int = 300) -> list[int]:
        try:
            rows = self.store.db.query(
                "SELECT rowid FROM resource_fts WHERE resource_fts MATCH ? "
                "ORDER BY bm25(resource_fts, 10.0, 4.0, 1.0, 2.0) LIMIT ?", (expr, limit))
        except Exception as exc:  # sqlite3.OperationalError for an expression FTS5 can't parse
            log.warning("nexus search: bad query expression %r (%s)", expr, exc)
            return []
        return [r[0] for r in rows]

    def _semantic(self, text: str, limit: int = 100, floor: Optional[float] = None) -> list[int]:
        if self.embedder is None:
            return []
        floor = getattr(self.embedder, "floor", 0.3) if floor is None else floor
        ids, mat = self.store.vectors(self.embedder.model)
        if not len(ids):
            return []
        try:
            vec = np.asarray(self.embedder.embed([text], "RETRIEVAL_QUERY")[0], dtype=np.float32)
        except Exception as exc:
            log.info("nexus search: meaning search skipped (%s)", type(exc).__name__)
            return []
        if vec.shape[0] != mat.shape[1]:
            return []
        norms = np.linalg.norm(mat, axis=1) * (np.linalg.norm(vec) or 1.0)
        sims = mat @ vec / np.where(norms == 0, 1.0, norms)
        order = np.argsort(-sims)[:limit]
        return [int(ids[i]) for i in order if sims[i] >= floor]

    def _assemble(self, scores: dict[int, float], q: ParsedQuery, site: Optional[str], kind: Optional[str],
                  max_results: int, lexical: set[int]) -> list[dict]:
        if not scores:
            return []
        ids = list(scores)
        marks = ",".join("?" * len(ids))
        rows = {r["id"]: r for r in self.store.db.query(
            f"SELECT id, url, final_url, domain, site, title, description, substr(text, 1, 6000) AS text, "
            f"resource_type, rank, relevance, content_hash, simhash, last_visited, discovered_at, last_changed "
            f"FROM resources WHERE id IN ({marks}) AND status='fetched' AND duplicate_of IS NULL", tuple(ids))}
        words = q.terms + [w for p in q.phrases for w in p.split()]
        ranked = []
        for rid, base in scores.items():
            row = rows.get(rid)
            if row is None or not row["last_visited"]:
                continue
            if site and not (in_domain(row["domain"], site) or row["site"] == site):
                continue
            if kind and row["resource_type"] != kind and not (kind == "page" and row["resource_type"] == "article"):
                continue
            title = (row["title"] or "").lower()
            title_hits = sum(1 for w in set(words) if w in title) / max(1, len(set(words)))
            score = base * (1.0 + 0.5 * row["rank"] + 0.4 * title_hits + 0.2 * row["relevance"])
            ranked.append((score, row))
        ranked.sort(key=lambda x: -x[0])
        results, hashes, sims = [], set(), []
        for score, row in ranked:
            sim = to_unsigned(row["simhash"]) if row["simhash"] is not None else 0
            if row["content_hash"] in hashes or (sim and any(hamming(sim, s) <= NEAR_BITS for s in sims)):
                continue
            hashes.add(row["content_hash"])
            if sim:
                sims.append(sim)
            results.append(self._result(row, score, words, row["id"] in lexical))
            if len(results) >= max_results:
                break
        return results

    def _result(self, row, score: float, words: list[str], lexical: bool) -> dict:
        related = self.store.db.query(
            "SELECT r.url, r.title FROM edges e JOIN resources r ON r.id = e.dst WHERE e.src=? AND "
            "r.status='fetched' AND r.duplicate_of IS NULL AND e.rel IN ('link','next','prev') "
            "ORDER BY r.rank DESC LIMIT 3", (row["id"],))
        return {"title": row["title"] or row["url"], "url": row["final_url"] or row["url"], "domain": row["domain"],
                "description": row["description"], "snippet": snippet(row["text"], words),
                "type": row["resource_type"], "score": round(score, 6), "rank": round(row["rank"], 4),
                "match": "words" if lexical else "meaning", "discovered": row["discovered_at"],
                "last_checked": row["last_visited"], "last_changed": row["last_changed"],
                "related": [{"title": r["title"] or r["url"], "url": r["url"]} for r in related]}


def snippet(text: str, words: list[str], width: int = 240) -> str:
    """The stretch of `text` holding the most query words."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= width or not words:
        return text[:width]
    low = text.lower()
    best, best_hits = 0, -1
    for start in range(0, len(text) - width + 1, max(1, width // 4)):
        window = low[start:start + width]
        hits = sum(window.count(w) for w in set(words))
        if hits > best_hits:
            best, best_hits = start, hits
    cut = text[best:best + width]
    return ("..." if best else "") + cut + ("..." if best + width < len(text) else "")


class HashedEmbedder:
    """Offline backup vectors: hashed word and character-trigram counts. They match spelling variants and shared
    words, not true meaning, so the Gemini embedder is preferred whenever it is available."""
    model = "hashed-trigram-512"
    floor = 0.08  # lowest cosine similarity that still counts as a match

    def __init__(self, dims: int = 512):
        self.dims = dims

    def embed(self, texts: list[str], task: str = "") -> list[list[float]]:
        out = []
        for text in texts:
            vec = np.zeros(self.dims, dtype=np.float32)
            for word in _WORD.findall(text.lower())[:4000]:
                feats = [word] + [f"#{word}#"[i:i + 3] for i in range(len(word))]
                for j, feat in enumerate(feats):
                    h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=4).digest(), "little")
                    vec[h % self.dims] += (1.0 if j == 0 else 0.3) * (1 if h & 1 << 31 else -1)
            norm = np.linalg.norm(vec)
            out.append((vec / norm if norm else vec).tolist())
        return out


class EngineEmbedder:
    """Meaning vectors from Jarvis's adaptive engine (Gemini embeddings)."""

    floor = 0.3

    def __init__(self, engine):
        self.engine = engine

    @property
    def model(self) -> str:
        try:
            route = self.engine.get_runtime_configuration().routes.get("embed", ())
        except Exception:
            route = ()
        return f"engine:{route[0]}" if route else "engine:none"

    def embed(self, texts: list[str], task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        _, vectors = self.engine.embed(texts, task_type=task)
        return vectors


def embed_pending(store: GraphStore, embedder: Embedder, limit: int = 200, batch: int = 32) -> int:
    """Adds meaning vectors for pages that don't have one (or whose text changed). Returns how many."""
    done = 0
    model = embedder.model
    while done < limit:
        todo = store.pending_vectors(model, min(batch, limit - done))
        if not todo:
            break
        vectors = embedder.embed([t for _, _, t in todo], "RETRIEVAL_DOCUMENT")
        model = embedder.model
        store.save_vectors(model, [(rid, digest, v) for (rid, digest, _), v in zip(todo, vectors)])
        done += len(todo)
    return done
