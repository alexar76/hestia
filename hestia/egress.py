"""Tenant egress: the only way a sandboxed agent reaches the network, and only where it said.

The WebAssembly runner has no sockets and its container has no network, so a handler cannot
open a connection — and must not get one. Agents that sell data they read elsewhere (a chain
RPC, a public weather API) still need it. They get it through the hearth, on terms decided at
deploy time:

- the deploy names the hosts (`egress_hosts`, at most MAX_HOSTS exact DNS names). They are
  public: the roster shows them, so a buyer sees where an agent's answers come from;
- the handler calls `fetch(url, …)`. Inside the sandbox that only records the request and
  ends the run; the hearth makes the request and runs the handler again with the response.
  Each request is one round; a call has at most MAX_ROUNDS;
- the hearth fetches HTTPS on port 443 only, from a host on the list, after resolving it and
  refusing any non-public address, and connects to the address it checked (no DNS rebinding):
  the TLS name stays the host, so the certificate is verified for it. No redirects are
  followed, no proxy from the environment is used, cookies are not kept, and responses are cut
  at MAX_RESPONSE_BYTES;
- a tenant has a budget of requests per minute across all its calls, and every request is
  logged with the tenant, host, status and size — never the body.

Nothing a handler returns can add a host, change the method set or reach a private address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger("hestia.egress")

MAX_HOSTS = 8
MAX_ROUNDS = 6
MAX_REQUEST_BODY = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
REQUEST_TIMEOUT_S = 5.0
DEFAULT_PER_MINUTE = 60
# All agents together. A key admits itself for free, so a per-agent budget alone does not bound
# what the hearth's address sends: ten keys are ten agents.
DEFAULT_HEARTH_PER_MINUTE = 600
METHODS = frozenset({"GET", "POST"})
# Request headers a handler may set. Everything else (Host, Cookie, Connection, Proxy-*,
# X-Forwarded-*) is dropped; the hearth sets Host and User-Agent itself.
REQUEST_HEADERS = frozenset({"accept", "accept-language", "authorization", "content-type",
                             "x-api-key"})
# Response headers a handler sees.
RESPONSE_HEADERS = frozenset({"content-type", "content-length", "date", "etag", "last-modified",
                              "location", "retry-after", "cache-control", "x-ratelimit-remaining",
                              "x-ratelimit-reset", "payment-required"})
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet",
                     ".arpa", ".onion", ".invalid", ".test", ".example")
_BLOCKED_NAMES = frozenset({"localhost", "metadata.google.internal", "metadata"})


class EgressRefused(ValueError):
    """A request the hearth will not make. The message goes back to the handler."""


def normalize_host(raw: str) -> str:
    """One deploy-time host: an exact public DNS name, lower-case, no port, no IP literal."""
    host = (raw or "").strip().lower().rstrip(".")
    if not host or len(host) > 253:
        raise ValueError(f"egress host {raw!r} is empty or too long")
    if "*" in host:
        raise ValueError(f"egress host {raw!r}: wildcards are not allowed, name each host")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise ValueError(f"egress host {raw!r}: an IP address is not allowed, use its DNS name")
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise ValueError(f"egress host {raw!r} is not a DNS name")
    if host in _BLOCKED_NAMES or host.endswith(_BLOCKED_SUFFIXES) or labels[-1].isdigit():
        raise ValueError(f"egress host {raw!r} is not a public name")
    return host


def normalize_hosts(raw: list[str] | tuple[str, ...]) -> list[str]:
    hosts: list[str] = []
    for item in raw or ():
        host = normalize_host(item)
        if host not in hosts:
            hosts.append(host)
    if len(hosts) > MAX_HOSTS:
        raise ValueError(f"at most {MAX_HOSTS} egress hosts")
    return hosts


_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")  # RFC 8215 local-use NAT64


def _public(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Global unicast, judged by the IPv4 address an IPv6 one carries (IPv4-mapped, 6to4, NAT64
    64:ff9b::/96 — which Python calls global — via hestia.domains, the check the 2026-10-25
    audit settled on), and never the local-use NAT64 prefix."""
    from hestia.domains import _public as public_unicast

    if isinstance(addr, ipaddress.IPv6Address) and addr in _NAT64_LOCAL:
        return False
    return public_unicast(addr)


def _system_resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return [item[4][0] for item in infos]


@dataclass(frozen=True)
class EgressRequest:
    """A request a handler asked for, checked against the tenant's hosts."""

    method: str
    url: str
    host: str
    headers: dict[str, str]
    body: str | None

    @classmethod
    def parse(cls, raw: Any, allowed: list[str]) -> "EgressRequest":
        if not isinstance(raw, dict):
            raise EgressRefused("fetch request must be an object")
        method = str(raw.get("method") or "GET").upper()
        if method not in METHODS:
            raise EgressRefused(f"fetch method {method} is not allowed (GET or POST)")
        url = raw.get("url")
        if not isinstance(url, str) or len(url) > 2048:
            raise EgressRefused("fetch url must be a string of at most 2048 characters")
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise EgressRefused("fetch url must be https")
        if parts.username or parts.password:
            raise EgressRefused("fetch url must not carry credentials")
        if parts.port not in (None, 443):
            raise EgressRefused("fetch url must use port 443")
        host = (parts.hostname or "").lower().rstrip(".")
        if host not in allowed:
            raise EgressRefused(f"{host or 'that host'} is not in this agent's egress hosts")
        headers_raw = raw.get("headers") or {}
        if not isinstance(headers_raw, dict):
            raise EgressRefused("fetch headers must be an object")
        headers = {}
        for name, value in headers_raw.items():
            key = str(name).lower()
            if key in REQUEST_HEADERS and isinstance(value, str) and len(value) <= 4096 \
                    and "\r" not in value and "\n" not in value:
                headers[key] = value
        body = raw.get("body")
        if body is not None:
            if not isinstance(body, str):
                raise EgressRefused("fetch body must be a string")
            if len(body.encode("utf-8")) > MAX_REQUEST_BODY:
                raise EgressRefused(f"fetch body is larger than {MAX_REQUEST_BODY} bytes")
            if method == "GET":
                raise EgressRefused("GET carries no body")
        clean = urlunsplit(("https", host, parts.path or "/", parts.query, ""))
        return cls(method=method, url=clean, host=host, headers=headers, body=body)

