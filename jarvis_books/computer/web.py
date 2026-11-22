"""The internet for Jarvis: web search and pages, Hugging Face and GitHub (read-only), and downloads."""
from __future__ import annotations

import base64
import hashlib
import html
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import time
import zipfile
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, parse_qs, quote, unquote, urlparse

import requests

from jarvis_books.errors import NotFound

log = logging.getLogger(__name__)

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")
UA = "JarvisBookWriter/1.0 (+https://github.com/hawpetossjustin25-collab/jarvis-book-writer)"
MAX_PAGE_BYTES = 3_000_000
MAX_TEXT = 15_000
RESEARCH_CHARS = 4_000  # per page when several pages are read at once
THIN_PAGE = 200  # fewer characters than this: the page needs JavaScript or is a wall
ROBOT_REST_S = 15 * 60  # after DuckDuckGo asks for a robot check (or Bing sends only junk), leave it alone this long
KEEP_FREE_BYTES = 2 * 1024 ** 3
API_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
REDIRECTS = (301, 302, 303, 307, 308)


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "pre", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)

    def text(self) -> str:
        joined = "".join(self.out)
        lines = (re.sub(r"[ \t\r\f\v]+", " ", x).strip() for x in joined.split("\n"))
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class _Scrape(HTMLParser):
    """Pull the structured bits out of a page: title, links, headings, tables, list items."""
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.title = ""
        self._in_title = False
        self.links: list[dict] = []
        self.headings: list[str] = []
        self.lists: list[str] = []
        self.tables: list[list[list[str]]] = []
        self._href = ""
        self._a: list[str] = []
        self._heading = ""
        self._li = ""
        self._table = None
        self._row = None
        self._cell: Optional[list[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
            return
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        a = dict(attrs)
        if tag == "a":
            self._href = a.get("href", "")
            self._a = []
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading = tag
            self._a_head = []
        elif tag == "li":
            self._li = tag
            self._a_li = []
        elif tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            return
        if tag in self.SKIP and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag == "a":
            text = re.sub(r"\s+", " ", "".join(self._a)).strip()
            if self._href and text:
                self.links.append({"text": text[:120], "url": self._href})
            self._href = ""
        elif tag == self._heading:
            self.headings.append(re.sub(r"\s+", " ", "".join(self._a_head)).strip()[:200])
            self._heading = ""
        elif tag == "li" and self._li:
            self.lists.append(re.sub(r"\s+", " ", "".join(self._a_li)).strip()[:200])
            self._li = ""
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip()[:120])
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table[:50])
            self._table = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self.skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        if self._href:
            self._a.append(data)
        if self._heading:
            self._a_head.append(data)
        if self._li:
            self._a_li.append(data)


def page_text(markup: str) -> tuple[str, str]:
    p = _Text()
    p.feed(markup)
    return p.title.strip(), p.text()


def _public(url: str) -> str:
    """Only real internet addresses: no files, no programs on this computer or the home network."""
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("only http and https web addresses work")
    try:
        infos = socket.getaddrinfo(u.hostname, None)
    except socket.gaierror as exc:
        raise NotFound(f"couldn't find {u.hostname} (no internet, or the address is wrong)") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"{u.hostname} is on this computer or the home network, not the internet")
    return url.strip()


_STOP = set("what which when where who whom whose why how does did do is are was were the a an of in on at to for "
             "from with and or about into than that this these those there their them they can could should would "
             "will much many most best tell me find look up search information info please".split())


def _keywords(query: str) -> str:
    return " ".join(w for w in re.findall(r"[\w'-]+", query) if w.lower() not in _STOP)


