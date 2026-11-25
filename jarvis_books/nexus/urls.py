"""URL normalization and the network guard: one spelling per page, and only public internet hosts."""
from __future__ import annotations

import asyncio
import ipaddress
import posixpath
import re
import socket
import time
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

_TRACKING = re.compile(r"^(utm_[a-z_]+|fbclid|gclid|dclid|gbraid|wbraid|msclkid|yclid|mc_cid|mc_eid|igshid|_hsenc|"
                       r"_hsmi|mkt_tok|ref_src|spm|oly_anon_id|oly_enc_id|vero_id|_ga|_gl)$", re.I)
_BINARY = re.compile(r"\.(?:jpe?g|png|gif|webp|bmp|ico|svg|mp3|mp4|m4a|wav|ogg|avi|mov|mkv|webm|zip|gz|tgz|rar|7z|"
                     r"exe|msi|dmg|iso|apk|bin|woff2?|ttf|otf|eot|css|js)$", re.I)
_TWO_PART = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.nz", "co.jp", "com.br",
             "com.mx", "co.in", "co.za", "com.cn", "com.tw", "com.sg", "com.tr", "co.kr"}
_PATH_SAFE = "/:@!$&'()*+,;=-._~%"


class UnsafeURL(ValueError):
    """The address is not a public http(s) web page (a file, this computer, the home network...)."""


def normalize(url: str, base: Optional[str] = None) -> Optional[str]:
    """The canonical form of `url` (resolved against `base`), or None when it isn't an http(s) address.

    Lowercases the scheme and host, drops default ports, credentials, fragments and tracking parameters,
    resolves dot segments, sorts the query and normalizes percent escapes, so the same page is stored once.
    """
    url = (url or "").strip()
    if not url or url.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    if base:
        url = urljoin(base, url)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if not host or len(host) > 253:
        return None
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None or (scheme, port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
    path = parts.path or "/"
    trailing = path.endswith("/")
    path = posixpath.normpath(path) if path not in ("", "/") else "/"
    path = "/" + path.lstrip("/") if path != "." else "/"
    if trailing and not path.endswith("/"):
        path += "/"
    path = re.sub(r"%[0-9a-fA-F]{2}", _escape, quote(path, safe=_PATH_SAFE))
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query), doseq=True), ""))


def _escape(match: re.Match) -> str:
    char = chr(int(match.group(0)[1:], 16))
    return char if char.isascii() and (char.isalnum() or char in "-._~") else match.group(0).upper()


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def site_of(host_or_url: str) -> str:
    """The registrable site: 'docs.python.org' -> 'python.org', 'www.bbc.co.uk' -> 'bbc.co.uk'."""
    host = host_of(host_or_url) if "/" in host_or_url else host_or_url.lower().strip(".")
    try:
        ipaddress.ip_address(host.strip("[]"))
        return host
    except ValueError:
        pass
    labels = host.split(".")
    keep = 3 if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_PART else 2
    return ".".join(labels[-keep:])


def in_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def looks_binary(url: str) -> bool:
    return bool(_BINARY.search(urlsplit(url).path))


def _blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
            or ip.is_unspecified or not ip.is_global)


Resolver = Callable[[str], Awaitable[list[str]]]


async def system_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0].split("%")[0] for info in infos]


class NetworkGuard:
    """Lets requests reach public internet addresses only (no SSRF into this computer or the home network)."""

    def __init__(self, resolver: Resolver = system_resolver, ttl_s: float = 300.0, clock=time.monotonic):
        self.resolver, self.ttl_s, self.clock = resolver, ttl_s, clock
        self._cache: dict[str, tuple[float, Optional[str]]] = {}

    async def check(self, url: str) -> None:
        """Raises UnsafeURL unless `url` is http(s) on a host whose every address is public."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise UnsafeURL("only http and https web addresses can be fetched")
        if parts.username or parts.password:
            raise UnsafeURL("addresses with a user name or password are not fetched")
        host = parts.hostname.lower()
        hit = self._cache.get(host)
        if hit and self.clock() - hit[0] < self.ttl_s:
            if hit[1]:
                raise UnsafeURL(hit[1])
            return
        problem = await self._problem(host)
        self._cache[host] = (self.clock(), problem)
        if problem:
            raise UnsafeURL(problem)

    async def _problem(self, host: str) -> Optional[str]:
        if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
            return f"{host} is on this computer or the home network, not the internet"
        try:
            addresses = [ipaddress.ip_address(host.strip("[]"))]
        except ValueError:
            try:
                addresses = [ipaddress.ip_address(a) for a in await self.resolver(host)]
            except (OSError, ValueError) as exc:
                return f"couldn't find {host} ({type(exc).__name__})"
        if not addresses:
            return f"couldn't find {host}"
        if any(_blocked_ip(a) for a in addresses):
            return f"{host} is on this computer or the home network, not the internet"
        return None
