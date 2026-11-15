"""The internet for Jarvis: web search and pages, Hugging Face and GitHub (read-only), and downloads."""
from __future__ import annotations

import base64
import hashlib
import html
import ipaddress
import os
import re
import shutil
import socket
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from typing import Callable, Optional
from urllib.parse import urljoin, parse_qs, quote, unquote, urlparse

import requests

from jarvis_books.errors import NotFound

UA = "JarvisBookWriter/1.0 (+https://github.com/hawpetossjustin25-collab/jarvis-book-writer)"
MAX_PAGE_BYTES = 3_000_000
MAX_TEXT = 15_000
KEEP_FREE_BYTES = 2 * 1024 ** 3


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
        self._cell: list[str] = []

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
        elif tag in ("td", "th") and self._cell is not None:
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


class Web:
    def __init__(self, session: Optional[requests.Session] = None, check_address: Callable[[str], str] = _public,
                 timeout_s: float = 20.0):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", UA)
        self.check = check_address
        self.timeout_s = timeout_s

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

    # ---- the web ------------------------------------------------------------------------------
    def search(self, query: str, limit: int = 8) -> dict:
        resp = self._get("https://html.duckduckgo.com/html/", params={"q": query})
        results = []
        for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)'
                             r'(?:class="result__snippet"[^>]*>(.*?)</a>|(?=<a[^>]+class="result__a"))', resp.text, re.S):
            href = html.unescape(m.group(1))
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
            if "duckduckgo.com/y.js" in href:  # ads
                continue
            title = page_text(m.group(2))[1]
            snippet = page_text(m.group(4) or "")[1]
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= limit:
                break
        return {"query": query, "results": results}

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
