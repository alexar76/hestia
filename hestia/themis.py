"""Optional THEMIS admission before a tenant is started."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from hestia.models import CapabilitySpec
from hestia.ssrf import assert_public_https


class AdmissionDenied(RuntimeError):
    pass


def admit(
    *,
    url: str,
    capability: CapabilitySpec,
    admit_review: bool,
    timeout: float = 8.0,
) -> dict[str, Any]:
    dossier = {
        "candidate": {
            "capability_id": capability.capability_id,
            "name": capability.name,
            "description": capability.description,
            "publisher_id": capability.publisher_id,
        },
        "permissions": {"network": "egress-allowlist", "filesystem": "tmpfs"},
        "evidence": [],
        "usage": {"price_per_call_usd": capability.price_per_call_usd},
        "policy": {"require_https": True},
    }
    host = (urlparse(url).hostname or "").lower()
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    try:
        assert_public_https(url, allow_http_loopback=loopback)
    except ValueError as exc:
        raise AdmissionDenied(str(exc)) from exc
    try:
        response = httpx.post(f"{url.rstrip('/')}/invoke", json=dossier, timeout=timeout)
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        raise AdmissionDenied("THEMIS is unreachable; deploy fails closed") from exc
    decision = str((body.get("result") or body).get("decision") or "").lower()
    if decision == "approve":
        return body
    if decision == "review" and admit_review:
        return body
    raise AdmissionDenied(f"THEMIS decision is {decision or 'missing'}")
