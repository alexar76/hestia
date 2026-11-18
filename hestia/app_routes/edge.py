"""The tenant edge: /t/<slug>/<path> proxied to the tenant."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from hestia.names import holder_of
from hestia.payments import decode_tenant_json, tenant_request_key
from hestia.runtime.wasm import is_wasm
from hestia.store import TenantStore

_EDGE_FORWARD_HEADERS = frozenset({"content-type", "accept"})
_ENCODED_SEPARATOR = re.compile(br"%(?:2f|5c)", re.IGNORECASE)


def canonical_edge_path(path: str, raw_path: bytes) -> str:
    """Use one unambiguous tenant path for both payment admission and dispatch.

    ASGI has already decoded the path once. URL clients and upstream routers can
    normalize it again, so reject paths whose meaning could change at that hop.
    Query parameters are handled separately by the edge and never enter this path.
    """
    # Root/index hosting is legitimate. Preserve ordinary directory paths,
    # while the two reserved routes have one spelling for admission/dispatch.
    if path in ("invoke/", "health/"):
        path = path[:-1]
    segments = path.split("/") if path else []
    if segments and segments[-1] == "":
        segments.pop()
    if (
        _ENCODED_SEPARATOR.search(raw_path)
        or any(char in path for char in "\\%?#;")
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or any(segment in ("", ".", "..") for segment in segments)
    ):
        raise HTTPException(status_code=400, detail="ambiguous tenant path")
    return path


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
    _deliver_paid: Callable[..., Awaitable[Response]],
    _settle: Callable[..., Any],
    _suspended: Callable[..., Any],
    _terms_for: Callable[..., Any],
    _wasm_edge: Callable[..., Awaitable[Any]],
    app: FastAPI,
    store: TenantStore,
) -> None:
    """Attach the tenant edge route to ``app``."""
    @app.api_route("/t/{slug}/{path:path}", methods=["GET", "POST"])
    async def edge(slug: str, path: str, request: Request) -> Response:
        path = canonical_edge_path(path, request.scope.get("raw_path", b""))
        row = store.get(slug)
        if row is None or row.status != "running":
            raise HTTPException(status_code=404, detail="tenant is not running")
        if holder_of(row.owner_pubkey) in _suspended():
            raise HTTPException(status_code=403, detail="this agent's owner is suspended here")
        # Only the paid door is gated. /health stays free so a buyer can see a
        # tenant is alive before paying it anything.
        claim = None
        headers = edge_forward_headers(request.headers)
        query = request.url.query
        wasm = is_wasm(row.listen_url)
        wasm_body = {}
        if path == "invoke" and request.method == "POST":
            raw = await request.body()
            if wasm:
                try:
                    wasm_body = json.loads(raw.decode() or "{}")
                except (UnicodeDecodeError, ValueError):
                    return JSONResponse(status_code=400, content={"ok": False, "error": "invalid json"})
                if not isinstance(wasm_body, dict):
                    return JSONResponse(status_code=400,
                                        content={"ok": False, "error": "body must be an object"})
                try:
                    json.dumps(wasm_body, ensure_ascii=False, allow_nan=False).encode()
                except ValueError:
                    return JSONResponse(status_code=400, content={"ok": False, "error": "invalid json"})
            terms = _terms_for(row)
            tx_hash = request.headers.get("x-payment", "").strip()
            if terms is not None or tx_hash:
                content_type = headers.get("content-type", "")
                immediate, claim = await run_in_threadpool(
                    _settle,
                    row,
                    terms,
                    tx_hash,
                    request.headers.get("x-payment-nonce", "").strip(),
                    request.headers.get("x-payment-secret", "").strip(),
                    tenant_request_key("edge", raw, query, content_type, headers.get("accept", "")),
                )
                if immediate is not None:
                    return immediate
        if wasm:
            return await _wasm_edge(row, path, request.method, wasm_body, claim)
        target = f"{row.listen_url.rstrip('/')}/{path}"
        if query:
            # Already percent-encoded by the caller; dropping it silently blanked
            # every query parameter a tenant was handed.
            target = f"{target}?{query}"
        raw = await request.body()

        async def send(_snapshot: Any) -> Response:
            async with httpx.AsyncClient(timeout=20.0) as client:
                async with client.stream(
                    request.method, target, content=raw, headers=headers
                ) as streamed:
                    body = await _read_capped(streamed)
                    media = streamed.headers.get("content-type", "application/json")
                    status = streamed.status_code
            if claim and status == 200 and media.partition(";")[0].lower() == "application/json":
                decode_tenant_json(body)
            return Response(content=body, status_code=status, media_type=media)

        return await _deliver_paid(row, claim, send)