def _relevant(query: str, result: dict) -> bool:
    """Bing (when it suspects a robot) and Wikipedia (on long questions) send unrelated pages; keep results that share
    the query's key words."""
    words = {w for w in re.findall(r"[a-z0-9]{3,}", query.lower()) if w not in _STOP}
    if not words:
        return True
    hay = " ".join((result.get("title", ""), result.get("snippet", ""), result.get("url", ""))).lower()
    hits = sum(w in hay or _stem(w) in hay for w in words)
    return hits >= max(min(2, len(words)), min(4, (len(words) + 1) // 2))


def _stem(word: str) -> str:
    for end in ("est", "ing", "ed", "es", "s"):
        if word.endswith(end) and len(word) - len(end) >= 4:
            return word[:-len(end)]
    return word


def _bing_target(href: str) -> str:
    """Bing wraps results in bing.com/ck/a?...&u=a1<base64 of the real address>."""
    if "bing.com/ck/a" not in href:
        return href
    u = parse_qs(urlparse(href).query).get("u", [""])[0]
    if not u.startswith("a1"):
        return href
    raw = u[2:]
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return href


def api_headers(text: str) -> dict:
    """Headers as 'Name: value' lines or a JSON object."""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"the headers aren't valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("headers must be a JSON object")
        pairs = [(str(k), str(v)) for k, v in raw.items()]
    else:
        pairs = []
        for line in text.splitlines():
            if line.strip():
                name, sep, value = line.partition(":")
                if not sep:
                    raise ValueError(f"a header line needs 'Name: value', not {line.strip()!r}")
                pairs.append((name.strip(), value.strip()))
    for name, value in pairs:
        if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name) or "\n" in value or "\r" in value:
            raise ValueError(f"{name!r} isn't a valid header")
    return dict(pairs)


class Web:
    def __init__(self, session: Optional[requests.Session] = None, check_address: Callable[[str], str] = _public,
                 timeout_s: float = 20.0):
        self.session = session or requests.Session()
        if "python-requests" in str(self.session.headers.get("User-Agent", "python-requests")):
            self.session.headers["User-Agent"] = UA  # sites like Wikipedia refuse the default one
        self.check = check_address
        self.timeout_s = timeout_s
        self.clock: Callable[[], float] = time.time
        self._ddg_rest_until = 0.0  # DuckDuckGo limits quick searches; asking again only extends the block
        self._bing_rest_until = 0.0  # Bing, once it answers a suspected robot with first-word-only junk
        # A search that doesn't run from this computer (Groq's search tool), for when the free engines block it.
        self.ai_search: Optional[Callable[[str, int], list[dict]]] = None

    def _get(self, url: str, **kw) -> requests.Response:
        try:
            resp = self.session.get(self.check(url), timeout=self.timeout_s, **kw)
        except requests.Timeout as exc:
            raise NotFound(f"{urlparse(url).hostname} took too long to answer") from exc
        except requests.ConnectionError as exc:
            raise NotFound(f"couldn't reach {urlparse(url).hostname} (is the internet on?)") from exc
        if resp.status_code == 404:
            raise NotFound(f"not found: {url}")
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            raise NotFound("GitHub's hourly limit for searches is used up; try again later")
        if resp.status_code >= 400:
            raise NotFound(f"{urlparse(url).hostname} answered {resp.status_code}")
        return resp

    # ---- web APIs -----------------------------------------------------------------------------
    def _send(self, method: str, url: str, **kw) -> requests.Response:
        """One request; redirects are followed by hand so every hop is checked to be on the internet."""
        for _ in range(6):
            self.check(url)
            try:
                resp = self.session.request(method, url, timeout=self.timeout_s, allow_redirects=False,
                                            stream=True, **kw)
            except requests.Timeout as exc:
                raise NotFound(f"{urlparse(url).hostname} took too long to answer") from exc
            except requests.ConnectionError as exc:
                raise NotFound(f"couldn't reach {urlparse(url).hostname} (is the internet on?)") from exc
            except requests.RequestException as exc:
                raise ValueError(f"that request couldn't be sent ({type(exc).__name__})") from exc
            where = resp.headers.get("Location")
            if resp.status_code not in REDIRECTS or not where:
                return resp
            url = urljoin(url, where)
            if resp.status_code == 303 or (resp.status_code in (301, 302) and method == "POST"):
                method = "GET"
                kw.pop("json", None), kw.pop("data", None)
        raise NotFound("that API redirected too many times")

    def call_api(self, url: str, method: str = "GET", headers: str = "", body: str = "", keys=None) -> dict:
        """Call a web API. {key:NAME} slots get the author's saved keys, and the answer comes back scrubbed of them."""
        method = (method or "GET").upper()
        if method not in API_METHODS:
            raise ValueError(f"method must be one of {', '.join(API_METHODS)}")
        fill = keys.fill if keys is not None else (lambda v: v)
        scrub = keys.scrub if keys is not None else (lambda t: t)
        kw: dict = {"headers": fill(api_headers(headers))}
        if body:
            real = fill(body)
            try:
                kw["json"] = json.loads(real)
            except ValueError:
                kw["data"] = real.encode("utf-8")
        resp = self._send(method, fill(url.strip()), **kw)
        data = resp.raw.read(MAX_PAGE_BYTES + 1, decode_content=True) if getattr(resp, "raw", None) else resp.content
        kind = resp.headers.get("Content-Type", "")
        text = scrub(data[:MAX_PAGE_BYTES].decode(resp.encoding or "utf-8", errors="replace"))
        out: dict = {"status": resp.status_code, "ok": resp.status_code < 400, "url": scrub(resp.url),
                     "content_type": kind}
        if method == "HEAD" or not text.strip():
            return out
        if "json" in kind or text.lstrip()[:1] in ("{", "["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if parsed is not None and len(text) <= MAX_TEXT:
                out["json"] = parsed
                return out
        elif "html" in kind:
            out["title"], text = page_text(text)
        elif kind and not any(k in kind for k in ("text", "xml", "csv", "javascript", "yaml")):
            out["note"] = "the API sent a file, not data; use download_file to save it"
            return out
        out["text"], out["more"] = text[:MAX_TEXT], len(text) > MAX_TEXT
        return out

    # ---- the web ------------------------------------------------------------------------------
    def search(self, query: str, limit: int = 8) -> dict:
        """DuckDuckGo first; when it answers with a robot check (or nothing), Bing; when Bing sends only junk,
        the AI search (Groq's, from its own servers); Wikipedia last."""
        first_error = None
        blocked = self.clock() < self._ddg_rest_until
        try:
            if blocked:
                results, engine = [], "duckduckgo"
            else:
                results, engine = self._duckduckgo(query, limit), "duckduckgo"
        except NotFound as exc:
            results, first_error = [], exc
            blocked = self.clock() < self._ddg_rest_until
        keywords = _keywords(query)
        junk = None  # Bing answered, but with nothing about the question
        for q in dict.fromkeys((query, keywords)):  # plain questions first, then just the key words
            if results or not q or self.clock() < self._bing_rest_until:
                break
            try:
                found = self._bing(q, limit)
            except NotFound:
                found = []
            results, engine = [r for r in found if _relevant(query, r)], "bing"
            junk = bool(found) and not results if junk in (None, True) else False
        if junk:  # Bing does this to a suspected robot (results for the first word only); give it a rest
            self._bing_rest_until = self.clock() + ROBOT_REST_S
        if not results and self.ai_search is not None:
            try:
                found = self.ai_search(query, limit)
            except Exception:
                log.exception("the AI web search failed")
                found = []
            results = [r for r in found if str(r.get("url", "")).startswith(("http://", "https://"))][:limit]
            if results:
                engine = "groq"
        if not results:
            try:
                results = [r for r in self._wikipedia(keywords or query, limit) if _relevant(query, r)]
                engine = "wikipedia"
            except NotFound as exc:
                if first_error is not None:  # nothing answered: most likely no internet
                    raise first_error from exc
                results = []
        out = {"query": query, "engine": engine, "results": results}
        if blocked and not results:
            out["blocked"] = True  # the engines are limiting searches, not a sign the subject has nothing
        return out

    def _wikipedia(self, query: str, limit: int) -> list[dict]:
        resp = self._get("https://en.wikipedia.org/w/api.php",
                         params={"action": "query", "list": "search", "srsearch": query, "format": "json",
                                 "srlimit": min(limit, 10), "utf8": 1})
        try:
            hits = resp.json()["query"]["search"]
        except (ValueError, KeyError, TypeError):
            return []
        return [{"title": h["title"], "url": "https://en.wikipedia.org/wiki/" + quote(h["title"].replace(" ", "_")),
                 "snippet": page_text(h.get("snippet", ""))[1]} for h in hits[:limit]]

    def _duckduckgo(self, query: str, limit: int) -> list[dict]:
        resp = self._get("https://html.duckduckgo.com/html/", params={"q": query})
        if resp.status_code == 202 or ("result__a" not in resp.text and "anomaly" in resp.text):
            self._ddg_rest_until = self.clock() + ROBOT_REST_S
            raise NotFound("DuckDuckGo asked for a robot check")
        results = []
        for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)'
                             r'(?:class="result__snippet"[^>]*>(.*?)</a>|(?=<a[^>]+class="result__a"))', resp.text, re.S):
            href = html.unescape(m.group(1))
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
            if "duckduckgo.com/y.js" in href:  # ads
                continue
            results.append({"title": page_text(m.group(2))[1], "url": href, "snippet": page_text(m.group(4) or "")[1]})
            if len(results) >= limit:
                break
        return results

    def _bing(self, query: str, limit: int) -> list[dict]:
        resp = self._get("https://www.bing.com/search", params={"q": query, "setlang": "en", "cc": "US", "adlt": "strict"},
                         headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.8"})
        results = []
        for block in re.findall(r'<li class="b_algo".*?</li>', resp.text, re.S):
            a = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not a:
                continue
            href = _bing_target(html.unescape(a.group(1)))
            if not href.startswith(("http://", "https://")):
                continue
            snip = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
            results.append({"title": page_text(a.group(2))[1], "url": href,
                            "snippet": page_text(snip.group(1))[1] if snip else ""})
            if len(results) >= limit:
                break
        return results

    def read_page(self, url: str, max_chars: int = MAX_TEXT) -> dict:
        resp = self._get(url, stream=True)
        data = resp.raw.read(MAX_PAGE_BYTES + 1, decode_content=True) if hasattr(resp, "raw") and resp.raw else resp.content
        kind = resp.headers.get("Content-Type", "")
        text = data[:MAX_PAGE_BYTES].decode(resp.encoding or "utf-8", errors="replace")
        title = ""
        if "html" in kind or text.lstrip().lower().startswith(("<!doctype html", "<html")):
            title, text = page_text(text)
        elif not ("text" in kind or "json" in kind or "xml" in kind):
            return {"url": resp.url, "type": kind, "note": "this isn't a web page; use download_file to save it"}
        return {"url": resp.url, "title": title, "text": text[:max_chars], "more": len(text) > max_chars}

    def scrape(self, url: str, max_chars: int = MAX_TEXT) -> dict:
        """Read a public page and return its structure: title, links, headings, tables, list items and text."""
        resp = self._get(url, stream=True)
        data = resp.raw.read(MAX_PAGE_BYTES + 1, decode_content=True) if hasattr(resp, "raw") and resp.raw else resp.content
        kind = resp.headers.get("Content-Type", "")
        markup = data[:MAX_PAGE_BYTES].decode(resp.encoding or "utf-8", errors="replace")
        if not ("html" in kind or markup.lstrip().lower().startswith(("<!doctype html", "<html"))):
            return {"url": resp.url, "type": kind, "note": "this isn't a web page; use download_file to save it"}
        p = _Scrape()
        p.feed(markup)
        _, text = page_text(markup)
        links = []
        for link in p.links[:100]:
            u = urljoin(resp.url, link["url"])
            links.append({"text": link["text"], "url": u})
        return {"url": resp.url, "title": p.title.strip(), "headings": p.headings[:50],
                "links": links, "tables": p.tables[:10], "list_items": p.lists[:100],
                "text": text[:max_chars]}

    def _page(self, url: str, max_chars: int) -> dict:
        """read_page that reports a source it couldn't read instead of failing the whole search."""
        try:
            out = self.read_page(url, max_chars)
        except (NotFound, ValueError, requests.RequestException) as exc:
            return {"url": url, "error": str(exc)}
        if "note" in out:
            return {"url": url, "error": out["note"]}
        if len(out.get("text", "").strip()) < THIN_PAGE:
            return {"url": out["url"], "error": "almost no text (the page may need JavaScript)"}
        return out

    def _read_many(self, urls: list[str], want: int, max_chars: int) -> tuple[list[dict], list[dict], list[str]]:
        """Read pages in parallel, in order, until `want` of them gave real text. Returns (read, skipped, unread)."""
        read, skipped, queue = [], [], list(urls)
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="web-read") as pool:
            while queue and len(read) < want:
                batch, queue = queue[:want - len(read)], queue[want - len(read):]
                for page in pool.map(lambda u: self._page(u, max_chars), batch):
                    if page.get("error"):
                        skipped.append({"url": page["url"], "why": page["error"]})
                    else:
                        read.append(page)
        return read, skipped, queue

    def research(self, question: str, pages: int = 4, max_chars: int = RESEARCH_CHARS) -> dict:
        """Search the web and read the best few results, so Jarvis can answer from real pages with sources."""
        want = max(1, min(int(pages or 4), 8))
        results, seen = [], set()
        found = self.search(question, limit=12)
        for r in found["results"]:
            u = r["url"].split("#")[0]
            if u.startswith(("http://", "https://")) and u not in seen:
                seen.add(u)
                results.append(r)
        by_url = {r["url"].split("#")[0]: r for r in results}
        read, skipped, unread = self._read_many(list(by_url), want, max_chars)
        sources = [{"title": p.get("title") or by_url.get(p["url"], {}).get("title", ""), "url": p["url"],
                    "text": p["text"]} for p in read]
        out = {"question": question, "sources": sources, "skipped": skipped[:8],
               "other_results": [by_url[u] for u in unread[:6]]}
        if not sources and found.get("blocked"):
            out["blocked"] = True
            out["note"] = "the search engines are limiting searches right now; try again in a few minutes"
        elif not sources:
            out["note"] = ("no page could be read; try other words, or use the snippets in other_results"
                           if results else "the search found nothing; try other words")
        return out

    def explore_site(self, url: str, look_for: str = "", pages: int = 5, max_chars: int = RESEARCH_CHARS) -> dict:
        """Open a page, then follow its links on the same site (the ones matching look_for first)."""
        want = max(1, min(int(pages or 5), 10))
        start = self.scrape(url, max_chars)
        if "note" in start:
            return start
        host = urlparse(start["url"]).hostname
        words = set(re.findall(r"\w{3,}", (look_for or "").lower()))
        seen, links = {start["url"].split("#")[0]}, []
        for link in start["links"]:
            u = link["url"].split("#")[0]
            if urlparse(u).hostname == host and u.startswith(("http://", "https://")) and u not in seen:
                seen.add(u)
                hay = (link["text"] + " " + u).lower()
                links.append((-sum(w in hay for w in words), len(links), link["text"], u))
        links.sort()
        read, skipped, unread = self._read_many([u for *_, u in links], want - 1, max_chars)
        first = {"title": start["title"], "url": start["url"], "text": start["text"], "headings": start["headings"]}
        names = {u: t for _, _, t, u in links}
        return {"site": host, "pages": [first] + [{"title": p.get("title", ""), "url": p["url"], "text": p["text"]}
                                                  for p in read],
                "skipped": skipped[:8], "more_links": [{"text": names[u], "url": u} for u in unread[:20]]}

    # ---- Hugging Face ---------------------------------------------------------------------------------
    def hf_search(self, query: str, kind: str = "models", limit: int = 10) -> dict:
        kind = kind if kind in ("models", "datasets", "spaces") else "models"
        resp = self._get(f"https://huggingface.co/api/{kind}",
                         params={"search": query, "limit": limit, "sort": "downloads" if kind != "spaces" else "likes"})
        return {"query": query, "kind": kind,
                "results": [{"id": r.get("id"), "downloads": r.get("downloads"), "likes": r.get("likes"),
                             "task": r.get("pipeline_tag"), "url": f"https://huggingface.co/{r.get('id')}"}
                            for r in resp.json()]}

    def hf_model(self, repo: str) -> dict:
        repo = _repo(repo, "huggingface.co")
        info = self._get(f"https://huggingface.co/api/models/{repo}", params={"blobs": "true"}).json()
        files = [{"name": s["rfilename"], "size": s.get("size")} for s in info.get("siblings", [])]
        card = info.get("cardData") or {}
        readme = ""
        try:
            readme = self._get(f"https://huggingface.co/{repo}/raw/main/README.md").text[:6000]
        except NotFound:
            pass
        return {"id": info.get("id", repo), "task": info.get("pipeline_tag"), "license": card.get("license"),
                "downloads": info.get("downloads"), "likes": info.get("likes"), "gated": info.get("gated"),
                "tags": info.get("tags", [])[:25], "files": files[:80], "readme": readme,
                "url": f"https://huggingface.co/{repo}"}

    # ---- GitHub ------------------------------------------------------------------------------------------
    def github_search(self, query: str, limit: int = 10) -> dict:
        resp = self._get("https://api.github.com/search/repositories",
                         params={"q": query, "per_page": limit, "sort": "stars"},
                         headers={"Accept": "application/vnd.github+json"})
        return {"query": query, "results": [
            {"repo": r["full_name"], "description": r.get("description"), "stars": r.get("stargazers_count"),
             "language": r.get("language"), "updated": (r.get("pushed_at") or "")[:10], "url": r["html_url"]}
            for r in resp.json().get("items", [])]}

    def github_read(self, repo: str, path: str = "", ref: str = "") -> dict:
        repo = _repo(repo, "github.com")
        url = f"https://api.github.com/repos/{repo}/contents/{quote(path.strip('/'))}"
        data = self._get(url, params={"ref": ref} if ref else None,
                         headers={"Accept": "application/vnd.github+json"}).json()
        if isinstance(data, list):
            return {"repo": repo, "folder": path or "/", "entries": [
                {"name": e["name"], "folder": e["type"] == "dir", "size": e.get("size")} for e in data]}
        if data.get("encoding") == "base64":
            raw = base64.b64decode(data.get("content", ""))
            if b"\x00" in raw[:4096]:
                return {"repo": repo, "file": path, "binary": True, "size": data.get("size")}
            text = raw.decode("utf-8", errors="replace")
            return {"repo": repo, "file": path, "text": text[:MAX_TEXT], "more": len(text) > MAX_TEXT}
        return {"repo": repo, "file": path, "note": "too big to show here; use download_file with its raw URL",
                "download_url": data.get("download_url")}

    # ---- downloads (always after the author's yes) ------------------------------------------------------------
    def download(self, url: str, folder: Path, name: str = "", progress: Callable[[float, str], None] = lambda f, m: None,
                 cancel: Callable[[], bool] = lambda: False) -> dict:
        url = hf_url(url)
        folder.mkdir(parents=True, exist_ok=True)
        resp = self._get(url, stream=True)
        total = int(resp.headers.get("Content-Length") or 0)
        if total and total > shutil.disk_usage(folder).free - KEEP_FREE_BYTES:
            raise NotFound(f"not enough free space for {total / 1e9:.1f} GB")
        fname = _safe_name(name or _filename(resp, url))
        dest = folder / fname
        if dest.exists():
            raise ValueError(f"{dest} already exists")
        part = dest.with_name(dest.name + ".part")
        sha, got = hashlib.sha256(), 0
        try:
            with open(part, "wb") as f:
                for chunk in resp.iter_content(1 << 20):
                    if cancel():
                        raise NotFound("download cancelled")
                    f.write(chunk)
                    sha.update(chunk)
                    got += len(chunk)
                    if total:
                        progress(min(0.99, got / total), f"{got / 1e6:.0f} of {total / 1e6:.0f} MB")
            os.replace(part, dest)
        finally:
            if part.exists():
                part.unlink()
        progress(1.0, "done")
        return {"saved": str(dest), "bytes": got, "sha256": sha.hexdigest()}

    def github_download(self, repo: str, folder: Path, ref: str = "", **kw) -> dict:
        repo = _repo(repo, "github.com")
        url = f"https://codeload.github.com/{repo}/zip/{quote(ref) if ref else 'HEAD'}"
        name = repo.replace("/", "-") + ".zip"
        out = self.download(url, folder, name, **kw)
        zpath = Path(out["saved"])
        target = folder / repo.split("/")[1]
        if target.exists():
            raise ValueError(f"{target} already exists")
        with zipfile.ZipFile(zpath) as z:
            for member in z.namelist():  # no paths that climb out of the folder
                if member.startswith("/") or ".." in Path(member).parts:
                    raise ValueError("that zip has unsafe paths")
            z.extractall(folder / (target.name + ".unzip"))
        inner = next((folder / (target.name + ".unzip")).iterdir())
        os.replace(inner, target)
        shutil.rmtree(folder / (target.name + ".unzip"), ignore_errors=True)
        zpath.unlink()
        return {"repo": repo, "folder": str(target), "bytes": out["bytes"]}


def hf_url(url: str) -> str:
    """'hf://owner/repo/file' -> the file's download address on Hugging Face."""
    if url.startswith("hf://"):
        parts = url[5:].split("/", 2)
        if len(parts) < 3:
            raise ValueError("use hf://owner/repo/path/to/file")
        return f"https://huggingface.co/{parts[0]}/{parts[1]}/resolve/main/{quote(parts[2])}"
    return url


def _repo(repo: str, host: str) -> str:
    r = repo.strip().removesuffix(".git")
    if host in r:
        r = urlparse(r if "://" in r else "https://" + r).path
    r = r.strip("/")
    parts = r.split("/")
    if len(parts) < 2 or not all(re.fullmatch(r"[\w.-]+", p) for p in parts[:2]):
        raise ValueError(f"'{repo}' isn't an owner/name repository")
    return "/".join(parts[:2])


def _filename(resp, url: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
    return unquote(m.group(1)) if m else (unquote(Path(urlparse(url).path).name) or "download")


def _safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "download"
