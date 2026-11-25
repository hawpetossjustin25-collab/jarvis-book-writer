"""OS1 crawl settings: one validated, frozen record, loaded from nexus.json in Jarvis's home when it exists."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

log = logging.getLogger(__name__)

USER_AGENT = "JarvisNexus/1.0 (+https://github.com/hawpetossjustin25-collab/jarvis-book-writer)"
_LIMITS = {  # name: (lowest, highest)
    "max_pages": (1, 100_000), "max_depth": (0, 50), "concurrency": (1, 32), "host_delay_s": (0.0, 120.0),
    "timeout_s": (1.0, 300.0), "retries": (0, 10), "backoff_s": (0.0, 60.0), "max_retry_wait_s": (0.0, 3600.0),
    "max_bytes": (10_000, 50_000_000), "max_text_chars": (1_000, 2_000_000), "queue_limit": (10, 10_000_000),
    "max_redirects": (0, 20), "links_per_page": (1, 10_000), "revisit_min_s": (60.0, 31_536_000.0),
    "revisit_max_s": (60.0, 31_536_000.0), "robots_ttl_s": (60.0, 604_800.0), "cache_ttl_s": (0.0, 604_800.0),
}


@dataclass(frozen=True)
class CrawlConfig:
    max_pages: int = 100  # pages fetched in one crawl
    max_depth: int = 3  # link hops from a seed
    concurrency: int = 4  # pages in flight at once (one per host at a time)
    host_delay_s: float = 1.0  # at least this long between two requests to one host (robots.txt can ask for more)
    timeout_s: float = 20.0
    retries: int = 2  # extra tries after a network error, a timeout, 429 or 5xx
    backoff_s: float = 1.0  # first wait before a retry; doubles each time
    max_retry_wait_s: float = 120.0  # the longest Retry-After that is honored
    max_bytes: int = 3_000_000  # bigger responses are cut here
    max_text_chars: int = 200_000  # text kept per page
    queue_limit: int = 20_000  # links waiting to be fetched
    max_redirects: int = 5
    links_per_page: int = 300  # links taken from one page
    same_site: bool = True  # follow links only within the seeds' sites
    allowed_domains: tuple[str, ...] = ()  # extra sites that may be followed
    blocked_domains: tuple[str, ...] = ()  # never fetched
    revisit_min_s: float = 6 * 3600.0  # a page that keeps changing is revisited this often at most
    revisit_max_s: float = 30 * 86400.0  # a page that never changes is still checked this often
    robots_ttl_s: float = 86400.0
    cache_ttl_s: float = 900.0  # search results are reused this long unless the index changed
    user_agent: str = USER_AGENT

    def __post_init__(self):
        for name, (low, high) in _LIMITS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            if not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
        if self.revisit_min_s > self.revisit_max_s:
            raise ValueError("revisit_min_s can't be more than revisit_max_s")
        if not isinstance(self.same_site, bool):
            raise ValueError("same_site must be true or false")
        for name in ("allowed_domains", "blocked_domains"):
            value = getattr(self, name)
            if isinstance(value, str) or not all(isinstance(d, str) and d.strip() for d in value):
                raise ValueError(f"{name} must be a list of domain names")
            object.__setattr__(self, name, tuple(d.strip().lower().lstrip(".") for d in value))
        if not self.user_agent.strip():
            raise ValueError("user_agent can't be empty")

    @classmethod
    def from_dict(cls, data: dict) -> "CrawlConfig":
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown crawl settings: {', '.join(unknown)}")
        data = {k: tuple(v) if isinstance(v, list) else v for k, v in data.items()}
        return cls(**data)

    def with_(self, **changes) -> "CrawlConfig":
        return replace(self, **{k: v for k, v in changes.items() if v is not None})

    def to_dict(self) -> dict:
        out = asdict(self)
        out["allowed_domains"], out["blocked_domains"] = list(self.allowed_domains), list(self.blocked_domains)
        return out


def load_config(path: Path) -> CrawlConfig:
    """Settings from `path`, or the defaults when it is missing. A broken file is reported and ignored."""
    if not path.exists():
        return CrawlConfig()
    try:
        return CrawlConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, TypeError, OSError) as exc:
        log.warning("nexus: ignoring %s (%s); using the default crawl settings", path.name, exc)
        return CrawlConfig()
