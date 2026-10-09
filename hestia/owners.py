"""Tenant owners: who may change which agent on a shared hearth.

Until 2026-10-04 every write on a hearth — deploy, stop, announce — took one operator
token, and ``owner_pubkey`` was stored on the tenant row and never read again. A second
business given that token could stop or overwrite every other business's agents, so a
reference hearth could only ever hold the operator's own.

An owner is an Ed25519 key. Every owner request is signed over what it does and where:

    hestia-owner/1
    <hearth public base>      the hearth it is meant for — a request for one hearth
                              cannot be replayed against another
    <METHOD>
    <path>                    without the query string
    <unix timestamp>          within ``owner_skew_s`` of the hearth's clock
    <nonce>                   8-64 of [A-Za-z0-9_-], spent once per owner
    <sha256 hex of the body>  the exact bytes sent

and carried in four headers: ``X-Hestia-Owner`` (base64 raw public key),
``X-Hestia-Timestamp``, ``X-Hestia-Nonce``, ``X-Hestia-Signature`` (base64).

The operator token keeps every power it had. An owner may deploy onto a free slug or one
it already owns, within its quota, and may stop, announce and list only its own agents.

Some keys are never an owner: the 32 zero bytes the hestia-agents package ships as a
placeholder, and every other small-order point. A permissive Ed25519 verifier accepts a
forged signature under a small-order key, so a tenant carrying one (the three reference
agents do) stays the operator's alone.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PROOF_VERSION = "hestia-owner/1"
HEADER_OWNER = "x-hestia-owner"
HEADER_TIMESTAMP = "x-hestia-timestamp"
HEADER_NONCE = "x-hestia-nonce"
HEADER_SIGNATURE = "x-hestia-signature"

_NONCE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# Encodings of the eight small-order points of edwards25519, plus the non-canonical
# encodings of the same points (y >= p) — the blocklist libsodium applies. The all-zero
# key is the hestia-agents placeholder.
_SMALL_ORDER_HEX = (
    "0100000000000000000000000000000000000000000000000000000000000000",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0000000000000000000000000000000000000000000000000000000000000080",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa",
    "0100000000000000000000000000000000000000000000000000000000000080",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
)
_BLOCKED_KEYS = frozenset(bytes.fromhex(h) for h in _SMALL_ORDER_HEX)


class OwnerProofError(ValueError):
    """The request does not prove an owner. The message is safe to return."""


@dataclass(frozen=True)
class OwnerProof:
    pubkey: str      # canonical base64 of the raw 32-byte key
    nonce: str


def canonical_owner_key(value: str) -> str:
    """The owner key in canonical form, or OwnerProofError.

    Canonical base64 of exactly 32 bytes that load as an Ed25519 public key and are not a
    small-order point. Comparing canonical strings is what makes "the same owner" mean the
    same bytes — two base64 spellings of one key must not be two owners.
    """
    try:
        raw = base64.b64decode((value or "").strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise OwnerProofError("owner key is not base64") from exc
    if len(raw) != 32:
        raise OwnerProofError("owner key must be 32 bytes")
    if raw in _BLOCKED_KEYS:
        raise OwnerProofError("owner key is a placeholder or small-order point and cannot own")
    try:
        Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise OwnerProofError("owner key is not an Ed25519 public key") from exc
    return base64.b64encode(raw).decode()


def is_ownable_key(value: str) -> bool:
    try:
        canonical_owner_key(value)
        return True
    except OwnerProofError:
        return False


def owner_message(
    *, hearth: str, method: str, path: str, timestamp: str, nonce: str, body: bytes
) -> bytes:
    return "\n".join((
        PROOF_VERSION,
        hearth.rstrip("/"),
        method.upper(),
        path,
        timestamp,
        nonce,
        hashlib.sha256(body).hexdigest(),
    )).encode()


def has_owner_headers(headers: Mapping[str, str]) -> bool:
    return bool(headers.get(HEADER_OWNER) or headers.get(HEADER_SIGNATURE))


def verify_owner_request(
    *,
    headers: Mapping[str, str],
    hearth: str,
    method: str,
    path: str,
    body: bytes,
    skew_s: int,
    now: float | None = None,
) -> OwnerProof:
    """Check the signature, the clock and the shape. Spending the nonce is the caller's
    job (it needs the ledger), and must happen before anything is acted on."""
    owner = canonical_owner_key(headers.get(HEADER_OWNER) or "")
    timestamp = (headers.get(HEADER_TIMESTAMP) or "").strip()
    nonce = (headers.get(HEADER_NONCE) or "").strip()
    signature = (headers.get(HEADER_SIGNATURE) or "").strip()
    if not timestamp.isdigit():
        raise OwnerProofError("X-Hestia-Timestamp must be unix seconds")
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > skew_s:
        raise OwnerProofError(f"X-Hestia-Timestamp is outside ±{skew_s}s of the hearth clock")
    if not _NONCE.match(nonce):
        raise OwnerProofError("X-Hestia-Nonce must be 8-64 of [A-Za-z0-9_-]")
    try:
        sig = base64.b64decode(signature, validate=True)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(owner)).verify(
            sig,
            owner_message(hearth=hearth, method=method, path=path,
                          timestamp=timestamp, nonce=nonce, body=body),
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise OwnerProofError("owner signature is invalid") from exc
    return OwnerProof(pubkey=owner, nonce=nonce)


def sign_owner_request(
    private_key: Ed25519PrivateKey,
    *,
    hearth: str,
    method: str,
    path: str,
    body: bytes = b"",
    now: float | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Headers for one owner request — the client half, for owners and their tools."""
    timestamp = str(int(time.time() if now is None else now))
    nonce = nonce or secrets.token_urlsafe(18)
    message = owner_message(hearth=hearth, method=method, path=path,
                            timestamp=timestamp, nonce=nonce, body=body)
    return {
        "X-Hestia-Owner": base64.b64encode(private_key.public_key().public_bytes_raw()).decode(),
        "X-Hestia-Timestamp": timestamp,
        "X-Hestia-Nonce": nonce,
        "X-Hestia-Signature": base64.b64encode(private_key.sign(message)).decode(),
    }
