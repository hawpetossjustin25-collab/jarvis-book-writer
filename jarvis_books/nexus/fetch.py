"""Web retrieval for OS1: async HTTP with the network guard, robots.txt, per-host politeness, retries and limits."""
from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib import robotparser
from urllib.parse import urlsplit, urlunsplit

import httpx

from jarvis_books.nexus.config import CrawlConfig
from jarvis_books.nexus.urls import NetworkGuard, UnsafeURL, normalize

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
ACCEPT = "text/html,application/xhtml+xml,text/plain;q=0.9,application/json;q=0.8,application/xml;q=0.7,*/*;q=0.5"


@dataclass
class FetchResult:
    url: str  # what was asked for
    final_url: str  # where it ended up after redirects
    status: int = 0
    content_type: str = ""
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    elapsed_ms: float = 0.0
    redirects: list[str] = field(default_factory=list)
    attempts: int = 0
    truncated: bool = False
    not_modified: bool = False
    error: str = ""
    error_kind: str = ""  # unsafe, robots, network, timeout, http, too_many_redirects
    retry_after_s: Optional[float] = None

    @property
    def ok(self) -> bool:
        return not self.error and 200 <= self.status < 300


def retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Seconds from a Retry-After header (a number of seconds or an HTTP date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        return None
    return max(0.0, when - (now if now is not None else time.time()))


class HostPacer:
    """Keeps at least `delay` seconds between the starts of two requests to one host, across all workers."""

    def __init__(self, clock=time.monotonic, sleep=asyncio.sleep):
        self.clock, self.sleep = clock, sleep
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.waited_s = 0.0

    async def wait(self, host: str, delay: float) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            pause = self._next.get(host, 0.0) - self.clock()
            if pause > 0:
                self.waited_s += pause
                await self.sleep(pause)
            self._next[host] = self.clock() + delay

    def hold(self, host: str, seconds: float) -> None:
        """Keeps away from a host that asked us to slow down (429 / 503 Retry-After)."""
        self._next[host] = max(self._next.get(host, 0.0), self.clock() + seconds)


class RobotsPolicy:
    """robots.txt per host, cached. A missing file (4xx) allows everything; an erroring one (5xx or unreachable)
    blocks the host for a while, as the robots standard (RFC 9309) asks."""

    def __init__(self, fetcher: "Fetcher", ttl_s: float, clock=time.monotonic):
        self.fetcher, self.ttl_s, self.clock = fetcher, ttl_s, clock
        self._cache: dict[str, tuple[float, Optional[robotparser.RobotFileParser], bool]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _origin(url: str) -> str:
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.netloc, "", "", ""))

    async def _rules(self, url: str) -> tuple[Optional[robotparser.RobotFileParser], bool]:
        origin = self._origin(url)
        hit = self._cache.get(origin)
        if hit and self.clock() - hit[0] < self.ttl_s:
            return hit[1], hit[2]
        async with self._locks.setdefault(origin, asyncio.Lock()):
            hit = self._cache.get(origin)
            if hit and self.clock() - hit[0] < self.ttl_s:
                return hit[1], hit[2]
            result = await self.fetcher.fetch(origin + "/robots.txt", check_robots=False, retries=1)
            if result.ok:
                parser = robotparser.RobotFileParser()
                parser.parse(result.body.decode("utf-8", "replace").splitlines())
                entry = (self.clock(), parser, False)
            elif 400 <= result.status < 500:
                entry = (self.clock(), None, False)
            else:  # 5xx or unreachable: assume everything is disallowed, look again in an hour at most
                entry = (self.clock() - max(0.0, self.ttl_s - 3600), None, True)
            self._cache[origin] = entry
            return entry[1], entry[2]

    async def allowed(self, url: str, agent: str) -> bool:
        if urlsplit(url).path == "/robots.txt":
            return True
        parser, blocked = await self._rules(url)
        if blocked:
            return False
        return parser.can_fetch(agent, url) if parser else True

    async def delay(self, url: str, agent: str) -> float:
        parser, _ = await self._rules(url)
        if not parser:
            return 0.0
        value = parser.crawl_delay(agent)
        if value is None:
            rate = parser.request_rate(agent)
            return rate.seconds / rate.requests if rate and rate.requests else 0.0
        return float(value)


