from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from hestia.app import build_app
from hestia.config import Settings

OWNER = Ed25519PrivateKey.generate()
OWNER_PUB = base64.b64encode(OWNER.public_key().public_bytes_raw()).decode()


def capability(**kwargs) -> dict:
    body = {
        "product_id": "demo-agent",
        "capability_id": "demo.echo@v1",
        "name": "Demo echo",
        "description": "Echoes the payload so a live invoke can be proven.",
        "price_per_call_usd": 0.001,
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "publisher_id": "tests",
        "provider_pubkey": OWNER_PUB,
    }
    body.update(kwargs)
    return body


def deploy_payload(slug: str = "demo-echo", handler: str = "") -> dict:
    return {
        "slug": slug,
        "capability": capability(),
        "source": {"kind": "template", "handler": handler},
        "owner_pubkey": OWNER_PUB,
        "announce": False,
    }


def client(tmp_path: Path, **overrides) -> TestClient:
    settings = Settings.for_test(tmp_path)
    if overrides:
        settings = Settings(**{**settings.__dict__, **overrides})
    return TestClient(build_app(settings))


def auth(_client: TestClient) -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}
