"""Recursive exploration: a persistent, prioritized frontier worked by bounded concurrent workers."""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable, Optional

from jarvis_books.nexus.config import CrawlConfig
from jarvis_books.nexus.dedup import content_hash, simhash, words
from jarvis_books.nexus.fetch import Fetcher, FetchResult
from jarvis_books.nexus.parse import Link, ParsedPage, parse_feed, parse_html, parse_text, resource_type
from jarvis_books.nexus.store import GraphStore, PageRecord
from jarvis_books.nexus.urls import host_of, in_domain, looks_binary, normalize, site_of

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None]
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are", "how", "what", "about",
         "from", "by", "at", "it", "this", "that", "be", "as", "do", "does", "i", "my", "me"}


@dataclass
class CrawlReport:
    run_id: int
    status: str  # done, stopped, limit
    pages_fetched: int = 0
    pages_new: int = 0
    pages_changed: int = 0
    pages_unchanged: int = 0
    duplicates: int = 0
    failed: int = 0
    blocked: int = 0  # robots.txt or the network guard
    links_found: int = 0
    queued_left: int = 0
    bytes: int = 0
    seconds: float = 0.0
    cpu_seconds: float = 0.0
    pages_per_minute: float = 0.0
    avg_fetch_ms: float = 0.0
    p95_fetch_ms: float = 0.0
    polite_wait_s: float = 0.0
    errors: dict = field(default_factory=dict)
    sample: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def topic_terms(topic: str) -> set[str]:
    return {w for w in words(topic) if w not in _STOP and len(w) > 1}


def link_relevance(terms: set[str], url: str, anchor: str) -> float:
    """0..1: how many topic words appear in the link's text and address."""
    if not terms:
        return 0.5
    found = set(words(anchor)) | set(words(re.sub(r"[-_/.]", " ", url)))
    return len(terms & found) / len(terms)


def page_relevance(terms: set[str], page: ParsedPage) -> float:
    if not terms:
        return 0.5
    head = set(words(page.title + " " + page.description))
    body = words(page.text[:20000])
    counts = {t: body.count(t) for t in terms}
    covered = sum(1 for t in terms if counts[t] or t in head) / len(terms)
    in_head = len(terms & head) / len(terms)
    return round(min(1.0, 0.6 * covered + 0.4 * in_head), 4)


