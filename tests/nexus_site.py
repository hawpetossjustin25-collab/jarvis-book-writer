"""A fake internet for NEXUS tests: real httpx requests answered by an in-process MockTransport."""
from __future__ import annotations

import asyncio
from typing import Callable, Optional, Union

import httpx

from jarvis_books.nexus.config import CrawlConfig
from jarvis_books.nexus.fetch import Fetcher
from jarvis_books.nexus.urls import NetworkGuard

PUBLIC_IP = "93.184.216.34"
Answer = Union[str, tuple, Callable[[httpx.Request], httpx.Response]]


def page(title: str, body: str = "", links: tuple = (), description: str = "", head: str = "") -> str:
    anchors = "".join(f'<a href="{href}">{text}</a> ' for href, text in links)
    return (f"<html lang='en'><head><title>{title}</title><meta name='description' content='{description}'>{head}"
            f"</head><body><h1>{title}</h1><p>{body}</p><nav>{anchors}</nav>"
            f"<script>var secret = 'ignore me';</script></body></html>")


class FakeWeb:
    """Maps URLs to answers: an HTML string, (status, body, headers), or a function of the request."""

    def __init__(self, routes: Optional[dict[str, Answer]] = None, private: tuple[str, ...] = ()):
        self.routes: dict[str, Answer] = dict(routes or {})
        self.private = set(private)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        answer = self.routes.get(url)
        if answer is None and url.endswith("/robots.txt"):
            return httpx.Response(404, text="")
        if answer is None:
            return httpx.Response(404, text="<html><title>Not found</title></html>",
                                  headers={"content-type": "text/html"})
        if callable(answer):
            return answer(request)
        if isinstance(answer, tuple):
            status, body, headers = (answer + ({},))[:3] if len(answer) == 2 else answer
            return httpx.Response(status, content=body.encode() if isinstance(body, str) else body,
                                  headers={"content-type": "text/html; charset=utf-8", **headers})
        return httpx.Response(200, content=answer.encode(), headers={"content-type": "text/html; charset=utf-8"})

    async def resolve(self, host: str) -> list[str]:
        if host in self.private:
            return ["192.168.1.10"]
        if host.startswith("nowhere"):
            raise OSError("name not known")
        return [PUBLIC_IP]

    def hits(self, url: str) -> int:
        return sum(1 for r in self.requests if str(r.url) == url)

    def factory(self, sleeps: Optional[list] = None) -> Callable[[CrawlConfig], Fetcher]:
        async def fake_sleep(seconds: float) -> None:
            if sleeps is not None:
                sleeps.append(seconds)
            await asyncio.sleep(0)

        def make(config: CrawlConfig) -> Fetcher:
            client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler),
                                       headers={"User-Agent": config.user_agent})
            fetcher = Fetcher(config, client=client, guard=NetworkGuard(self.resolve), sleep=fake_sleep)
            fetcher._own_client = True
            return fetcher
        return make


FAST = CrawlConfig(host_delay_s=0.0, backoff_s=0.0, concurrency=3, retries=1)
