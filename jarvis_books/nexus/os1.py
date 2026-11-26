"""OS1, the observation system: the one entry point Jarvis (and later OS2/OS3) uses, over NEXUS contracts."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from jarvis_books.nexus.config import CrawlConfig, load_config
from jarvis_books.nexus.contracts import ContractError, NexusRequest, NexusResponse, failure, success, \
    validate_payload
from jarvis_books.nexus.crawler import Crawler, Progress
from jarvis_books.nexus.fetch import Fetcher
from jarvis_books.nexus.rsi import Improver
from jarvis_books.nexus.search import Embedder, HashedEmbedder, SearchEngine, embed_pending
from jarvis_books.nexus.store import GraphStore
from jarvis_books.nexus.urls import normalize

log = logging.getLogger(__name__)

_URL = {"type": "string", "minLength": 4, "maxLength": 2000}
PAYLOADS = {
    "search": {"type": "object", "required": ["query"], "additionalProperties": False, "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 500},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
        "site": {"type": "string", "maxLength": 253}, "type": {"type": "string", "maxLength": 20},
        "use_cache": {"type": "boolean"}}},
    "crawl": {"type": "object", "additionalProperties": False, "properties": {
        "seeds": {"type": "array", "items": _URL, "maxItems": 50},
        "topic": {"type": "string", "maxLength": 300},
        "max_pages": {"type": "integer", "minimum": 1, "maximum": 100_000},
        "max_depth": {"type": "integer", "minimum": 0, "maximum": 50},
        "same_site": {"type": "boolean"},
        "resume": {"type": "boolean"}}},
    "revisit": {"type": "object", "additionalProperties": False, "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 10_000}}},
    "resource": {"type": "object", "required": ["url"], "additionalProperties": False, "properties": {"url": _URL}},
    "status": {"type": "object", "additionalProperties": False, "properties": {}},
    "history": {"type": "object", "additionalProperties": False, "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 500}}},
    "embed": {"type": "object", "additionalProperties": False, "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 100_000}}},
    "improve": {"type": "object", "additionalProperties": False, "properties": {
        "target": {"enum": ["search", "crawl", "both"]},
        "rounds": {"type": "integer", "minimum": 1, "maximum": 20},
        "candidates": {"type": "integer", "minimum": 1, "maximum": 30},
        "seed": {"type": "integer"}}},
    "rsi": {"type": "object", "required": ["action"], "additionalProperties": False, "properties": {
        "action": {"enum": ["status", "history", "rollback"]},
        "kind": {"enum": ["search", "crawl"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500}}},
}


class OS1:
    """Owns the resource graph, crawler and search engine. Thread-safe; one crawl runs at a time."""

    def __init__(self, home: Path | str, config: Optional[CrawlConfig] = None,
                 embedder: Optional[Embedder] = None, fetcher_factory: Callable[[CrawlConfig], Fetcher] = Fetcher,
                 seed_search: Optional[Callable[[str, int], list[str]]] = None):
        home = Path(home)
        self.config = config or load_config(home / "nexus.json")
        self.store = GraphStore(home / "nexus.db")
        self.embedder = embedder
        self.backup_embedder = HashedEmbedder()
        self.fetcher_factory = fetcher_factory
        self.seed_search = seed_search
        self.search_engine = SearchEngine(self.store, embedder, self.config.cache_ttl_s)
        self._crawl_lock = threading.Lock()
        self._improve_lock = threading.Lock()
        self.improver = Improver(self)
        self.improver.apply()  # the tuned ranking and crawl settings that won their tests

    def close(self) -> None:
        self.store.close()

    # ---- contract entry point
    def handle(self, request: NexusRequest | dict | str, stop: Optional[threading.Event] = None,
               progress: Optional[Progress] = None) -> NexusResponse:
        started = time.perf_counter()
        try:
            if isinstance(request, str):
                request = NexusRequest.from_json(request)
            elif isinstance(request, dict):
                request = NexusRequest.from_dict(request)
        except ContractError as exc:
            raw = request if isinstance(request, dict) else {}
            bogus = NexusRequest("jarvis", "os1", "invalid", {}, str(raw.get("request_id") or "invalid")[:100])
            return failure(bogus, "os1", exc.code, str(exc), started)
        if request.destination != "os1":
            return failure(request, "os1", "invalid_request", f"this is OS1, not {request.destination}", started)
        schema = PAYLOADS.get(request.operation)
        if schema is None:
            return failure(request, "os1", "unknown_operation",
                           f"OS1 can do: {', '.join(sorted(PAYLOADS))}", started)
        try:
            validate_payload(schema, request.payload)
            results, extra = getattr(self, "_op_" + request.operation)(request.payload, stop, progress)
        except ContractError as exc:
            return failure(request, "os1", exc.code, str(exc), started)
        except ValueError as exc:
            return failure(request, "os1", "invalid_request", str(exc), started)
        except Exception as exc:
            log.exception("nexus os1 %s failed", request.operation)
            return failure(request, "os1", "internal", f"{type(exc).__name__}: {exc}", started)
        log.info("nexus os1 %s %s ok in %.0f ms", request.request_id[:12], request.operation,
                 (time.perf_counter() - started) * 1000)
        return success(request, "os1", results, started, **extra)

    def call(self, operation: str, payload: dict, stop: Optional[threading.Event] = None,
             progress: Optional[Progress] = None, correlation_id: Optional[str] = None) -> NexusResponse:
        return self.handle(NexusRequest("jarvis", "os1", operation, payload, correlation_id=correlation_id),
                           stop, progress)

    # ---- operations
    def _op_search(self, p: dict, stop, progress) -> tuple[list, dict]:
        out = self.search_engine.search(p["query"], p.get("max_results", 10), p.get("site"), p.get("type"),
                                        p.get("use_cache", True))
        return out["results"], {"expanded": out["expanded"], "spelling": out["spelling"], "cached": out["cached"],
                                "matched": out["matched"], "semantic": out["semantic"],
                                "indexed_pages": self.store.stats()["searchable"]}

    def _op_crawl(self, p: dict, stop, progress) -> tuple[list, dict]:
        if not self._crawl_lock.acquire(blocking=False):
            raise ContractError("a crawl is already running; wait for it or stop it first", "unavailable")
        try:
            cfg = self.config.with_(max_pages=p.get("max_pages"), max_depth=p.get("max_depth"),
                                    same_site=p.get("same_site"))
            crawler = Crawler(self.store, cfg, self.fetcher_factory)
            topic = p.get("topic", "").strip()
            if p.get("resume"):
                run = self.store.unfinished_run()
                if run is None:
                    raise ContractError("there is no stopped crawl to continue", "not_found")
                report = crawler.crawl([], stop=stop, progress=progress, resume_run=run)
            else:
                seeds = [u for u in (normalize(s) for s in p.get("seeds", [])) if u]
                if not seeds and topic and self.seed_search is not None:
                    seeds = [u for u in (normalize(s) for s in self.seed_search(topic, 5)) if u]
                    cfg = cfg.with_(same_site=p.get("same_site", False))
                    crawler = Crawler(self.store, cfg, self.fetcher_factory)
                if not seeds:
                    raise ContractError("give a starting web address (or a topic, when web search is available)")
                report = crawler.crawl(seeds, topic, stop, progress)
            vectors = self._embed(200)
            return [report.to_dict()], {"vectors_added": vectors}
        finally:
            self._crawl_lock.release()

    def _op_revisit(self, p: dict, stop, progress) -> tuple[list, dict]:
        if not self._crawl_lock.acquire(blocking=False):
            raise ContractError("a crawl is already running", "unavailable")
        try:
            due = len(self.store.due(p.get("limit", 50)))
            if not due:
                return [], {"due": 0}
            report = Crawler(self.store, self.config, self.fetcher_factory).revisit(p.get("limit", 50), stop, progress)
            return [report.to_dict()], {"due": due, "vectors_added": self._embed(200)}
        finally:
            self._crawl_lock.release()

    def _op_resource(self, p: dict, stop, progress) -> tuple[list, dict]:
        url = normalize(p["url"])
        node = self.store.by_url(url) if url else None
        if node is None:
            raise ContractError("that page isn't in the index yet", "not_found")
        out_n, in_n = self.store.link_counts(node["id"])
        node = {k: v for k, v in node.items() if k != "text"} | {
            "text_preview": (self.store.resource(node["id"])["text"] or "")[:1500],
            "outgoing_count": out_n, "incoming_count": in_n,
            "outgoing": self.store.outgoing(node["id"], 25), "incoming": self.store.incoming(node["id"], 25),
            "visits": self.store.visits(node["id"], 10)}
        return [node], {}

    def _op_status(self, p: dict, stop, progress) -> tuple[list, dict]:
        stats = self.store.stats()
        stats["crawl_running"] = self._crawl_lock.locked()
        stats["unfinished_crawl"] = self.store.unfinished_run()
        stats["search_cache_hits"] = self.search_engine.cache_hits
        stats["search_cache_misses"] = self.search_engine.cache_misses
        stats["meaning_search"] = self.embedder.model if self.embedder else self.backup_embedder.model
        stats["settings"] = self.config.to_dict()
        return [stats], {}

    def _op_history(self, p: dict, stop, progress) -> tuple[list, dict]:
        limit = p.get("limit", 20)
        runs = [dict(r) for r in self.store.db.query(
            "SELECT id, started_at, finished_at, status, topic, seeds, stats FROM crawl_runs ORDER BY id DESC "
            "LIMIT ?", (limit,))]
        return [{"searches": self.store.search_history(limit), "crawls": runs}], {}

    def _op_improve(self, p: dict, stop, progress) -> tuple[list, dict]:
        if not self._improve_lock.acquire(blocking=False):
            raise ContractError("a self-improvement round is already running; wait for it", "unavailable")
        try:
            target = p.get("target", "both")
            out = []
            if target in ("search", "both"):
                out.append(self.improver.improve_search(p.get("rounds", 1), p.get("candidates", 6), p.get("seed")))
            if target in ("crawl", "both"):
                out.append(self.improver.improve_crawl())
            return out, {}
        finally:
            self._improve_lock.release()

    def _op_rsi(self, p: dict, stop, progress) -> tuple[list, dict]:
        action = p["action"]
        if action == "status":
            return [self.improver.status()], {}
        if action == "history":
            return self.improver.history(p.get("kind"), p.get("limit", 20)), {}
        if "kind" not in p:
            raise ValueError("say which to roll back: search or crawl")
        return [self.improver.rollback(p["kind"])], {}

    def _op_embed(self, p: dict, stop, progress) -> tuple[list, dict]:
        return [], {"vectors_added": self._embed(p.get("limit", 1000))}

    def _embed(self, limit: int) -> int:
        """Meaning vectors for new pages: Gemini when it works, else the offline hashed vectors."""
        for embedder in ([self.embedder] if self.embedder else []) + [self.backup_embedder]:
            try:
                added = embed_pending(self.store, embedder, limit)
                self.search_engine.embedder = embedder
                if added:
                    self.store.bump_generation()
                return added
            except Exception as exc:
                log.info("nexus: %s vectors unavailable (%s)", embedder.model, type(exc).__name__)
        return 0
