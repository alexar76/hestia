"""Ed25519 provider identity. Same scar tissue as create-aimarket-agent: no symlinks."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class ProviderSigner:
    def __init__(self, path: Path) -> None:
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

    def sign_result(
        self, result: dict, *, capability_id: str, product_id: str, input_payload: dict
    ) -> str:
        canonical = _canonical_receipt(result, capability_id, product_id, input_payload)
        return base64.b64encode(self.private.sign(canonical.encode())).decode()


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


def verify_owner_signature(*, public_key_b64: str, message: bytes, signature_b64: str) -> None:
    try:
        raw = base64.b64decode(public_key_b64, validate=True)
        sig = base64.b64decode(signature_b64, validate=True)
        key = Ed25519PublicKey.from_public_bytes(raw)
        key.verify(sig, message)
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise ValueError("owner signature is invalid") from exc
