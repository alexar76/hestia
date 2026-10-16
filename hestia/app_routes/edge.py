"""The tenant edge: /t/<slug>/<path> proxied to the tenant."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from hestia.names import holder_of
from hestia.runtime.wasm import is_wasm
from hestia.store import TenantStore


_EDGE_FORWARD_HEADERS = frozenset({"content-type", "accept"})


def edge_forward_headers(headers: Any) -> dict[str, str]:
    """Edge proxy allowlist. Authorization must never reach the tenant."""
    return {
        key: value
        for key, value in headers.items()
        if key.lower() in _EDGE_FORWARD_HEADERS
    }


def register(
    *,
    _read_capped: Callable[..., Awaitable[Any]],
    _release_claim: Callable[..., Any],
    _release_on_platform_failure: Callable[..., Any],
    _settle: Callable[..., Any],
    _still_serving: Callable[..., Any],
    _suspended: Callable[..., Any],
    _terms_for: Callable[..., Any],
    _wasm_edge: Callable[..., Awaitable[Any]],
    app: FastAPI,
    store: TenantStore,
) -> None:
    """Attach the tenant edge route to ``app``."""
    @app.api_route("/t/{slug}/{path:path}", methods=["GET", "POST"])
    async def edge(slug: str, path: str, request: Request) -> Response:
        row = store.get(slug)
        if row is None or row.status != "running":
            raise HTTPException(status_code=404, detail="tenant is not running")
        if holder_of(row.owner_pubkey) in _suspended():
            raise HTTPException(status_code=403, detail="this agent's owner is suspended here")
        # Only the paid door is gated. /health stays free so a buyer can see a
        # tenant is alive before paying it anything.
        claim = None
        if path == "invoke" and request.method == "POST":
            terms = _terms_for(row)
            if terms is not None:
                refusal, claim = _settle(
                    row,
                    terms,
                    request.headers.get("x-payment", "").strip(),
                    request.headers.get("x-payment-nonce", "").strip(),
                    request.headers.get("x-payment-secret", "").strip(),
                )
                if refusal is not None:
                    return refusal
        if is_wasm(row.listen_url):
            return await _wasm_edge(row, path, request, claim)
        target = f"{row.listen_url.rstrip('/')}/{path}"
        query = request.url.query
        if query:
            # Already percent-encoded by the caller; dropping it silently blanked
            # every query parameter a tenant was handed.
            target = f"{target}?{query}"
        raw = await request.body()
        headers = edge_forward_headers(request.headers)
        if not _still_serving(row):
            # The row was read before the body arrived and before any payment
            # settled. If the tenant was stopped or moved meanwhile, its old
            # address may already belong to someone else.
            _release_claim(claim)
            raise HTTPException(status_code=503, detail="tenant changed during the request; retry")
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                async with client.stream(
                    request.method, target, content=raw, headers=headers
                ) as streamed:
                    body = await _read_capped(streamed)
                    media = streamed.headers.get("content-type", "application/json")
                    status = streamed.status_code
        except httpx.HTTPError as exc:
            # The tenant is unreachable — the platform's fault, not the caller's.
            # A payment already claimed for this call is released so the same
            # transaction can be retried once the tenant is back.
            _release_claim(claim)
            raise HTTPException(status_code=502, detail="tenant unreachable") from exc
        except HTTPException:
            # An oversized tenant response (from _read_capped) delivered nothing —
            # release the claim, same as an unreachable tenant.
            _release_claim(claim)
            raise
        # A provider-side failure (5xx that is not the per-call deadline) gave the
        # buyer nothing, so the claim is released; a 4xx (the caller's input was
        # rejected) and a 504 (the call ran to its budget on that input) are billed.
        _release_on_platform_failure(claim, status)
        return Response(
            content=body,
            status_code=status,
            media_type=media,
        )