def request_key(raw: dict[str, Any]) -> str:
    """The key the guest stores a response under: the request exactly as the handler asked
    for it. Must match hestia.sandbox GUEST `_key` byte for byte."""
    import json

    headers = raw.get("headers") or {}
    pairs = sorted((str(k).lower(), str(v)) for k, v in headers.items()) if isinstance(headers, dict) else []
    return json.dumps([str(raw.get("method") or "GET").upper(), raw.get("url"), pairs,
                       raw.get("body")], ensure_ascii=False, separators=(",", ":"))


class RateLimit:
    """Requests per minute per tenant, across all its calls. Thread-safe, no event loop."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, int(per_minute))
        self._lock = threading.Lock()
        self._seen: dict[str, deque[float]] = {}

    def take(self, slug: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            window = self._seen.setdefault(slug, deque())
            while window and window[0] <= now - 60.0:
                window.popleft()
            if len(window) >= self.per_minute:
                return False
            window.append(now)
            return True


Fetcher = Callable[[EgressRequest, str], Awaitable[dict[str, Any]]]


class Egress:
    """Makes the requests handlers ask for. One per hearth."""

    def __init__(self, *, per_minute: int = DEFAULT_PER_MINUTE,
                 hearth_per_minute: int = DEFAULT_HEARTH_PER_MINUTE,
                 resolve: Callable[[str], list[str]] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 user_agent: str = "hestia-egress/1") -> None:
        self.limit = RateLimit(per_minute)
        self.hearth_limit = RateLimit(hearth_per_minute)
        self._resolve = resolve or _system_resolve
        self._transport = transport
        self.user_agent = user_agent

    async def _address(self, host: str) -> str:
        try:
            raw = await asyncio.to_thread(self._resolve, host)
        except OSError as exc:
            raise EgressRefused(f"{host} does not resolve") from exc
        addresses = []
        for item in raw:
            try:
                addresses.append(ipaddress.ip_address(item.split("%", 1)[0]))
            except ValueError:
                continue
        if not addresses:
            raise EgressRefused(f"{host} does not resolve")
        # Every address must be public: a name that resolves to one public and one private
        # address is the rebinding pattern, and is refused whole.
        if not all(_public(a) for a in addresses):
            raise EgressRefused(f"{host} resolves to a non-public address")
        return str(addresses[0])

    async def fetch(self, slug: str, request: EgressRequest) -> dict[str, Any]:
        """The response a handler sees: {status, headers, text} or {error}."""
        if not self.limit.take(slug):
            raise EgressRefused(f"this agent's egress budget ({self.limit.per_minute}/min) is spent")
        if not self.hearth_limit.take("*"):
            raise EgressRefused("this hearth's egress budget is spent; retry in a minute")
        address = await self._address(request.host)
        parts = urlsplit(request.url)
        literal = f"[{address}]" if ":" in address else address
        target = urlunsplit(("https", literal, parts.path, parts.query, ""))
        headers = {**request.headers, "host": request.host, "user-agent": self.user_agent,
                   "accept-encoding": "identity"}
        started = time.perf_counter()
        status, size = 0, 0
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=REQUEST_TIMEOUT_S,
                                         follow_redirects=False, trust_env=False) as client:
                async with client.stream(
                    request.method, target, headers=headers,
                    content=request.body.encode("utf-8") if request.body is not None else None,
                    # TLS to the checked address, with the certificate verified for the host.
                    extensions={"sni_hostname": request.host},
                ) as response:
                    status = response.status_code
                    chunks: list[bytes] = []
                    # A transport may hand back a response it already read (in-process ones do);
                    # a network one streams, and is cut as soon as it passes the cap.
                    stream = ([response.content] if response.is_stream_consumed
                              else response.aiter_raw())
                    if isinstance(stream, list):
                        for chunk in stream:
                            size += len(chunk)
                            chunks.append(chunk)
                    else:
                        async for chunk in stream:
                            size += len(chunk)
                            if size > MAX_RESPONSE_BYTES:
                                break
                            chunks.append(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise EgressRefused(f"response is larger than {MAX_RESPONSE_BYTES} bytes")
                    kept = {k.lower(): v for k, v in response.headers.items()
                            if k.lower() in RESPONSE_HEADERS}
        except httpx.HTTPError as exc:
            logger.info("egress %s %s %s failed: %s", slug, request.method, request.host,
                        type(exc).__name__)
            return {"status": 0, "headers": {}, "text": "",
                    "error": f"{type(exc).__name__}: could not reach {request.host}"}
        finally:
            logger.info("egress %s %s %s status=%s bytes=%s ms=%d", slug, request.method,
                        request.host, status, size, int((time.perf_counter() - started) * 1000))
        return {"status": status, "headers": kept,
                "text": b"".join(chunks).decode("utf-8", errors="replace")}
