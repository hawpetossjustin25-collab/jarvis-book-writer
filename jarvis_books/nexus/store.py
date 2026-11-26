"""The persistent resource graph: pages (nodes), links (edges), the crawl frontier, visit history and search data.

Lives in its own SQLite file (WAL) next to Jarvis's library, so a big crawl never slows the books database.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from jarvis_books.nexus.dedup import NEAR_BITS, bands, hamming, to_signed, to_unsigned
from jarvis_books.nexus.urls import host_of, site_of
from jarvis_books.storage.db import Database

log = logging.getLogger(__name__)

MIGRATIONS: dict[int, str] = {
    1: """
CREATE TABLE resources (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL,
    site TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    resource_type TEXT NOT NULL DEFAULT 'unknown',
    lang TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'discovered',
    http_status INTEGER,
    final_url TEXT,
    content_hash TEXT,
    simhash INTEGER,
    band0 INTEGER, band1 INTEGER, band2 INTEGER, band3 INTEGER,
    duplicate_of INTEGER REFERENCES resources(id) ON DELETE SET NULL,
    relevance REAL NOT NULL DEFAULT 0,
    rank REAL NOT NULL DEFAULT 0,
    trust TEXT NOT NULL DEFAULT '{}',
    depth INTEGER NOT NULL DEFAULT 0,
    parent_id INTEGER REFERENCES resources(id) ON DELETE SET NULL,
    discovered_at REAL NOT NULL,
    last_visited REAL,
    last_modified TEXT,
    etag TEXT,
    last_changed REAL,
    change_count INTEGER NOT NULL DEFAULT 0,
    visit_count INTEGER NOT NULL DEFAULT 0,
    next_visit REAL,
    revisit_s REAL,
    error TEXT
);
CREATE INDEX idx_res_status ON resources(status);
CREATE INDEX idx_res_site ON resources(site);
CREATE INDEX idx_res_domain ON resources(domain);
CREATE INDEX idx_res_hash ON resources(content_hash);
CREATE INDEX idx_res_next ON resources(next_visit);
CREATE INDEX idx_res_b0 ON resources(band0);
CREATE INDEX idx_res_b1 ON resources(band1);
CREATE INDEX idx_res_b2 ON resources(band2);
CREATE INDEX idx_res_b3 ON resources(band3);
CREATE TABLE edges (
    src INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    dst INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    rel TEXT NOT NULL DEFAULT 'link',
    anchor TEXT NOT NULL DEFAULT '',
    relevance REAL NOT NULL DEFAULT 0,
    discovered_at REAL NOT NULL,
    PRIMARY KEY (src, dst)
);
CREATE INDEX idx_edges_dst ON edges(dst);
CREATE TABLE crawl_runs (
    id INTEGER PRIMARY KEY,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL DEFAULT 'running',
    topic TEXT NOT NULL DEFAULT '',
    seeds TEXT NOT NULL DEFAULT '[]',
    config TEXT NOT NULL DEFAULT '{}',
    stats TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE frontier (
    resource_id INTEGER PRIMARY KEY REFERENCES resources(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    priority REAL NOT NULL,
    depth INTEGER NOT NULL,
    enqueued_at REAL NOT NULL
);
CREATE INDEX idx_frontier_pick ON frontier(run_id, priority DESC, enqueued_at);
CREATE TABLE visits (
    id INTEGER PRIMARY KEY,
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    run_id INTEGER REFERENCES crawl_runs(id) ON DELETE SET NULL,
    at REAL NOT NULL,
    outcome TEXT NOT NULL,
    http_status INTEGER,
    elapsed_ms REAL,
    bytes INTEGER,
    content_hash TEXT,
    error TEXT
);
CREATE INDEX idx_visits_res ON visits(resource_id, at);
CREATE TABLE vectors (
    resource_id INTEGER PRIMARY KEY REFERENCES resources(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    vec BLOB NOT NULL
);
CREATE INDEX idx_vectors_model ON vectors(model);
CREATE TABLE search_history (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    query TEXT NOT NULL,
    filters TEXT NOT NULL DEFAULT '{}',
    result_count INTEGER NOT NULL,
    elapsed_ms REAL NOT NULL,
    cached INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE search_cache (
    key TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    at REAL NOT NULL,
    results TEXT NOT NULL
);
CREATE TABLE metrics (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    run_id INTEGER,
    name TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX idx_metrics_name ON metrics(name, at);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE VIRTUAL TABLE resource_fts USING fts5(
    title, description, text, url, content='resources', content_rowid='id', tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE resource_words USING fts5(
    title, text, content='resources', content_rowid='id', tokenize='unicode61', detail='none'
);
CREATE VIRTUAL TABLE resource_vocab USING fts5vocab(resource_words, row);
CREATE TRIGGER resources_ai AFTER INSERT ON resources BEGIN
    INSERT INTO resource_fts(rowid, title, description, text, url)
    VALUES (new.id, new.title, new.description, new.text, new.url);
    INSERT INTO resource_words(rowid, title, text) VALUES (new.id, new.title, new.text);
END;
CREATE TRIGGER resources_ad AFTER DELETE ON resources BEGIN
    INSERT INTO resource_fts(resource_fts, rowid, title, description, text, url)
    VALUES ('delete', old.id, old.title, old.description, old.text, old.url);
    INSERT INTO resource_words(resource_words, rowid, title, text) VALUES ('delete', old.id, old.title, old.text);
END;
CREATE TRIGGER resources_au AFTER UPDATE OF title, description, text, url ON resources BEGIN
    INSERT INTO resource_fts(resource_fts, rowid, title, description, text, url)
    VALUES ('delete', old.id, old.title, old.description, old.text, old.url);
    INSERT INTO resource_fts(rowid, title, description, text, url)
    VALUES (new.id, new.title, new.description, new.text, new.url);
    INSERT INTO resource_words(resource_words, rowid, title, text) VALUES ('delete', old.id, old.title, old.text);
    INSERT INTO resource_words(rowid, title, text) VALUES (new.id, new.title, new.text);
END;
""",
    2: """
CREATE TABLE intel_pages (
    resource_id INTEGER PRIMARY KEY REFERENCES resources(id) ON DELETE CASCADE,
    content_hash TEXT,
    category TEXT NOT NULL DEFAULT 'general',
    summary TEXT NOT NULL DEFAULT '',
    entity_count INTEGER NOT NULL DEFAULT 0,
    claim_count INTEGER NOT NULL DEFAULT 0,
    analyzed_at REAL NOT NULL
);
CREATE TABLE intel_entities (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    norm TEXT NOT NULL,
    name TEXT NOT NULL,
    mentions INTEGER NOT NULL DEFAULT 0,
    UNIQUE(kind, norm)
);
CREATE INDEX idx_ent_norm ON intel_entities(norm);
CREATE TABLE intel_mentions (
    entity_id INTEGER NOT NULL REFERENCES intel_entities(id) ON DELETE CASCADE,
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (entity_id, resource_id)
);
CREATE INDEX idx_mention_res ON intel_mentions(resource_id);
CREATE TABLE intel_claims (
    id INTEGER PRIMARY KEY,
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    subject_norm TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value TEXT NOT NULL,
    value_norm TEXT NOT NULL,
    sentence TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX idx_claim_key ON intel_claims(subject_norm, predicate);
CREATE INDEX idx_claim_res ON intel_claims(resource_id);
CREATE TABLE intel_relations (
    a INTEGER NOT NULL REFERENCES intel_entities(id) ON DELETE CASCADE,
    b INTEGER NOT NULL REFERENCES intel_entities(id) ON DELETE CASCADE,
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    weight INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (a, b, resource_id)
);
CREATE INDEX idx_rel_b ON intel_relations(b);
CREATE INDEX idx_rel_res ON intel_relations(resource_id);
""",
}

SEARCHABLE = ("fetched",)  # statuses whose pages appear in search results


@dataclass(frozen=True)
class PageRecord:
    """What one fetch learned about a page; written by GraphStore.record_page."""
    final_url: str
    http_status: int
    title: str
    description: str
    text: str
    resource_type: str
    lang: str
    content_hash: str
    simhash: int
    etag: Optional[str]
    last_modified: Optional[str]
    trust: dict
    status: str = "fetched"


class GraphStore:
    def __init__(self, path: Path | str, clock=time.time):
        self.db = Database(path, migrations=MIGRATIONS)
        self.clock = clock

    def close(self) -> None:
        self.db.close()

    # ---- generation counter: bumped whenever searchable content changes, invalidating the search cache
    def generation(self) -> int:
        row = self.db.query_one("SELECT value FROM meta WHERE key='generation'")
        return int(row[0]) if row else 0

    def bump_generation(self) -> int:
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta(key, value) VALUES('generation', '1') ON CONFLICT(key) "
                      "DO UPDATE SET value = CAST(value AS INTEGER) + 1")
        return self.generation()

    # ---- nodes and edges
    def ensure(self, url: str, parent_id: Optional[int] = None, depth: int = 0, relevance: float = 0.0) -> int:
        """The id of `url`'s node, creating it (as 'discovered') when new."""
        row = self.db.query_one("SELECT id FROM resources WHERE url=?", (url,))
        if row:
            return row[0]
        host = host_of(url)
        cur = self.db.execute(
            "INSERT INTO resources(url, domain, site, depth, parent_id, relevance, discovered_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(url) DO NOTHING",
            (url, host, site_of(host), depth, parent_id, relevance, self.clock()))
        if cur.rowcount:
            return cur.lastrowid
        return self.db.query_one("SELECT id FROM resources WHERE url=?", (url,))[0]

    def add_edge(self, src: int, dst: int, anchor: str = "", rel: str = "link", relevance: float = 0.0) -> None:
        if src == dst:
            return
        self.db.execute(
            "INSERT INTO edges(src, dst, rel, anchor, relevance, discovered_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(src, dst) DO UPDATE SET anchor = CASE WHEN excluded.anchor != '' THEN excluded.anchor "
            "ELSE edges.anchor END, relevance = MAX(edges.relevance, excluded.relevance)",
            (src, dst, rel, anchor[:300], relevance, self.clock()))

    def resource(self, rid: int) -> Optional[dict]:
        row = self.db.query_one("SELECT * FROM resources WHERE id=?", (rid,))
        return _node(row) if row else None

    def by_url(self, url: str) -> Optional[dict]:
        row = self.db.query_one("SELECT * FROM resources WHERE url=? OR final_url=? ORDER BY url=? DESC LIMIT 1",
                                (url, url, url))
        return _node(row) if row else None

    def outgoing(self, rid: int, limit: int = 50) -> list[dict]:
        rows = self.db.query("SELECT r.id, r.url, r.title, r.status, e.anchor, e.rel FROM edges e JOIN resources r "
                             "ON r.id = e.dst WHERE e.src=? ORDER BY e.relevance DESC, r.rank DESC LIMIT ?",
                             (rid, limit))
        return [dict(r) for r in rows]

    def incoming(self, rid: int, limit: int = 50) -> list[dict]:
        rows = self.db.query("SELECT r.id, r.url, r.title, r.status, e.anchor, e.rel FROM edges e JOIN resources r "
                             "ON r.id = e.src WHERE e.dst=? ORDER BY r.rank DESC LIMIT ?", (rid, limit))
        return [dict(r) for r in rows]

    def link_counts(self, rid: int) -> tuple[int, int]:
        out = self.db.query_one("SELECT COUNT(*) FROM edges WHERE src=?", (rid,))[0]
        inc = self.db.query_one("SELECT COUNT(*) FROM edges WHERE dst=?", (rid,))[0]
        return out, inc

    def visits(self, rid: int, limit: int = 20) -> list[dict]:
        rows = self.db.query("SELECT at, outcome, http_status, elapsed_ms, bytes, error FROM visits "
                             "WHERE resource_id=? ORDER BY at DESC, id DESC LIMIT ?", (rid, limit))
        return [dict(r) for r in rows]

    # ---- crawl runs and the persistent frontier
    def start_run(self, topic: str, seeds: list[str], config: dict) -> int:
        cur = self.db.execute("INSERT INTO crawl_runs(started_at, topic, seeds, config) VALUES(?,?,?,?)",
                              (self.clock(), topic, json.dumps(seeds), json.dumps(config)))
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, stats: dict) -> None:
        self.db.execute("UPDATE crawl_runs SET finished_at=?, status=?, stats=? WHERE id=?",
                        (self.clock(), status, json.dumps(stats), run_id))

    def run(self, run_id: int) -> Optional[dict]:
        row = self.db.query_one("SELECT * FROM crawl_runs WHERE id=?", (run_id,))
        if not row:
            return None
        out = dict(row)
        for key in ("seeds", "config", "stats"):
            out[key] = json.loads(out[key])
        return out

    def unfinished_run(self) -> Optional[int]:
        """The newest crawl that stopped early (cancelled, crashed or power cut) with links still queued."""
        row = self.db.query_one("SELECT r.id FROM crawl_runs r WHERE r.status != 'done' AND EXISTS "
                                "(SELECT 1 FROM frontier f WHERE f.run_id = r.id) ORDER BY r.id DESC LIMIT 1")
        return row[0] if row else None

    def enqueue(self, run_id: int, rid: int, priority: float, depth: int) -> bool:
        cur = self.db.execute("INSERT INTO frontier(resource_id, run_id, priority, depth, enqueued_at) "
                              "VALUES(?,?,?,?,?) ON CONFLICT(resource_id) DO UPDATE SET "
                              "priority = MAX(frontier.priority, excluded.priority), "
                              "depth = MIN(frontier.depth, excluded.depth), run_id = excluded.run_id",
                              (rid, run_id, priority, depth, self.clock()))
        return cur.rowcount > 0

    def frontier_size(self, run_id: Optional[int] = None) -> int:
        if run_id is None:
            return self.db.query_one("SELECT COUNT(*) FROM frontier")[0]
        return self.db.query_one("SELECT COUNT(*) FROM frontier WHERE run_id=?", (run_id,))[0]

    def pop(self, run_id: int, skip_hosts: Iterable[str] = ()) -> Optional[tuple[int, str, int]]:
        """Takes the highest-priority queued page (id, url, depth), preferring hosts not in `skip_hosts`."""
        skip = list(skip_hosts)
        with self.db.transaction() as c:
            row = None
            if skip:
                marks = ",".join("?" * len(skip))
                row = c.execute(f"SELECT f.resource_id, r.url, f.depth FROM frontier f JOIN resources r "
                                f"ON r.id = f.resource_id WHERE f.run_id=? AND r.domain NOT IN ({marks}) "
                                f"ORDER BY f.priority DESC, f.enqueued_at LIMIT 1", (run_id, *skip)).fetchone()
            if row is None:
                row = c.execute("SELECT f.resource_id, r.url, f.depth FROM frontier f JOIN resources r "
                                "ON r.id = f.resource_id WHERE f.run_id=? ORDER BY f.priority DESC, f.enqueued_at "
                                "LIMIT 1", (run_id,)).fetchone()
            if row is None:
                return None
            c.execute("DELETE FROM frontier WHERE resource_id=?", (row[0],))
            return row[0], row[1], row[2]

    def clear_frontier(self, run_id: int) -> int:
        return self.db.execute("DELETE FROM frontier WHERE run_id=?", (run_id,)).rowcount

    def visited_in_run(self, run_id: int, rid: int) -> bool:
        return self.db.query_one("SELECT 1 FROM visits WHERE run_id=? AND resource_id=?", (run_id, rid)) is not None

    def due(self, limit: int, now: Optional[float] = None) -> list[tuple[int, str]]:
        rows = self.db.query("SELECT id, url FROM resources WHERE next_visit IS NOT NULL AND next_visit <= ? "
                             "ORDER BY next_visit LIMIT ?", (now if now is not None else self.clock(), limit))
        return [(r[0], r[1]) for r in rows]

    # ---- fetch outcomes
    def conditional_headers(self, rid: int) -> tuple[Optional[str], Optional[str]]:
        row = self.db.query_one("SELECT etag, last_modified FROM resources WHERE id=? AND status='fetched'", (rid,))
        return (row[0], row[1]) if row else (None, None)

    def find_duplicate(self, rid: int, digest: str, sim: int) -> Optional[int]:
        """Another stored, original page with the same text (exact hash) or nearly the same (SimHash)."""
        row = self.db.query_one("SELECT id FROM resources WHERE content_hash=? AND id != ? AND duplicate_of IS NULL "
                                "AND status='fetched' ORDER BY id LIMIT 1", (digest, rid))
        if row:
            return row[0]
        if not sim:
            return None
        b = bands(sim)
        rows = self.db.query("SELECT id, simhash FROM resources WHERE id != ? AND duplicate_of IS NULL AND "
                             "status='fetched' AND (band0=? OR band1=? OR band2=? OR band3=?) ORDER BY id LIMIT 200",
                             (rid, *b))
        for other, other_sim in rows:
            if other_sim is not None and hamming(sim, to_unsigned(other_sim)) <= NEAR_BITS:
                return other
        return None

    def record_page(self, rid: int, page: PageRecord, duplicate_of: Optional[int], revisit_min: float,
                    revisit_max: float) -> bool:
        """Saves a fetched page. True when its content changed since the previous visit."""
        now = self.clock()
        old = self.db.query_one("SELECT content_hash, revisit_s, change_count FROM resources WHERE id=?", (rid,))
        changed = bool(old and old[0] and old[0] != page.content_hash)
        first = not (old and old[0])
        interval = old[1] if old and old[1] else revisit_min * 4
        interval = revisit_min if first else (max(revisit_min, interval / 2) if changed else min(revisit_max, interval * 2))
        b = bands(page.simhash)
        self.db.execute(
            "UPDATE resources SET final_url=?, http_status=?, title=?, description=?, text=?, resource_type=?, "
            "lang=?, content_hash=?, simhash=?, band0=?, band1=?, band2=?, band3=?, duplicate_of=?, trust=?, "
            "status=?, etag=?, last_modified=?, last_visited=?, visit_count=visit_count+1, "
            "last_changed=CASE WHEN ? THEN ? ELSE COALESCE(last_changed, ?) END, change_count=change_count+?, "
            "revisit_s=?, next_visit=?, error=NULL WHERE id=?",
            (page.final_url, page.http_status, page.title, page.description, page.text, page.resource_type,
             page.lang, page.content_hash, to_signed(page.simhash), *b, duplicate_of, json.dumps(page.trust),
             page.status, page.etag, page.last_modified, now, changed or first, now, now, int(changed), interval,
             now + interval, rid))
        return changed

    def record_unchanged(self, rid: int, revisit_max: float) -> None:
        """A 304 Not Modified: the stored copy is still current, so the page is checked less often."""
        now = self.clock()
        self.db.execute("UPDATE resources SET last_visited=?, visit_count=visit_count+1, "
                        "revisit_s=MIN(?, COALESCE(revisit_s, ?) * 2), next_visit=? + MIN(?, COALESCE(revisit_s, ?) * 2) "
                        "WHERE id=?", (now, revisit_max, revisit_max / 8, now, revisit_max, revisit_max / 8, rid))

    def record_failure(self, rid: int, status: str, error: str, http_status: Optional[int],
                       retry_after_s: Optional[float]) -> None:
        now = self.clock()
        self.db.execute("UPDATE resources SET status = CASE WHEN status='fetched' THEN status ELSE ? END, error=?, "
                        "http_status=COALESCE(?, http_status), last_visited=?, visit_count=visit_count+1, "
                        "next_visit=? WHERE id=?",
                        (status, error[:500], http_status, now, now + retry_after_s if retry_after_s else None, rid))

    def mark_status(self, rid: int, status: str, error: str = "") -> None:
        self.db.execute("UPDATE resources SET status=?, error=? WHERE id=?", (status, error[:500] or None, rid))

    def log_visit(self, rid: int, run_id: Optional[int], outcome: str, http_status: Optional[int],
                  elapsed_ms: Optional[float], size: Optional[int], digest: Optional[str], error: str = "") -> None:
        self.db.execute("INSERT INTO visits(resource_id, run_id, at, outcome, http_status, elapsed_ms, bytes, "
                        "content_hash, error) VALUES(?,?,?,?,?,?,?,?,?)",
                        (rid, run_id, self.clock(), outcome, http_status, elapsed_ms, size, digest, error[:500] or None))

    # ---- vectors for meaning search
    def pending_vectors(self, model: str, limit: int) -> list[tuple[int, str, str]]:
        rows = self.db.query(
            "SELECT r.id, r.content_hash, r.title || '\n' || r.description || '\n' || substr(r.text, 1, 2000) "
            "FROM resources r LEFT JOIN vectors v ON v.resource_id = r.id AND v.model = ? "
            "WHERE r.status='fetched' AND r.duplicate_of IS NULL AND (v.resource_id IS NULL OR "
            "v.content_hash != r.content_hash) ORDER BY r.rank DESC, r.id LIMIT ?", (model, limit))
        return [(r[0], r[1], r[2]) for r in rows]

    def save_vectors(self, model: str, items: list[tuple[int, str, list[float]]]) -> None:
        with self.db.transaction() as c:
            c.executemany("INSERT INTO vectors(resource_id, model, content_hash, vec) VALUES(?,?,?,?) "
                          "ON CONFLICT(resource_id) DO UPDATE SET model=excluded.model, "
                          "content_hash=excluded.content_hash, vec=excluded.vec",
                          [(rid, model, digest, np.asarray(v, dtype=np.float32).tobytes()) for rid, digest, v in items])

    def vectors(self, model: str) -> tuple[np.ndarray, np.ndarray]:
        rows = self.db.query("SELECT v.resource_id, v.vec FROM vectors v JOIN resources r ON r.id = v.resource_id "
                             "WHERE v.model=? AND r.status='fetched' AND r.duplicate_of IS NULL", (model,))
        if not rows:
            return np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float32)
        ids = np.array([r[0] for r in rows], dtype=np.int64)
        mat = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        return ids, mat

    # ---- link-graph rank (PageRank over fetched pages)
    def compute_rank(self, damping: float = 0.85, iterations: int = 40, tol: float = 1e-9) -> int:
        ids = [r[0] for r in self.db.query("SELECT id FROM resources WHERE status='fetched'")]
        if not ids:
            return 0
        index = {rid: i for i, rid in enumerate(ids)}
        edges = [(index[s], index[d]) for s, d in self.db.query("SELECT src, dst FROM edges")
                 if s in index and d in index]
        n = len(ids)
        rank = np.full(n, 1.0 / n)
        if edges:
            src = np.array([e[0] for e in edges])
            dst = np.array([e[1] for e in edges])
            outdeg = np.bincount(src, minlength=n).astype(float)
        for _ in range(iterations):
            new = np.full(n, (1 - damping) / n)
            if edges:
                np.add.at(new, dst, damping * rank[src] / outdeg[src])
                dangling = rank[outdeg == 0].sum()
            else:
                dangling = rank.sum()
            new += damping * dangling / n
            done = np.abs(new - rank).sum() < tol
            rank = new
            if done:
                break
        scaled = rank / rank.max()
        with self.db.transaction() as c:
            c.executemany("UPDATE resources SET rank=? WHERE id=?", [(float(scaled[i]), rid) for rid, i in index.items()])
        return n

    # ---- search history, cache and metrics
    def log_search(self, query: str, filters: dict, count: int, elapsed_ms: float, cached: bool) -> None:
        self.db.execute("INSERT INTO search_history(at, query, filters, result_count, elapsed_ms, cached) "
                        "VALUES(?,?,?,?,?,?)", (self.clock(), query, json.dumps(filters), count, elapsed_ms, int(cached)))

    def search_history(self, limit: int = 20) -> list[dict]:
        rows = self.db.query("SELECT at, query, filters, result_count, elapsed_ms, cached FROM search_history "
                             "ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "filters": json.loads(r["filters"]), "cached": bool(r["cached"])} for r in rows]

    def cached(self, key: str, ttl_s: float) -> Optional[list]:
        row = self.db.query_one("SELECT generation, at, results FROM search_cache WHERE key=?", (key,))
        if row and row[0] == self.generation() and self.clock() - row[1] <= ttl_s:
            return json.loads(row[2])
        return None

    def cache(self, key: str, results: list, keep: int = 500) -> None:
        self.db.execute("INSERT INTO search_cache(key, generation, at, results) VALUES(?,?,?,?) ON CONFLICT(key) "
                        "DO UPDATE SET generation=excluded.generation, at=excluded.at, results=excluded.results",
                        (key, self.generation(), self.clock(), json.dumps(results)))
        self.db.execute("DELETE FROM search_cache WHERE key NOT IN (SELECT key FROM search_cache ORDER BY at DESC "
                        "LIMIT ?)", (keep,))

    def record_metrics(self, run_id: Optional[int], values: dict[str, float]) -> None:
        now = self.clock()
        with self.db.transaction() as c:
            c.executemany("INSERT INTO metrics(at, run_id, name, value) VALUES(?,?,?,?)",
                          [(now, run_id, k, float(v)) for k, v in values.items()])

    def metric_series(self, name: str, limit: int = 50) -> list[tuple[float, float]]:
        rows = self.db.query("SELECT at, value FROM metrics WHERE name=? ORDER BY id DESC LIMIT ?", (name, limit))
        return [(r[0], r[1]) for r in rows]

    def stats(self) -> dict:
        counts = {r[0]: r[1] for r in self.db.query("SELECT status, COUNT(*) FROM resources GROUP BY status")}
        return {
            "pages": sum(counts.values()),
            "by_status": counts,
            "searchable": self.db.query_one("SELECT COUNT(*) FROM resources WHERE status='fetched' AND "
                                            "duplicate_of IS NULL")[0],
            "duplicates": self.db.query_one("SELECT COUNT(*) FROM resources WHERE duplicate_of IS NOT NULL")[0],
            "links": self.db.query_one("SELECT COUNT(*) FROM edges")[0],
            "sites": self.db.query_one("SELECT COUNT(DISTINCT site) FROM resources WHERE status='fetched'")[0],
            "queued": self.frontier_size(),
            "changed_pages": self.db.query_one("SELECT COUNT(*) FROM resources WHERE change_count > 0")[0],
            "due_for_revisit": self.db.query_one("SELECT COUNT(*) FROM resources WHERE next_visit <= ?",
                                                 (self.clock(),))[0],
            "crawls": self.db.query_one("SELECT COUNT(*) FROM crawl_runs")[0],
            "searches": self.db.query_one("SELECT COUNT(*) FROM search_history")[0],
            "database_mb": round(self.db.size_bytes() / 1e6, 2),
        }


def _node(row) -> dict:
    out = dict(row)
    out["trust"] = json.loads(out.get("trust") or "{}")
    if out.get("simhash") is not None:
        out["simhash"] = to_unsigned(out["simhash"])
    for key in ("band0", "band1", "band2", "band3"):
        out.pop(key, None)
    return out
