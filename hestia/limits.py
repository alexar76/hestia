"""Request body ceiling, enforced at the ASGI layer.

A Content-Length check alone is not a ceiling: a chunked request carries no
Content-Length, so the header test waves it through and the endpoint reads the
whole thing into memory. `/ai-market/v2/invoke` parses its body *before* it
knows the capability, hence before `_auth` — so an unauthenticated client could
stream an arbitrarily large body into the hearth. This middleware caps the
bytes themselves.
"""

from __future__ import annotations

import json
import os
from typing import Any

_BAD_LENGTH = object()

# Shared with Settings / tenant_stub. One ceiling, not three magic numbers.
DEFAULT_MAX_BODY_BYTES = 256 * 1024


def resolve_max_body_bytes(raw: str | None = None) -> int:
    """Parse HESTIA_MAX_BODY_BYTES (or a caller-supplied raw). Must be > 0."""
    if raw is None:
        raw = os.environ.get("HESTIA_MAX_BODY_BYTES", "")
    text = raw.strip()
    value = int(text) if text else DEFAULT_MAX_BODY_BYTES
    if value <= 0:
        raise RuntimeError("HESTIA_MAX_BODY_BYTES must be > 0")
    return value


def declared_length(scope: dict[str, Any]) -> Any:
    """Content-Length as an int, None when absent, _BAD_LENGTH when unparsable."""
    for key, value in scope.get("headers") or ():
        if key == b"content-length":
            try:
                return int(value.decode("latin-1").strip())
            except ValueError:
                return _BAD_LENGTH
    return None


def is_chunked(scope: dict[str, Any]) -> bool:
    for key, value in scope.get("headers") or ():
        if key == b"transfer-encoding" and b"chunked" in value.lower():
            return True
    return False


async def _refuse(send, status: int, error: str) -> None:
    raw = json.dumps({"ok": False, "error": error}, separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": raw, "more_body": False})


class BodyLimitMiddleware:
    """Refuse a body over `max_bytes`, declared or streamed."""

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        length = declared_length(scope)
        if length is _BAD_LENGTH:
            await _refuse(send, 400, "bad content-length")
            return
        if length is not None:
            # The server enforces a declared length, so the header is the truth here.
            if length > self.max_bytes:
                await _refuse(send, 413, "body too large")
                return
            await self.app(scope, receive, send)
            return
        if not is_chunked(scope):
            await self.app(scope, receive, send)
            return
        # Chunked: no declared length, so count the bytes. Buffering is bounded
        # by max_bytes — we stop reading the moment the cap is crossed.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                await _refuse(send, 413, "body too large")
                return
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)