class Fetcher:
    """Fetches public web pages politely. Use as `async with Fetcher(config) as f: await f.fetch(url)`."""

    def __init__(self, config: CrawlConfig, client: Optional[httpx.AsyncClient] = None,
                 guard: Optional[NetworkGuard] = None, clock=time.monotonic, sleep=asyncio.sleep,
                 rng: Optional[random.Random] = None):
        self.config = config
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_s), follow_redirects=False, http2=False,
            limits=httpx.Limits(max_connections=config.concurrency * 2, max_keepalive_connections=config.concurrency),
            headers={"User-Agent": config.user_agent, "Accept": ACCEPT, "Accept-Language": "en;q=1.0, *;q=0.5"})
        self.guard = guard or NetworkGuard()
        self.clock, self.sleep = clock, sleep
        self.rng = rng or random.Random()
        self.pacer = HostPacer(clock, sleep)
        self.robots = RobotsPolicy(self, config.robots_ttl_s, clock)
        self._slots = asyncio.Semaphore(config.concurrency)

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()

    async def fetch(self, url: str, etag: Optional[str] = None, last_modified: Optional[str] = None,
                    check_robots: bool = True, retries: Optional[int] = None) -> FetchResult:
        """Never raises for network trouble: failures come back in `error` / `error_kind`."""
        tries = (self.config.retries if retries is None else retries) + 1
        started = time.perf_counter()
        result = FetchResult(url, url)
        for attempt in range(tries):
            result = await self._follow(url, etag, last_modified, check_robots)
            result.attempts = attempt + 1
            retryable = result.error_kind in ("network", "timeout") or result.status in RETRY_STATUS
            if not retryable or attempt == tries - 1:
                break
            wait = self.config.backoff_s * (2 ** attempt) * (0.5 + self.rng.random())
            if result.retry_after_s is not None:
                if result.retry_after_s > self.config.max_retry_wait_s:
                    break
                wait = max(wait, result.retry_after_s)
            log.info("nexus fetch retry %d for %s in %.1fs (%s)", attempt + 1, url, wait,
                     result.error_kind or result.status)
            await self.sleep(wait)
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        return result

    async def _follow(self, url: str, etag: Optional[str], last_modified: Optional[str],
                      check_robots: bool) -> FetchResult:
        result = FetchResult(url, url)
        current = url
        for hop in range(self.config.max_redirects + 1):
            try:
                await self.guard.check(current)
            except UnsafeURL as exc:
                result.error, result.error_kind = str(exc), "unsafe"
                return result
            if check_robots:
                if not await self.robots.allowed(current, self.config.user_agent):
                    result.error, result.error_kind = "robots.txt asks crawlers not to open this page", "robots"
                    return result
                delay = max(self.config.host_delay_s, await self.robots.delay(current, self.config.user_agent))
            else:
                delay = self.config.host_delay_s
            host = urlsplit(current).hostname or ""
            await self.pacer.wait(host, delay)
            headers = {}
            if hop == 0 and etag:
                headers["If-None-Match"] = etag
            if hop == 0 and last_modified:
                headers["If-Modified-Since"] = last_modified
            async with self._slots:
                outcome = await self._get(current, headers, result)
            if outcome is None:
                return result
            location = outcome
            if not location:
                result.final_url = current
                return result
            nxt = normalize(location, current)
            if not nxt:
                result.error, result.error_kind = f"redirect to an unsupported address: {location[:200]}", "http"
                return result
            result.redirects.append(nxt)
            current = result.final_url = nxt
        result.error, result.error_kind = "too many redirects", "too_many_redirects"
        return result

    async def _get(self, url: str, headers: dict, result: FetchResult) -> Optional[str]:
        """One request. Returns a redirect target, '' when done, or None after a failure (recorded in result)."""
        try:
            async with self.client.stream("GET", url, headers=headers) as resp:
                result.status = resp.status_code
                result.headers = {k.lower(): v for k, v in resp.headers.items()}
                result.content_type = resp.headers.get("content-type", "")
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        result.error, result.error_kind = "redirect without a destination", "http"
                        return None
                    return location
                if resp.status_code == 304:
                    result.not_modified = True
                    return ""
                if resp.status_code >= 400:
                    result.retry_after_s = retry_after(resp.headers.get("retry-after"))
                    if resp.status_code in (429, 503):
                        self.pacer.hold(urlsplit(url).hostname or "",
                                        min(result.retry_after_s or self.config.backoff_s * 4,
                                            self.config.max_retry_wait_s))
                    result.error, result.error_kind = f"the site answered {resp.status_code}", "http"
                    return None
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    room = self.config.max_bytes - size
                    chunks.append(chunk[:room])
                    size += len(chunk[:room])
                    if size >= self.config.max_bytes:
                        result.truncated = True
                        break
                result.body = b"".join(chunks)
                return ""
        except httpx.TimeoutException:
            result.error, result.error_kind = f"timed out after {self.config.timeout_s:.0f}s", "timeout"
        except (httpx.TransportError, httpx.DecodingError) as exc:
            result.error, result.error_kind = f"network error ({type(exc).__name__})", "network"
        except httpx.InvalidURL as exc:
            result.error, result.error_kind = f"bad address ({exc})", "unsafe"
        return None
