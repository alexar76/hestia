"""Fail-closed URL checks. Deploy payloads never become an open proxy."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED_HOSTS = frozenset({"localhost", "localhost.localdomain", "metadata.google.internal"})


def assert_public_https(url: str, *, allow_http_loopback: bool = False) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("url scheme must be https (or http on loopback in tests)")
    if parsed.username or parsed.password:
        raise ValueError("url must not contain userinfo")
    host = (parsed.hostname or "").strip(".").lower()
    if not host:
        raise ValueError("url host is required")
    if host in _BLOCKED_HOSTS and not allow_http_loopback:
        raise ValueError("url host is not allowed")
    if parsed.scheme == "http" and not allow_http_loopback:
        raise ValueError("http is only allowed for local tests")
    _assert_host_not_private(host, allow_loopback=allow_http_loopback)
    return url


def _assert_host_not_private(host: str, *, allow_loopback: bool) -> None:
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise ValueError(f"url host does not resolve: {host}") from exc
        candidates = [item[4][0] for item in infos]
        if not candidates:
            raise ValueError(f"url host does not resolve: {host}")
    for raw in candidates:
        addr = ipaddress.ip_address(raw)
        if allow_loopback and addr.is_loopback:
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            raise ValueError("url host resolves to a non-public address")
