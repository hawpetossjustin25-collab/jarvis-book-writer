"""Page parsing: title, description, readable text, language, publication date, author, structured metadata,
robots meta directives and every hyperlink."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import warnings
import xml.etree.ElementTree as ET
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
    published: Optional[float] = None  # Unix time the page says it was published, when it says
    author: str = ""
    meta: dict = field(default_factory=dict)  # structured metadata: schema type, site name, keywords, modified


_EARLIEST = datetime(1991, 1, 1, tzinfo=timezone.utc).timestamp()  # the first web page


def parse_date(value: str, now: Optional[float] = None) -> Optional[float]:
    """An ISO 8601, plain YYYY-MM-DD or e-mail style (RSS) date as Unix time. None when it isn't a date or it is
    before the web existed or more than a day in the future (a wrong clock or a placeholder)."""
    value = (value or "").strip()
    if not value:
        return None
    dt = None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
        if m:
            try:
                dt = datetime(int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                dt = None
        else:
            try:
                dt = parsedate_to_datetime(value)
            except (TypeError, ValueError, IndexError):
                dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = dt.timestamp()
    limit = (now if now is not None else time.time()) + 86400
    return ts if _EARLIEST <= ts <= limit else None


_DATE_META = ("article:published_time", "og:published_time", "datepublished", "date", "dc.date", "dc.date.issued",
              "dcterms.created", "pubdate", "publish-date", "publish_date", "citation_publication_date", "sailthru.date")
_MODIFIED_META = ("article:modified_time", "og:updated_time", "dcterms.modified", "last-modified")
_AUTHOR_META = ("author", "article:author", "dc.creator", "citation_author", "byl", "parsely-author", "sailthru.author")


def _ld_items(soup) -> list[dict]:
    """Every JSON-LD object on the page (including @graph members)."""
    out: list[dict] = []
    for tag in soup.find_all("script", type=lambda t: t and t.lower().strip() == "application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack and len(out) < 50:
            item = stack.pop(0)
            if isinstance(item, dict):
                out.append(item)
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
    return out


def _name(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    if isinstance(value, list):
        return ", ".join(n for n in (_name(v) for v in value[:5]) if n)
    return ""


def _types(item: dict) -> list[str]:
    t = item.get("@type")
    return [str(x) for x in t] if isinstance(t, list) else ([str(t)] if t else [])


def _language(value: str) -> str:
    """'en_US', 'en-US' and 'EN' all become 'en-us' / 'en'."""
    return re.sub(r"[^a-z0-9-]", "", (value or "").strip().replace("_", "-").lower().split(",")[0])[:20]


def _metadata(soup, meta: dict, page: ParsedPage) -> None:
    items = _ld_items(soup)
    main = next((i for i in items if i.get("datePublished") or i.get("author")), items[0] if items else {})
    published = parse_date(str(main.get("datePublished") or ""))
    if published is None:
        published = next((d for d in (parse_date(meta.get(k, "")) for k in _DATE_META) if d), None)
    if published is None:
        node = soup.find("article") or soup
        tag = node.find("time", datetime=True)
        published = parse_date(str(tag["datetime"])) if tag else None
    page.published = published
    author = _name(main.get("author"))
    if not author:
        author = next((meta[k] for k in _AUTHOR_META if meta.get(k) and not meta[k].startswith("http")), "")
    if not author:
        tag = soup.find(attrs={"rel": "author"}) or soup.find(class_=re.compile(r"\b(author|byline)\b"))
        author = tag.get_text(" ", strip=True) if tag else ""
        author = author if len(author) <= 120 else ""
    page.author = _SPACE.sub(" ", re.sub(r"^\s*by\s+", "", author, flags=re.I)).strip()[:200]
    if not page.lang:
        page.lang = _language(meta.get("content-language") or meta.get("og:locale") or str(main.get("inLanguage") or ""))
    info: dict = {}
    types = _types(main)
    if types:
        info["type"] = types[0][:60]
    modified = parse_date(str(main.get("dateModified") or "")) or next(
        (d for d in (parse_date(meta.get(k, "")) for k in _MODIFIED_META) if d), None)
    if modified:
        info["modified"] = modified
    site_name = meta.get("og:site_name") or _name(main.get("publisher"))
    if site_name:
        info["site_name"] = site_name[:120]
    keywords = meta.get("keywords") or main.get("keywords") or ""
    if isinstance(keywords, list):
        keywords = ",".join(str(k) for k in keywords)
    words = [k.strip() for k in str(keywords).split(",") if k.strip()][:20]
    if words:
        info["keywords"] = words
    if meta.get("article:section"):
        info["section"] = meta["article:section"][:80]
    page.meta = info


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
        page.lang = _language(str(html["lang"]))
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
    _metadata(soup, meta, page)
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


_ATOM = "{http://www.w3.org/2005/Atom}"


def _plain(markup: str) -> str:
    if "<" not in markup:
        return _SPACE.sub(" ", markup).strip()
    return _SPACE.sub(" ", BeautifulSoup(markup, "html.parser").get_text(" ", strip=True)).strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(body: bytes | str, base_url: str, max_text: int = 200_000, max_links: int = 300) -> Optional[ParsedPage]:
    """An RSS or Atom feed: its title, the entries' titles and summaries as text, and each entry's link.
    None when the body isn't a feed, so the caller can treat it as plain text."""
    raw = body if isinstance(body, bytes) else body.encode("utf-8", "replace")
    if b"<!DOCTYPE" in raw[:2048].upper() or b"<!ENTITY" in raw.upper():
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    kind = _local(root.tag)
    if kind == "rss":
        channel = root.find("channel")
        if channel is None:
            return None
        head, entries = channel, channel.findall("item")
    elif kind == "rdf":
        head = next((c for c in root if _local(c.tag) == "channel"), root)
        entries = [c for c in root if _local(c.tag) == "item"]
    elif root.tag == _ATOM + "feed":
        head, entries = root, root.findall(_ATOM + "entry")
    else:
        return None

    def child(node: ET.Element, *names: str) -> str:
        for c in node:
            if _local(c.tag) in names:
                return "".join(c.itertext()).strip()
        return ""

    def link_of(node: ET.Element) -> str:
        for c in node:
            if _local(c.tag) != "link":
                continue
            href = c.get("href")
            if href and c.get("rel", "alternate") == "alternate":
                return href
            if not href and (c.text or "").strip():
                return c.text.strip()
        return ""

    page = ParsedPage(resource_type="feed")
    page.title = _plain(child(head, "title"))[:500]
    page.description = _plain(child(head, "description", "subtitle"))[:1000]
    page.lang = _language(child(head, "language"))
    page.author = _plain(child(head, "managingeditor", "author", "creator"))[:200]
    parts, seen, dates = [], set(), []
    for entry in entries:
        when = parse_date(child(entry, "pubdate", "published", "updated", "date"))
        if when:
            dates.append(when)
        title = _plain(child(entry, "title"))
        summary = _plain(child(entry, "description", "summary", "content"))
        parts.append("\n".join(x for x in (title, summary) if x))
        url = normalize(link_of(entry), base_url)
        if url and url not in seen and len(page.links) < max_links:
            seen.add(url)
            page.links.append(Link(url, title[:300], "link"))
    page.text = _clean("\n\n".join(p for p in parts if p))[:max_text]
    page.published = max(dates) if dates else parse_date(child(head, "pubdate", "lastbuilddate", "updated"))
    return page
