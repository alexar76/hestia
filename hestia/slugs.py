"""Tenant slug rules — these become URL path segments and Hub hostnames."""

from __future__ import annotations

import re

_SLUG = re.compile(r"^[a-z][a-z0-9-]{1,46}[a-z0-9]$")
_DOUBLE_DASH = re.compile(r"--")

RESERVED = frozenset(
    {
        "admin",
        "ai-market",
        "api",
        "assets",
        "console",
        "docs",
        "health",
        "hestia",
        "hub",
        "invoke",
        "login",
        "metrics",
        "static",
        "status",
        "t",
        "ui",
        "v1",
        "v2",
        "well-known",
        "www",
    }
)


def validate_slug(slug: str) -> str:
    value = (slug or "").strip().lower()
    if not _SLUG.match(value) or _DOUBLE_DASH.search(value):
        raise ValueError(
            "slug must be 3–48 chars, start with a letter, use [a-z0-9-], "
            "and must not contain '--'"
        )
    if value in RESERVED:
        raise ValueError(f"slug '{value}' is reserved")
    return value