class Crawler:
    def __init__(self, store: GraphStore, config: CrawlConfig,
                 fetcher_factory: Callable[[CrawlConfig], Fetcher] = Fetcher):
        self.store, self.config, self.fetcher_factory = store, config, fetcher_factory

    def blocked(self, url: str) -> bool:
        host = host_of(url)
        return any(in_domain(host, d) for d in self.config.blocked_domains)

    def scope(self, seeds: Iterable[str]) -> Callable[[str], bool]:
        """Which links are followed: links are always recorded in the graph, but only these get fetched."""
        sites = {site_of(host_of(s)) for s in seeds}
        allowed, blocked = self.config.allowed_domains, self.config.blocked_domains

        def ok(url: str) -> bool:
            host = host_of(url)
            if any(in_domain(host, d) for d in blocked):
                return False
            if not self.config.same_site:
                return True
            return site_of(host) in sites or any(in_domain(host, d) for d in allowed)
        return ok

    def crawl(self, seeds: list[str], topic: str = "", stop: Optional[threading.Event] = None,
              progress: Optional[Progress] = None, resume_run: Optional[int] = None) -> CrawlReport:
        """Runs a crawl to the end (blocking). `stop` ends it early; queued links stay saved for `resume_run`."""
        return asyncio.run(self.crawl_async(seeds, topic, stop, progress, resume_run))

    async def crawl_async(self, seeds: list[str], topic: str = "", stop: Optional[threading.Event] = None,
                          progress: Optional[Progress] = None, resume_run: Optional[int] = None) -> CrawlReport:
        cfg = self.config
        clean = [u for u in dict.fromkeys(normalize(s) for s in seeds) if u]
        if resume_run is not None:
            run = self.store.run(resume_run)
            if not run:
                raise ValueError(f"there is no crawl #{resume_run}")
            run_id, topic, clean = resume_run, run["topic"], run["seeds"]
            self.store.db.execute("UPDATE crawl_runs SET status='running' WHERE id=?", (run_id,))
        else:
            if not clean:
                raise ValueError("no valid http(s) starting address")
            run_id = self.store.start_run(topic, clean, cfg.to_dict())
            for url in clean:
                self.store.enqueue(run_id, self.store.ensure(url), 2.0, 0)
        terms = topic_terms(topic)
        in_scope = self.scope(clean)
        report = CrawlReport(run_id, "running")
        timings: list[float] = []
        busy_hosts: set[str] = set()
        claimed = [0]  # pages taken from the frontier, so concurrent workers never pass max_pages
        started, cpu = time.perf_counter(), time.process_time()
        say = progress or (lambda p, m: None)

        async with self.fetcher_factory(cfg) as fetcher:
            async def worker() -> None:
                while claimed[0] < cfg.max_pages:
                    if stop is not None and stop.is_set():
                        return
                    item = self.store.pop(run_id, busy_hosts)
                    if item is None:
                        if not busy_hosts:
                            return
                        await asyncio.sleep(0.05)
                        continue
                    claimed[0] += 1
                    rid, url, depth = item
                    host = host_of(url)
                    busy_hosts.add(host)
                    try:
                        await self._visit(fetcher, run_id, rid, url, depth, terms, in_scope, report, timings)
                    except Exception:
                        log.exception("nexus: visiting %s failed", url)
                        report.failed += 1
                        report.errors["internal"] = report.errors.get("internal", 0) + 1
                        self.store.record_failure(rid, "failed", "internal error while processing", None, None)
                    finally:
                        busy_hosts.discard(host)
                    done = report.pages_fetched + report.failed + report.blocked
                    say(min(0.95, done / cfg.max_pages), f"{done}/{cfg.max_pages} pages, {report.links_found} links")

            await asyncio.gather(*(worker() for _ in range(cfg.concurrency)))
            report.polite_wait_s = round(fetcher.pacer.waited_s, 2)

        report.queued_left = self.store.frontier_size(run_id)
        if stop is not None and stop.is_set():
            report.status = "stopped"
        elif report.queued_left:
            report.status = "limit"
        else:
            report.status = "done"
            self.store.clear_frontier(run_id)
        say(0.97, "ranking pages")
        self.store.compute_rank()
        self.store.bump_generation()
        report.seconds = round(time.perf_counter() - started, 3)
        report.cpu_seconds = round(time.process_time() - cpu, 3)
        report.pages_per_minute = round(report.pages_fetched / report.seconds * 60, 2) if report.seconds else 0.0
        if timings:
            ordered = sorted(timings)
            report.avg_fetch_ms = round(sum(ordered) / len(ordered), 1)
            report.p95_fetch_ms = round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1)
        self.store.finish_run(run_id, report.status, report.to_dict())
        self.store.record_metrics(run_id, {
            "crawl.pages_per_minute": report.pages_per_minute, "crawl.avg_fetch_ms": report.avg_fetch_ms,
            "crawl.p95_fetch_ms": report.p95_fetch_ms, "crawl.failure_rate":
                (report.failed / max(1, report.pages_fetched + report.failed)),
            "crawl.duplicate_rate": report.duplicates / max(1, report.pages_fetched),
            "crawl.cpu_seconds": report.cpu_seconds, "crawl.bytes": report.bytes})
        log.info("nexus crawl #%d %s: %d fetched, %d new, %d changed, %d failed, %d blocked in %.1fs", run_id,
                 report.status, report.pages_fetched, report.pages_new, report.pages_changed, report.failed,
                 report.blocked, report.seconds)
        return report

    async def _visit(self, fetcher: Fetcher, run_id: int, rid: int, url: str, depth: int, terms: set[str],
                     in_scope: Callable[[str], bool], report: CrawlReport, timings: list[float]) -> None:
        cfg = self.config
        etag, modified = self.store.conditional_headers(rid)
        result = await fetcher.fetch(url, etag, modified)
        timings.append(result.elapsed_ms)
        report.bytes += len(result.body)
        if result.not_modified:
            report.pages_fetched += 1
            report.pages_unchanged += 1
            self.store.record_unchanged(rid, cfg.revisit_max_s)
            self.store.log_visit(rid, run_id, "not_modified", 304, result.elapsed_ms, 0, None)
            known = [Link(e["url"], e["anchor"] or "", e["rel"], e["rel"] == "nofollow")
                     for e in self.store.outgoing(rid, cfg.links_per_page) if e["rel"] in ("link", "nofollow", "next",
                                                                                          "prev", "alternate")]
            self._follow(run_id, rid, depth, known, terms, in_scope, report)
            return
        if not result.ok:
            self._failed(rid, run_id, result, report)
            return
        final = normalize(result.final_url) or url
        if final != url:
            target = self.store.ensure(final, rid, depth)
            self.store.add_edge(rid, target, rel="redirect", relevance=1.0)
        ctype = resource_type(result.content_type, final)
        robots_header = result.headers.get("x-robots-tag", "")
        if ctype in ("page", "article"):
            page = parse_html(result.body, final, cfg.max_text_chars, cfg.links_per_page, robots_header)
        elif ctype in ("text", "data", "feed"):
            feed = parse_feed(result.body, final, cfg.max_text_chars, cfg.links_per_page) \
                if ctype == "feed" or result.body.lstrip()[:1] == b"<" else None
            if feed is not None:
                page = feed
            else:
                page = parse_text(result.body.decode("utf-8", "replace"), cfg.max_text_chars)
                page.resource_type = ctype
        else:
            page = ParsedPage(title=final.rsplit("/", 1)[-1][:200], resource_type=ctype)
        report.pages_fetched += 1
        digest = content_hash(page.title + "\n" + page.text)
        sim = simhash(page.text) if len(page.text) >= 200 else 0
        duplicate = self.store.find_duplicate(rid, digest, sim)
        if page.canonical and page.canonical != url and page.canonical != final and in_scope(page.canonical):
            canon = self.store.ensure(page.canonical, rid, depth)
            self.store.add_edge(rid, canon, rel="canonical", relevance=1.0)
        trust = {"https": final.startswith("https://"), "redirects": len(result.redirects),
                 "truncated": result.truncated, "content_type": result.content_type.split(";")[0][:80],
                 "canonical": page.canonical, "noindex": page.noindex}
        status = "noindex" if page.noindex else "fetched"
        record = PageRecord(final, result.status, page.title, page.description, page.text, page.resource_type,
                            page.lang, digest, sim, result.headers.get("etag"), result.headers.get("last-modified"),
                            trust, status, page.published, page.author, page.meta)
        before = self.store.resource(rid)
        changed = self.store.record_page(rid, record, duplicate, cfg.revisit_min_s, cfg.revisit_max_s)
        self.store.db.execute("UPDATE resources SET relevance=MAX(relevance, ?) WHERE id=?",
                              (page_relevance(terms, page), rid))
        outcome = "duplicate" if duplicate else ("changed" if changed else
                                                 ("new" if not (before and before["content_hash"]) else "unchanged"))
        report.duplicates += bool(duplicate)
        report.pages_new += outcome == "new"
        report.pages_changed += outcome == "changed"
        report.pages_unchanged += outcome == "unchanged"
        self.store.log_visit(rid, run_id, outcome, result.status, result.elapsed_ms, len(result.body), digest)
        if len(report.sample) < 8 and not duplicate and page.title:
            report.sample.append({"title": page.title, "url": final})
        if page.nofollow or duplicate:
            return
        self._follow(run_id, rid, depth, page.links, terms, in_scope, report)

    def _follow(self, run_id: int, rid: int, depth: int, links: list[Link], terms: set[str],
                in_scope: Callable[[str], bool], report: CrawlReport) -> None:
        cfg = self.config
        for link in links:
            if looks_binary(link.url) or self.blocked(link.url):
                continue
            score = link_relevance(terms, link.url, link.anchor)
            child = self.store.ensure(link.url, rid, depth + 1, score)
            self.store.add_edge(rid, child, link.anchor, link.rel, score)
            report.links_found += 1
            if not in_scope(link.url) or link.nofollow or depth + 1 > cfg.max_depth or \
                    self.store.visited_in_run(run_id, child):
                continue
            node = self.store.db.query_one("SELECT status, next_visit FROM resources WHERE id=?", (child,))
            fresh = node["status"] == "discovered" or (node["next_visit"] or 0) <= self.store.clock()
            if fresh and self.store.frontier_size(run_id) < cfg.queue_limit:
                self.store.enqueue(run_id, child, round(0.7 * score + 0.3 / (depth + 2), 4), depth + 1)

    def _failed(self, rid: int, run_id: int, result: FetchResult, report: CrawlReport) -> None:
        kind = result.error_kind or "http"
        report.errors[kind] = report.errors.get(kind, 0) + 1
        if kind in ("robots", "unsafe"):
            report.blocked += 1
            self.store.mark_status(rid, "blocked", result.error)
        else:
            report.failed += 1
            self.store.record_failure(rid, "failed", result.error, result.status or None, result.retry_after_s)
        self.store.log_visit(rid, run_id, kind, result.status or None, result.elapsed_ms, len(result.body), None,
                             result.error)

    def revisit(self, limit: int = 50, stop: Optional[threading.Event] = None,
                progress: Optional[Progress] = None) -> CrawlReport:
        """Re-fetches pages whose revisit time has come, to catch changes. No new links are followed."""
        due = self.store.due(limit)
        cfg = self.config.with_(max_depth=0, max_pages=max(1, len(due)), same_site=True)
        run_id = self.store.start_run("revisit", [u for _, u in due], cfg.to_dict())
        for rid, _ in due:
            self.store.enqueue(run_id, rid, 1.0, 0)
        crawler = Crawler(self.store, cfg, self.fetcher_factory)
        return crawler.crawl([], "", stop, progress, resume_run=run_id)
