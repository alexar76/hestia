"""The hearth must be indexable by a real AIMarket hub, not merely well-formed.

These assert against the hub's OWN validator and signer, imported from the
sibling package, so the contract cannot drift on our side alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.conftest import auth, client, deploy_payload

HUB = Path(__file__).resolve().parent.parent.parent / "aimarket-hub"
if HUB.is_dir() and str(HUB) not in sys.path:
    sys.path.insert(0, str(HUB))


def hub_bits():
    try:
        from aimarket_hub.signing import Signer
        from aimarket_hub.validator import validate_manifest
    except ImportError:  # pragma: no cover - standalone checkout
        pytest.skip("sibling aimarket-hub sources are not on the path")
    return Signer, validate_manifest


HANDLER = "def handle(payload):\n    return {'ok': True}\n"


def test_manifest_passes_the_hubs_own_validator(tmp_path) -> None:
    Signer, validate_manifest = hub_bits()
    api = client(tmp_path)
    assert validate_manifest(api.get("/ai-market/v2/manifest").json()) == []


def test_manifest_signature_verifies_against_the_advertised_key(tmp_path) -> None:
    Signer, _ = hub_bits()
    api = client(tmp_path)
    wk = api.get("/.well-known/ai-market.json").json()
    manifest = api.get("/ai-market/v2/manifest").json()
    # Exactly what a crawler does: pin the key from well-known, verify with it.
    signer = Signer.__new__(Signer)
    assert Signer.verify_manifest_signature(signer, manifest, wk["signer_public_key"])


def test_a_tampered_price_breaks_the_manifest_signature(tmp_path) -> None:
    Signer, _ = hub_bits()
    api = client(tmp_path)
    wk = api.get("/.well-known/ai-market.json").json()
    manifest = api.get("/ai-market/v2/manifest").json()
    manifest["tools"][0]["price_per_call_usd"] = 0.0001
    signer = Signer.__new__(Signer)
    assert not Signer.verify_manifest_signature(signer, manifest, wk["signer_public_key"])


def test_well_known_advertises_what_a_crawler_pins(tmp_path) -> None:
    api = client(tmp_path)
    wk = api.get("/.well-known/ai-market.json").json()
    assert wk["signer_public_key"]
    assert wk["manifest_url"].endswith("/ai-market/v2/manifest")


def test_a_running_tenant_is_listed_and_routable_by_capability_id(tmp_path) -> None:
    api = client(tmp_path)
    body = deploy_payload("loud", HANDLER)
    body["capability"]["capability_id"] = "loud.thing@v1"
    body["payout_address"] = "0x1218000000000000000000000000000000000000"
    assert api.post("/v1/tenants", json=body, headers=auth(api)).status_code == 200

    tools = api.get("/ai-market/v2/manifest").json()["tools"]
    listed = [t for t in tools if t["capability_id"] == "loud.thing@v1"]
    assert len(listed) == 1
    # A hub keeps no per-tool URL; it routes against this single endpoint.
    assert listed[0]["invoke_url"].endswith("/ai-market/v2/invoke")
    # Announce → catalogue: the seller wallet travels with the tool.
    assert listed[0]["payout_address"] == "0x1218000000000000000000000000000000000000"

    # And that route must actually reach the tenant.
    res = api.post(
        "/ai-market/v2/invoke",
        json={"capability_id": "loud.thing@v1", "input": {"x": 1}},
    )
    assert res.status_code == 200
    assert res.json()["result"] == {"ok": True}


def test_the_manifest_and_the_public_roster_agree(tmp_path) -> None:
    """Two public views of the same hearth must not disagree."""
    api = client(tmp_path)
    body = deploy_payload("both", HANDLER)
    body["capability"]["capability_id"] = "both.thing@v1"
    api.post("/v1/tenants", json=body, headers=auth(api))
    roster = {t["capability_id"] for t in api.get("/v1/hearth").json()["tenants"]}
    tools = {t["capability_id"] for t in api.get("/ai-market/v2/manifest").json()["tools"]}
    assert roster <= tools


def test_a_stopped_tenant_leaves_the_manifest(tmp_path) -> None:
    api = client(tmp_path)
    body = deploy_payload("gone", HANDLER)
    body["capability"]["capability_id"] = "gone.thing@v1"
    api.post("/v1/tenants", json=body, headers=auth(api))
    api.post("/v1/tenants/gone/stop", headers=auth(api))
    ids = [t["capability_id"] for t in api.get("/ai-market/v2/manifest").json()["tools"]]
    assert "gone.thing@v1" not in ids


def test_routing_an_unknown_capability_is_a_404(tmp_path) -> None:
    api = client(tmp_path)
    res = api.post(
        "/ai-market/v2/invoke", json={"capability_id": "nobody.here@v1", "input": {}}
    )
    assert res.status_code == 404


def test_capabilities_count_matches_the_tools_it_signed(tmp_path) -> None:
    api = client(tmp_path)
    manifest = api.get("/ai-market/v2/manifest").json()
    assert manifest["capabilities_count"] == len(manifest["tools"])
    assert manifest["protocol_version"] == "v2"
    assert manifest["generated_at"].endswith("Z")
