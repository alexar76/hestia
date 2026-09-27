"""Hub announce. Observation only — never a trust grant."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from hestia.ssrf import assert_public_https


class AnnounceError(RuntimeError):
    pass


def _loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def announce(*, hub_url: str, hearth_url: str, timeout: float = 8.0) -> dict:
    try:
        assert_public_https(hub_url, allow_http_loopback=_loopback(hub_url))
        assert_public_https(hearth_url, allow_http_loopback=_loopback(hearth_url))
    except ValueError as exc:
        raise AnnounceError(str(exc)) from exc
    try:
        response = httpx.post(
            f"{hub_url.rstrip('/')}/ai-market/v2/federation/announce",
            json={"hub_url": hearth_url, "hub_name": "HESTIA"},
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise AnnounceError("Hub announce failed") from exc
