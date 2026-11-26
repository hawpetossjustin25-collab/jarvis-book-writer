"""OS2, the intelligence system: reads pages OS1 saved and turns them into sourced facts, entities and links
between them, then compares sources to sort every fact into verified, unverified or conflicting.

It never upgrades a guess to a fact: a fact is only what a page plainly states, it is "verified" only when two or
more different websites state the same value and none disagrees, and links found by names appearing together are
labelled inference. Page text is untrusted data; OS2 only quotes it, it never acts on it.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter, defaultdict
from typing import Optional

from jarvis_books.nexus import intel
from jarvis_books.nexus.contracts import ContractError, NexusRequest, NexusResponse, failure, success, \
    validate_payload

log = logging.getLogger(__name__)

MAX_PAGE_CHARS = 60_000
TRUSTED_SUFFIXES = (".gov", ".mil", ".edu")
PAYLOADS = {
    "analyze": {"type": "object", "additionalProperties": False, "properties": {
        "url": {"type": "string", "minLength": 4, "maxLength": 2000},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100_000},
        "force": {"type": "boolean"}}},
    "brief": {"type": "object", "required": ["query"], "additionalProperties": False, "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 500},
        "max_pages": {"type": "integer", "minimum": 1, "maximum": 50},
        "max_facts": {"type": "integer", "minimum": 1, "maximum": 200}}},
    "facts": {"type": "object", "required": ["subject"], "additionalProperties": False, "properties": {
        "subject": {"type": "string", "minLength": 1, "maxLength": 300},
        "predicate": {"type": "string", "minLength": 1, "maxLength": 60}}},
    "entity": {"type": "object", "required": ["name"], "additionalProperties": False, "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 300},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100}}},
    "contradictions": {"type": "object", "additionalProperties": False, "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 200}}},
    "status": {"type": "object", "additionalProperties": False, "properties": {}},
}


def trusted(site: str) -> bool:
    return any(site.endswith(s) or (s + ".") in site for s in TRUSTED_SUFFIXES)


class OS2:
    """Works on OS1's database (same file, its own tables). Thread-safe through the database's own lock."""

    def __init__(self, os1, clock=time.time):
        self.os1 = os1
        self.db = os1.store.db
        self.clock = clock
        self._lock = threading.Lock()

    # ---- contract entry point
    def handle(self, request: NexusRequest | dict | str) -> NexusResponse:
        started = time.perf_counter()
        try:
            if isinstance(request, str):
                request = NexusRequest.from_json(request)
            elif isinstance(request, dict):
                request = NexusRequest.from_dict(request)
        except ContractError as exc:
            raw = request if isinstance(request, dict) else {}
            bogus = NexusRequest("jarvis", "os2", "invalid", {}, str(raw.get("request_id") or "invalid")[:100])
            return failure(bogus, "os2", exc.code, str(exc), started)
        if request.destination != "os2":
            return failure(request, "os2", "invalid_request", f"this is OS2, not {request.destination}", started)
        schema = PAYLOADS.get(request.operation)
        if schema is None:
            return failure(request, "os2", "unknown_operation", f"OS2 can do: {', '.join(sorted(PAYLOADS))}",
                           started)
        try:
            validate_payload(schema, request.payload)
            results, extra = getattr(self, "_op_" + request.operation)(request.payload)
        except ContractError as exc:
            return failure(request, "os2", exc.code, str(exc), started)
        except ValueError as exc:
            return failure(request, "os2", "invalid_request", str(exc), started)
        except Exception as exc:
            log.exception("nexus os2 %s failed", request.operation)
            return failure(request, "os2", "internal", f"{type(exc).__name__}: {exc}", started)
        return success(request, "os2", results, started, **extra)

    def call(self, operation: str, payload: dict, correlation_id: Optional[str] = None) -> NexusResponse:
        return self.handle(NexusRequest("jarvis", "os2", operation, payload, correlation_id=correlation_id))

    # ---- reading pages
    def analyze_page(self, rid: int, force: bool = False) -> Optional[dict]:
        """Extract entities, facts, relations, category and summary from one saved page (skipped if unchanged)."""
        row = self.db.query_one("SELECT id, url, final_url, title, text, content_hash, status, duplicate_of "
                                "FROM resources WHERE id=?", (rid,))
        if row is None or row["status"] != "fetched" or row["duplicate_of"] is not None:
            return None
        done = self.db.query_one("SELECT content_hash FROM intel_pages WHERE resource_id=?", (rid,))
        if done is not None and not force and done[0] == row["content_hash"]:
            return {"resource_id": rid, "skipped": True}
        text = (row["text"] or "")[:MAX_PAGE_CHARS]
        body = f"{row['title']}.\n\n{text}" if row["title"] else text
        found = intel.entities(body)
        facts = intel.claims(body)
        category, _ = intel.classify(row["final_url"] or row["url"], row["title"], text)
        summary = intel.summarize(text, 3)
        low = body.lower()
        counts = Counter({(e.kind, e.norm): max(1, low.count(e.name.lower())) for e in found})
        names = {(e.kind, e.norm): e.name for e in found}
        now = self.clock()
        with self._lock, self.db.transaction() as conn:
            for table in ("intel_claims", "intel_relations"):
                conn.execute(f"DELETE FROM {table} WHERE resource_id=?", (rid,))
            for eid, cnt in conn.execute("SELECT entity_id, count FROM intel_mentions WHERE resource_id=?",
                                         (rid,)).fetchall():
                conn.execute("UPDATE intel_entities SET mentions = MAX(0, mentions - ?) WHERE id=?", (cnt, eid))
            conn.execute("DELETE FROM intel_mentions WHERE resource_id=?", (rid,))
            ids: dict[tuple[str, str], int] = {}
            for key, cnt in counts.items():
                kind, norm = key
                conn.execute("INSERT INTO intel_entities(kind, norm, name, mentions) VALUES (?,?,?,0) "
                             "ON CONFLICT(kind, norm) DO NOTHING", (kind, norm, names[key][:300]))
                eid = conn.execute("SELECT id FROM intel_entities WHERE kind=? AND norm=?", (kind, norm)).fetchone()[0]
                conn.execute("UPDATE intel_entities SET mentions = mentions + ? WHERE id=?", (cnt, eid))
                conn.execute("INSERT INTO intel_mentions(entity_id, resource_id, count) VALUES (?,?,?)",
                             (eid, rid, cnt))
                ids[key] = eid
            for c in facts:
                conn.execute("INSERT INTO intel_claims(resource_id, subject, subject_norm, predicate, value, "
                             "value_norm, sentence, created_at) VALUES (?,?,?,?,?,?,?,?)",
                             (rid, c.subject[:300], c.subject_norm[:300], c.predicate, c.value[:300],
                              c.value_norm[:300], c.sentence, now))
            pairs: Counter = Counter()
            for sent in intel.sentences(body):
                here = sorted({ids[(e.kind, e.norm)] for e in intel.entities(sent)
                               if e.kind in ("name", "org", "place") and (e.kind, e.norm) in ids})
                for i, a in enumerate(here[:12]):
                    for b in here[i + 1:12]:
                        pairs[(a, b)] += 1
            for (a, b), weight in pairs.items():
                conn.execute("INSERT INTO intel_relations(a, b, resource_id, weight) VALUES (?,?,?,?)",
                             (a, b, rid, weight))
            conn.execute("INSERT INTO intel_pages(resource_id, content_hash, category, summary, entity_count, "
                         "claim_count, analyzed_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(resource_id) DO UPDATE SET "
                         "content_hash=excluded.content_hash, category=excluded.category, summary=excluded.summary, "
                         "entity_count=excluded.entity_count, claim_count=excluded.claim_count, "
                         "analyzed_at=excluded.analyzed_at",
                         (rid, row["content_hash"], category, summary, len(counts), len(facts), now))
        return {"resource_id": rid, "url": row["final_url"] or row["url"], "category": category,
                "entities": len(counts), "facts": len(facts), "relations": len(pairs), "skipped": False}

    def pending(self, limit: int) -> list[int]:
        """Saved pages not read yet, or changed since they were read."""
        rows = self.db.query(
            "SELECT r.id FROM resources r LEFT JOIN intel_pages p ON p.resource_id = r.id "
            "WHERE r.status='fetched' AND r.duplicate_of IS NULL AND "
            "(p.resource_id IS NULL OR p.content_hash IS NOT r.content_hash) ORDER BY r.rank DESC, r.id LIMIT ?",
            (limit,))
        return [r[0] for r in rows]

    def _rid_for(self, url: str) -> int:
        from jarvis_books.nexus.urls import normalize
        try:
            url = normalize(url)
        except ValueError:
            pass
        row = self.db.query_one("SELECT id FROM resources WHERE url=? OR final_url=? ORDER BY status='fetched' DESC",
                                (url, url))
        if row is None:
            raise ContractError(f"{url} isn't in the web index yet", "not_found")
        return row[0]

    # ---- judging facts
    def judge(self, subject_norm: str, predicate: str) -> dict:
        """Every source's value for one (subject, predicate), sorted into verified/unverified/conflicting."""
        rows = self.db.query(
            "SELECT c.subject, c.value, c.value_norm, c.sentence, r.url, r.final_url, r.title, r.site "
            "FROM intel_claims c JOIN resources r ON r.id = c.resource_id "
            "WHERE c.subject_norm=? AND c.predicate=? AND r.duplicate_of IS NULL ORDER BY r.rank DESC",
            (subject_norm, predicate))
        groups: list[dict] = []
        for row in rows:
            for g in groups:
                if intel.same_value(g["value_norm"], row["value_norm"]):
                    break
            else:
                g = {"value": row["value"], "value_norm": row["value_norm"], "sources": [], "sites": set()}
                groups.append(g)
            if row["site"] not in g["sites"]:
                g["sites"].add(row["site"])
                g["sources"].append({"url": row["final_url"] or row["url"], "title": row["title"],
                                     "site": row["site"], "quote": row["sentence"]})
        groups.sort(key=lambda g: len(g["sites"]), reverse=True)
        all_sites = set().union(*(g["sites"] for g in groups)) if groups else set()
        functional = predicate in intel.FUNCTIONAL
        if not groups:
            status, confidence = "missing", 0.0
        elif functional and len(groups) > 1:
            status = "conflicting"
            confidence = round(0.5 * len(groups[0]["sites"]) / len(all_sites), 2)
        elif len(groups[0]["sites"]) >= 2:
            status = "verified"
            confidence = min(0.95, 0.6 + 0.1 * (len(groups[0]["sites"]) - 2)
                             + (0.05 if any(trusted(s) for s in groups[0]["sites"]) else 0))
        else:
            status = "unverified"
            confidence = 0.45 if any(trusted(s) for s in all_sites) else 0.35
        subject = rows[0]["subject"] if rows else subject_norm
        values = [{"value": g["value"], "sites": len(g["sites"]), "sources": g["sources"][:5]} for g in groups]
        if not functional:  # descriptions ("X is a ...") can all be true at once, so each stands on its own
            values = values[:5]
        return {"subject": subject, "predicate": predicate, "status": status, "confidence": round(confidence, 2),
                "values": values, "source_count": len(all_sites)}

    def _keys(self, where: str, params: tuple, limit: int) -> list[tuple[str, str]]:
        rows = self.db.query(f"SELECT c.subject_norm, c.predicate, COUNT(*) n FROM intel_claims c WHERE {where} "
                             f"GROUP BY c.subject_norm, c.predicate ORDER BY n DESC LIMIT ?", (*params, limit))
        return [(r[0], r[1]) for r in rows]

    # ---- operations
    def _op_analyze(self, p: dict) -> tuple[list, dict]:
        force = p.get("force", False)
        rids = [self._rid_for(p["url"])] if p.get("url") else self.pending(p.get("limit", 200))
        started = time.perf_counter()
        done = [r for r in (self.analyze_page(rid, force) for rid in rids) if r is not None]
        fresh = [r for r in done if not r["skipped"]]
        return fresh if p.get("url") else fresh[:20], {
            "pages_read": len(fresh), "pages_skipped": len(done) - len(fresh),
            "facts_found": sum(r["facts"] for r in fresh), "entities_found": sum(r["entities"] for r in fresh),
            "seconds": round(time.perf_counter() - started, 3), "pages_waiting": len(self.pending(100_000))}

    def _op_brief(self, p: dict) -> tuple[list, dict]:
        query, max_pages, max_facts = p["query"], p.get("max_pages", 8), p.get("max_facts", 30)
        found = self.os1.search_engine.search(query, max_pages)
        pages = []
        for r in found["results"]:
            if r["match"] == "meaning" and self.os1.embedder is None:
                continue  # the offline backup's meaning matches are too loose to brief from
            row = self.db.query_one("SELECT id FROM resources WHERE (url=? OR final_url=?) AND status='fetched' "
                                    "AND duplicate_of IS NULL", (r["url"], r["url"]))
            if row is None:
                continue
            self.analyze_page(row[0])
            info = self.db.query_one("SELECT category, summary FROM intel_pages WHERE resource_id=?", (row[0],))
            pages.append({"id": row[0], "title": r["title"], "url": r["url"], "site": r["domain"],
                          "category": info["category"] if info else "general", "match": r["match"],
                          "summary": info["summary"] if info else ""})
        if not pages:
            return [], {"query": query, "summary": "", "facts": {"verified": [], "unverified": [], "conflicting": []},
                        "inferences": [], "entities": [], "sources": [],
                        "missing": [f"nothing in the web index about {query!r} yet"]}
        ids = tuple(pg["id"] for pg in pages)
        marks = ",".join("?" * len(ids))
        terms = [w for w in intel.keywords(query)]
        keys = self._keys(f"c.resource_id IN ({marks})", ids, 400)
        if terms:
            on_topic = [k for k in keys if any(t in k[0] for t in terms)]
            keys = on_topic + [k for k in keys if k not in on_topic]
        facts: dict[str, list] = {"verified": [], "unverified": [], "conflicting": []}
        for subject_norm, predicate in keys[:max_facts * 2]:
            verdict = self.judge(subject_norm, predicate)
            if verdict["status"] in facts and sum(len(v) for v in facts.values()) < max_facts:
                facts[verdict["status"]].append(verdict)
        top = self.db.query(
            f"SELECT e.id, e.name, e.kind, SUM(m.count) n FROM intel_mentions m JOIN intel_entities e "
            f"ON e.id = m.entity_id WHERE m.resource_id IN ({marks}) AND e.kind IN ('name','org','place') "
            f"GROUP BY e.id ORDER BY n DESC LIMIT 15", ids)
        rel = self.db.query(
            f"SELECT ea.name a, eb.name b, SUM(r.weight) w, COUNT(DISTINCT r.resource_id) pages FROM intel_relations r "
            f"JOIN intel_entities ea ON ea.id = r.a JOIN intel_entities eb ON eb.id = r.b "
            f"WHERE r.resource_id IN ({marks}) GROUP BY r.a, r.b HAVING w >= 2 ORDER BY w DESC LIMIT 10", ids)
        missing = []
        if not any(facts.values()):
            missing.append("the pages found don't plainly state any facts about this")
        elif not facts["verified"]:
            missing.append("no fact here is confirmed by two different websites yet")
        summary = intel.summarize(" ".join(pg["summary"] for pg in pages), 4, focus=query)
        sources = [{k: pg[k] for k in ("title", "url", "site", "category", "match")} for pg in pages]
        return sources, {"query": query, "summary": summary, "facts": facts,
                         "inferences": [{"between": [r["a"], r["b"]], "kind": "mentioned together",
                                         "times": r["w"], "pages": r["pages"], "status": "inference"} for r in rel],
                         "entities": [{"name": r["name"], "kind": r["kind"], "mentions": r["n"]} for r in top],
                         "sources": sources, "missing": missing}

    def _op_facts(self, p: dict) -> tuple[list, dict]:
        subject = intel.norm_text(p["subject"])
        if p.get("predicate"):
            keys = [(subject, p["predicate"].lower().strip())]
        else:
            keys = self._keys("c.subject_norm = ?", (subject,), 50)
            if not keys:
                keys = self._keys("c.subject_norm LIKE ?", (f"%{subject}%",), 50)
        verdicts = [self.judge(s, pr) for s, pr in keys]
        if not verdicts:
            verdicts = [self.judge(subject, p.get("predicate", "any"))]
        return verdicts, {"subject": p["subject"]}

    def _op_entity(self, p: dict) -> tuple[list, dict]:
        norm = intel.norm_text(p["name"])
        row = self.db.query_one("SELECT * FROM intel_entities WHERE norm=? ORDER BY mentions DESC", (norm,)) or \
            self.db.query_one("SELECT * FROM intel_entities WHERE norm LIKE ? ORDER BY mentions DESC", (f"%{norm}%",))
        if row is None:
            raise ContractError(f"no pages in the web index mention {p['name']!r}", "not_found")
        limit = p.get("limit", 10)
        pages = self.db.query("SELECT r.url, r.final_url, r.title, m.count FROM intel_mentions m JOIN resources r "
                              "ON r.id = m.resource_id WHERE m.entity_id=? ORDER BY m.count DESC LIMIT ?",
                              (row["id"], limit))
        linked = self.db.query(
            "SELECT e.name, e.kind, SUM(r.weight) w FROM intel_relations r JOIN intel_entities e "
            "ON e.id = CASE WHEN r.a = ? THEN r.b ELSE r.a END WHERE r.a = ? OR r.b = ? "
            "GROUP BY e.id ORDER BY w DESC LIMIT ?", (row["id"], row["id"], row["id"], limit))
        facts = [self.judge(s, pr) for s, pr in self._keys("c.subject_norm = ?", (row["norm"],), limit)]
        return [{"name": row["name"], "kind": row["kind"], "mentions": row["mentions"],
                 "pages": [{"title": r["title"], "url": r["final_url"] or r["url"], "mentions": r["count"]}
                           for r in pages],
                 "connected_to": [{"name": r["name"], "kind": r["kind"], "times": r["w"], "status": "inference"}
                                  for r in linked],
                 "facts": facts}], {}

    def _op_contradictions(self, p: dict) -> tuple[list, dict]:
        limit = p.get("limit", 20)
        marks = ",".join("?" * len(intel.FUNCTIONAL))
        rows = self.db.query(
            f"SELECT c.subject_norm, c.predicate FROM intel_claims c JOIN resources r ON r.id = c.resource_id "
            f"WHERE c.predicate IN ({marks}) AND r.duplicate_of IS NULL GROUP BY c.subject_norm, c.predicate "
            f"HAVING COUNT(DISTINCT c.value_norm) > 1 AND COUNT(DISTINCT r.site) > 1 LIMIT 500", intel.FUNCTIONAL)
        out = []
        for s, pr in rows:
            verdict = self.judge(s, pr)
            if verdict["status"] == "conflicting":
                out.append(verdict)
            if len(out) >= limit:
                break
        return out, {}

    def _op_status(self, p: dict) -> tuple[list, dict]:
        one = lambda sql: self.db.query_one(sql)[0]  # noqa: E731
        cats = {r[0]: r[1] for r in self.db.query("SELECT category, COUNT(*) FROM intel_pages GROUP BY category")}
        return [{"pages_read": one("SELECT COUNT(*) FROM intel_pages"), "pages_waiting": len(self.pending(100_000)),
                 "facts": one("SELECT COUNT(*) FROM intel_claims"),
                 "entities": one("SELECT COUNT(*) FROM intel_entities WHERE mentions > 0"),
                 "relations": one("SELECT COUNT(*) FROM intel_relations"), "categories": cats}], {}
