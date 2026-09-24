"""Provider identity: Ed25519, plus an optional ML-DSA-65 layer (hybrid signing).

Same scar tissue as create-aimarket-agent: no symlinks.

The post-quantum half exists because a hub running `AIMARKET_PQC_REQUIRE=1`
(hub.attestedmemory.net does) refuses a classic-only manifest and a classic-only
receipt: the ed25519 signature verified, and the assay still said `fail`. Hybrid
means the SAME canonical string signed twice, in the additive `pq_algorithm` /
`pq_public_key` / `pq_value` fields of ``aimarket_hub.signing`` — a verifier that
never heard of ML-DSA reads `algorithm` + `value` and ignores the rest.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from pathlib import Path

try:
    from dilithium_py.ml_dsa import ML_DSA_65 as _MLDSA

    _PQ_LIB = True
except Exception:  # pragma: no cover - optional extra
    _MLDSA = None
    _PQ_LIB = False

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def pqc_enabled() -> bool:
    return (os.environ.get("HESTIA_PQC") or "").strip().lower() in ("1", "true", "yes", "on")


class ProviderSigner:
    def __init__(self, path: Path, *, pqc: bool | None = None) -> None:
        self.path = path
        if self.path.parent.exists() and self.path.parent.is_symlink():
            raise RuntimeError(f"provider key directory {self.path.parent} must not be a link")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists():
            seed = self._read_existing_seed()
        else:
            seed = Ed25519PrivateKey.generate().private_bytes_raw()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(seed)
        self.private = Ed25519PrivateKey.from_private_bytes(seed)
        if pqc is None:
            pqc = pqc_enabled()
        self._pq: tuple[bytes, bytes] | None = None
        if pqc:
            # Loud, never a quiet downgrade: a hearth that says HESTIA_PQC=1 and signs
            # classic-only looks healthy everywhere except on the one hub that refuses it.
            if not _PQ_LIB:
                raise RuntimeError(
                    "HESTIA_PQC is on but dilithium-py is missing — install aimarket-hestia[pqc]"
                )
            self._pq = self._load_or_make_pq(Path(f"{self.path}_mldsa"))

    def _load_or_make_pq(self, path: Path) -> tuple[bytes, bytes]:
        """`{key}_mldsa` holding `pk.hex()\\nsk.hex()` — the hub's own file layout.

        Beside the classical key, so it lives on the same volume: a PQ key that is not
        persisted is a new PQ identity on every restart, and a hub that pinned the old
        one refuses the hearth as `pq_key_mismatch`.
        """
        if path.exists():
            info = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise RuntimeError(f"provider PQ key {path} must be a regular file, not a link")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "r", encoding="ascii") as handle:
                os.fchmod(handle.fileno(), 0o600)
                lines = handle.read().split("\n")
            try:
                return bytes.fromhex(lines[0]), bytes.fromhex(lines[1])
            except (IndexError, ValueError) as exc:
                raise RuntimeError(f"provider PQ key {path} is corrupted") from exc
        pk, sk = _MLDSA.keygen()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(f"{pk.hex()}\n{sk.hex()}\n")
        return pk, sk

    @property
    def pq_public_key_b64(self) -> str:
        """The ML-DSA-65 public key, or "" when PQ signing is off."""
        return base64.b64encode(self._pq[0]).decode() if self._pq else ""

    def _pq_fields(self, canonical: str) -> dict[str, str]:
        if self._pq is None:
            return {}
        pk, sk = self._pq
        return {
            "pq_algorithm": "ml-dsa-65",
            "pq_public_key": base64.b64encode(pk).decode(),
            "pq_value": base64.b64encode(_MLDSA.sign(sk, canonical.encode())).decode(),
        }

    def _read_existing_seed(self) -> bytes:
        path_info = self.path.lstat()
        if self.path.is_symlink() or not stat.S_ISREG(path_info.st_mode):
            raise RuntimeError(f"provider key {self.path} must be a regular file, not a link")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened_info = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened_info.st_mode) or (
                opened_info.st_dev,
                opened_info.st_ino,
            ) != (path_info.st_dev, path_info.st_ino):
                raise RuntimeError(f"provider key {self.path} changed while it was being opened")
            os.fchmod(handle.fileno(), 0o600)
            seed = handle.read(33)
        if len(seed) != 32:
            raise RuntimeError(f"provider key {self.path} is corrupted; expected 32 bytes")
        return seed

    @property
    def public_key_b64(self) -> str:
        return base64.b64encode(self.private.public_key().public_bytes_raw()).decode()

    def sign_manifest(self, manifest: dict) -> dict[str, str]:
        """Signature block for a federation manifest, in the hub's own shape."""
        canonical = manifest_canonical(manifest)
        return {
            "algorithm": "ed25519",
            "public_key": self.public_key_b64,
            "value": base64.b64encode(self.private.sign(canonical.encode())).decode(),
            **self._pq_fields(canonical),
        }

    def sign_object(self, document: dict) -> dict[str, str]:
        """Whole-document signature, the hub's `sign_object` / `object_canonical`.

        Used for `.well-known`: a hub pins a peer's PQ key from THIS block
        (`signature.pq_public_key`), and relays its gossip only when it verifies.
        """
        canonical = object_canonical(document)
        return {
            "algorithm": "ed25519",
            "public_key": self.public_key_b64,
            "value": base64.b64encode(self.private.sign(canonical.encode())).decode(),
            **self._pq_fields(canonical),
        }

    def sign_result(
        self, result: dict, *, capability_id: str, product_id: str, input_payload: dict
    ) -> str:
        canonical = _canonical_receipt(result, capability_id, product_id, input_payload)
        return base64.b64encode(self.private.sign(canonical.encode())).decode()

    def sign_hub_receipt(self, receipt: dict) -> dict:
        """The signature block a hub's `verify_receipt_signature` reads.

        No `version` key: that is exactly a v1 block, which is what an interop
        receipt is. The hub reads `receipt["signature"]`; with PQ on, the same
        canonical also carries the ML-DSA-65 layer a PQ-strict hub demands.
        """
        canonical = hub_receipt_canonical(receipt)
        return {
            "algorithm": "ed25519",
            "value": base64.b64encode(self.private.sign(canonical.encode())).decode(),
            **self._pq_fields(canonical),
        }


