"""Page parsing: title, description, readable text, language, robots meta directives and every hyperlink."""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning, XMLParsedAsHTMLWarning

from jarvis_books.nexus.urls import normalize

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

_DROP = ("script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed")
_SPACE = re.compile(r"[ \t\r\f\v]+")
_LINES = re.compile(r"\n\s*\n+")


@dataclass(frozen=True)
class Link:
    url: str
    anchor: str
    rel: str  # link, nofollow, canonical, alternate, next, prev
    nofollow: bool = False


@dataclass
class ParsedPage:
    title: str = ""
    description: str = ""
    text: str = ""
    lang: str = ""
    resource_type: str = "page"
    canonical: Optional[str] = None
    noindex: bool = False
    nofollow: bool = False
    links: list[Link] = field(default_factory=list)


def resource_type(content_type: str, url: str = "") -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ("text/html", "application/xhtml+xml"):
        return "page"
    if ct == "application/pdf" or url.lower().endswith(".pdf"):
        return "document"
    if ct in ("application/json", "application/ld+json") or ct.endswith("+json"):
        return "data"
    if ct in ("application/rss+xml", "application/atom+xml") or ct.endswith(("/xml", "+xml")):
        return "feed" if "rss" in ct or "atom" in ct else "data"
    if ct.startswith("text/"):
        return "text"
    for kind in ("image", "audio", "video"):
        if ct.startswith(kind + "/"):
            return kind
    return "other"


def _clean(text: str) -> str:
    lines = [_SPACE.sub(" ", line).strip() for line in text.splitlines()]
    return _LINES.sub("\n\n", "\n".join(lines)).strip()


def _directives(value: str) -> set[str]:
    return {part.strip().lower() for part in (value or "").split(",") if part.strip()}


def parse_html(body: bytes | str, base_url: str, max_text: int = 200_000, max_links: int = 300,
               robots_header: str = "") -> ParsedPage:
    """Parses HTML (bytes are decoded using the page's own charset). Links come back normalized and deduplicated."""
    soup = BeautifulSoup(body, "html.parser")
    page = ParsedPage()
    base_tag = soup.find("base", href=True)
    base = normalize(base_tag["href"], base_url) if base_tag else None
    base = base or base_url
    html = soup.find("html")
    if html and html.get("lang"):
        page.lang = str(html["lang"]).strip()[:20].lower()
    meta = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("name") or tag.get("property") or tag.get("http-equiv") or "").strip().lower()
        if key and tag.get("content") is not None and key not in meta:
            meta[key] = str(tag["content"]).strip()
    directives = _directives(meta.get("robots", "")) | _directives(meta.get("jarvisnexus", "")) | \
        _directives(robots_header)
    page.noindex = bool(directives & {"noindex", "none"})
    page.nofollow = bool(directives & {"nofollow", "none"})
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    page.title = _SPACE.sub(" ", title or meta.get("og:title", "")).strip()[:500]
    page.description = _SPACE.sub(" ", meta.get("description") or meta.get("og:description", "")).strip()[:1000]
    if meta.get("og:type", "").lower() == "article" or soup.find("article"):
        page.resource_type = "article"
    seen: set[str] = set()
    for tag in soup.find_all("link", href=True):
        rels = {r.lower() for r in (tag.get("rel") or [])}
        url = normalize(str(tag["href"]), base)
        if not url:
            continue
        if "canonical" in rels and page.canonical is None:
            page.canonical = url
        for rel in ("next", "prev", "alternate"):
            if rel in rels and url not in seen and not tag.get("hreflang") and \
                    (rel != "alternate" or (tag.get("type") or "").endswith(("rss+xml", "atom+xml"))):
                seen.add(url)
                page.links.append(Link(url, tag.get("title") or "", rel))
    for tag in soup.find_all("a", href=True):
        if len(page.links) >= max_links:
            break
        url = normalize(str(tag["href"]), base)
        if not url or url in seen:
            continue
        seen.add(url)
        rels = {r.lower() for r in (tag.get("rel") or [])}
        nofollow = bool(rels & {"nofollow", "ugc", "sponsored"})
        anchor = _SPACE.sub(" ", tag.get_text(" ", strip=True) or tag.get("title") or tag.get("aria-label") or "")
        page.links.append(Link(url, anchor.strip()[:300], "nofollow" if nofollow else "link", nofollow))
    for tag in soup.find_all(_DROP):
        tag.decompose()
    root = soup.body or soup
    page.text = _clean(root.get_text("\n"))[:max_text]
    return page


def parse_text(body: str, max_text: int = 200_000) -> ParsedPage:
    text = _clean(body)[:max_text]
    first = next((line for line in text.splitlines() if line.strip()), "")
    return ParsedPage(title=first[:200], text=text, resource_type="text")
