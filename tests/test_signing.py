from pathlib import Path

import pytest

from hestia.signing import ProviderSigner, verify_owner_signature


def test_sign_and_verify_roundtrip(tmp_path: Path) -> None:
    signer = ProviderSigner(tmp_path / "provider.key")
    sig = signer.sign_result(
        {"ok": True},
        capability_id="hestia.hearth.list@v1",
        product_id="hestia",
        input_payload={},
    )
    assert signer.public_key_b64
    assert sig


def test_corrupt_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "provider.key"
    path.write_bytes(b"too-short")
    path.chmod(0o600)
    with pytest.raises(RuntimeError, match="corrupted"):
        ProviderSigner(path)


def test_invalid_owner_signature() -> None:
    with pytest.raises(ValueError):
        verify_owner_signature(
            public_key_b64="AAAA",
            message=b"x",
            signature_b64="AAAA",
        )