def _canonical_receipt(
    result: dict, capability_id: str, product_id: str, input_payload: dict
) -> str:
    input_json = json.dumps(
        input_payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return json.dumps(
        {
            "capability_id": capability_id,
            "product_id": product_id,
            "input_sha256": hashlib.sha256(input_json.encode()).hexdigest(),
            "result": result,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def hub_receipt_canonical(receipt: dict) -> str:
    """Byte-for-byte mirror of ``aimarket_hub.signing.Signer.receipt_canonical`` v1.

    A hub's federation assay probes a free capability and then asks one question of
    the answer: is there a ``receipt`` object, and is it signed by the key this peer
    advertises in ``.well-known``? HESTIA signed its results with its OWN canonical
    (`_canonical_receipt`, which binds the input hash), so the hub found a signature
    it could not check against a shape it knew — the probe came back
    "response had no receipt object" and the assay verdict was `fail`, which is why
    the hearth's agents never reached the catalogue.

    So the hearth now emits BOTH: its own receipt, which binds the input, and this
    one, which is the shape a hub verifies. Like ``manifest_canonical`` above, a
    cross-check test asserts this string is identical to the hub's, because a silent
    divergence reads as a forged signature and the peer just stops being admissible.

    v1 takes no `version` field — absent means 1 — and interpolates the values
    directly, so the caller must hand it the same Python types the hub would.
    """
    return (
        f"nonce:{receipt.get('nonce','')}"
        f"|product_id:{receipt.get('product_id','')}"
        f"|capability_id:{receipt.get('capability_id','')}"
        f"|price_usd:{receipt.get('price_usd',0)}"
        f"|timestamp:{receipt.get('timestamp','')}"
        f"|success:{1 if receipt.get('success') else 0}"
        f"|latency_ms:{receipt.get('latency_ms',0)}"
    )


def object_canonical(document: dict) -> str:
    """Mirror of ``aimarket_hub.signing.Signer.object_canonical``: the whole object minus `signature`."""
    body = {k: v for k, v in document.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def manifest_canonical(manifest: dict) -> str:
    """The exact string an AIMarket hub signs and verifies for a manifest.

    Mirrors ``aimarket_hub.signing.Signer.manifest_canonical``. It must stay
    byte-identical to that implementation: a hub verifies a peer's manifest by
    recomputing this from the parsed document, so any divergence reads as a
    forged signature and the hearth silently stops being indexable.

    `tools` and `by_hub` are covered by digest rather than inline so that a
    relay cannot retouch a price or a trust score under a still-valid signature.
    """
    tools_hash = hashlib.sha256(
        json.dumps(manifest.get("tools", []), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    by_hub_hash = hashlib.sha256(
        json.dumps(manifest.get("by_hub", {}), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return (
        f"capabilities_count:{manifest.get('capabilities_count', 0)}"
        f"|generated_at:{manifest.get('generated_at', '')}"
        f"|protocol_version:{manifest.get('protocol_version', 'v1')}"
        f"|tools_hash:{tools_hash}"
        f"|by_hub_hash:{by_hub_hash}"
    )


def verify_owner_signature(*, public_key_b64: str, message: bytes, signature_b64: str) -> None:
    try:
        raw = base64.b64decode(public_key_b64, validate=True)
        sig = base64.b64decode(signature_b64, validate=True)
        key = Ed25519PublicKey.from_public_bytes(raw)
        key.verify(sig, message)
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise ValueError("owner signature is invalid") from exc
