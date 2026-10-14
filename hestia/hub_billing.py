"""Hubs that sell a tenant on its owner's behalf, and what each of them owes that owner.

A tenant bills a buyer per call in USDC (x402, payments.py). A hub cannot pay that way out of a
buyer's credits or a subcontracting allowance — one on-chain transfer per $0.009 call costs more in
gas than the call — so until 2026-10-04 a hub could not resell a hearth tenant at all, and turning
hearth payments on broke the one hub that did (modelmarket.dev answered `upstream_unpaid`).

Now a hub that holds a key for this hearth (``HESTIA_TENANT_HUB_KEYS``, the same value as its
``AIMARKET_PEER_API_KEYS`` entry) may call a tenant without paying it on chain, when:

* the tenant is the operator's (its owner key is the placeholder or another key nobody can own) —
  the operator configured the hub key, and the revenue was the operator's anyway; or
* the tenant's owner chose that hub, naming the credit account there that receives its share
  (``POST /v1/owners/me/billing``). Any other hub key gets a 402 and the hub charges its buyer
  nothing.

Every such call is recorded (``hub_billed_calls``) and answered with a ``hub_billing`` block signed
by the hearth's provider key — the key the hub pinned when it indexed this hearth. The hub verifies
it, credits ``bill_to_account`` the owner's share of what IT charged the buyer, and strips the block
before the buyer sees the answer. The hearth holds no money and moves none: it attests who did the
work for whom, and the owner reads the same rows (``GET /v1/owners/me/statement``) to reconcile
with what the hub paid.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any

#: A credit account id on an AIMarket hub (``acct_`` + 16 hex).
ACCOUNT_RE = re.compile(r"^acct_[0-9a-f]{16}$")


def parse_hub_keys(raw: str) -> tuple[tuple[str, str], ...]:
    """``url=key,url=key`` → ((url, key), ...). URLs are compared without a trailing slash."""
    out: list[tuple[str, str]] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        url, sep, key = part.rpartition("=")
        url, key = url.strip().rstrip("/"), key.strip()
        if not sep or not url.startswith(("https://", "http://")) or not key:
            raise RuntimeError(
                "HESTIA_TENANT_HUB_KEYS must be url=key pairs separated by commas"
            )
        out.append((url, key))
    return tuple(out)


def hub_for_key(presented: str, hubs: tuple[tuple[str, str], ...]) -> str | None:
    """The hub a key belongs to. Constant-time against every key: no early exit that times which."""
    raw = presented.encode()
    found = None
    for url, key in hubs:
        if hmac.compare_digest(raw, key.encode()):
            found = url
    return found


def billing_block(
    signer: Any,
    *,
    hearth: str,
    hub: str,
    slug: str,
    capability_id: str,
    owner_pubkey: str,
    bill_to_account: str,
    price_usd: float,
    input_payload: Any,
) -> dict[str, Any]:
    """The signed attestation a hub credits the owner from. ``id`` is unique per call."""
    block: dict[str, Any] = {
        "version": "hestia-hub-billing/1",
        "id": "hb_" + secrets.token_hex(12),
        "hearth": hearth.rstrip("/"),
        "hub": hub.rstrip("/"),
        "slug": slug,
        "capability_id": capability_id,
        "owner_pubkey": owner_pubkey,
        "bill_to_account": bill_to_account,
        "price_usd": float(price_usd),
        "input_sha256": hashlib.sha256(
            json.dumps(input_payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode()
        ).hexdigest(),
        "issued_at": int(time.time()),
    }
    block["signature"] = signer.sign_object(block)
    return block
