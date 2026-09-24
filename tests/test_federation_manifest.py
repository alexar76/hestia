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
    # Hub federated invoke follows this field; missing it is the legacy
    # `/capabilities/{product}/{cap}/invoke` path, which 404s on this host.
    assert wk["mcp_endpoint"].endswith("/ai-market/v2/invoke")


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

def test_the_hearth_signs_a_receipt_the_hub_can_verify(tmp_path):
    """The interop receipt, checked against the hub's own canonical and verifier.

    The federation assay probes a free capability and asks one thing of the reply:
    is there a `receipt`, signed by the key `.well-known` advertises? Signing it
    with the hearth's own canonical is not enough — the two strings have to be the
    same string, or the hub reads a genuine signature as a forged one and the peer
    is never admitted. That is what kept the hearth's agents out of the catalogue.
    """
    Signer, _ = hub_bits()
    from hestia.signing import ProviderSigner, hub_receipt_canonical

    hearth = ProviderSigner(tmp_path / "hearth.key")
    hub = Signer(tmp_path / "hub.key")
    receipt = {
        "nonce": "0x" + "ab" * 16,
        "product_id": "hestia",
        "capability_id": "hestia.policy.decide@v1",
        "price_usd": 0.004,
        "timestamp": "2026-09-18T20:30:00Z",
        "success": 1,
        "latency_ms": 7,
    }
    assert hub_receipt_canonical(receipt) == hub.receipt_canonical(receipt, 1)

    receipt["signature"] = hearth.sign_hub_receipt(receipt)
    assert hub.verify_receipt_signature(receipt, hearth.public_key_b64) is True
    # And it must not verify under anybody else's key, or the check proves nothing.
    assert hub.verify_receipt_signature(receipt, hub.public_key_b64) is False


def test_the_well_known_carries_the_protocol_list_a_hub_validates(tmp_path):
    """`protocol_versions` is required; publishing only `protocol` failed the assay."""
    doc = client(tmp_path).get("/.well-known/ai-market.json").json()
    assert doc["protocol_versions"] == ["v2"]
    assert doc["protocol"] == "aimarket/2"



def test_a_pq_strict_hub_accepts_the_hearth(tmp_path, monkeypatch):
    """hub.attestedmemory.net runs AIMARKET_PQC_REQUIRE=1 and failed the hearth's assay.

    Its ed25519 signatures verified; the hub refused them anyway because the hearth
    signed classic-only. With HESTIA_PQC on, all three documents a hub checks — the
    manifest, the interop receipt and the well-known — must pass `require_pq=True`
    against the hub's own verifier, and a stripped PQ layer must not.
    """
    Signer, _ = hub_bits()
    from aimarket_hub.signing import pqc_available

    if not pqc_available():
        pytest.skip("dilithium-py is not installed")
    monkeypatch.setenv("HESTIA_PQC", "1")
    api = client(tmp_path)
    wk = api.get("/.well-known/ai-market.json").json()
    key = wk["signer_public_key"]
    pq_key = wk["signature"]["pq_public_key"]
    assert pq_key and wk["signature"]["pq_algorithm"] == "ml-dsa-65"

    hub = Signer.__new__(Signer)
    assert Signer.verify_object_signature(wk, key, require_pq=True, pq_public_key_b64=pq_key)

    manifest = api.get("/ai-market/v2/manifest").json()
    canonical = hub.manifest_canonical(manifest)
    assert Signer.verify_hybrid(key, manifest["signature"], canonical,
                                require_pq=True, pq_public_key_b64=pq_key)
    stripped = {k: v for k, v in manifest["signature"].items() if not k.startswith("pq_")}
    assert not Signer.verify_hybrid(key, stripped, canonical, require_pq=True)

    reply = api.post(
        "/ai-market/v2/invoke",
        json={"capability_id": "hestia.hearth.list@v1", "input": {}},
    ).json()
    receipt = reply["receipt"]
    canonical = hub.receipt_canonical(receipt, 1)
    assert Signer.verify_hybrid(key, receipt["signature"], canonical,
                                require_pq=True, pq_public_key_b64=pq_key)


def test_the_pq_identity_survives_a_restart(tmp_path, monkeypatch):
    """A hub pins the PQ key on first sight; a new one on restart is `pq_key_mismatch`."""
    from hestia.signing import _PQ_LIB, ProviderSigner

    if not _PQ_LIB:
        pytest.skip("dilithium-py is not installed")
    first = ProviderSigner(tmp_path / "provider.key", pqc=True).pq_public_key_b64
    again = ProviderSigner(tmp_path / "provider.key", pqc=True).pq_public_key_b64
    assert first and first == again
    assert (tmp_path / "provider.key_mldsa").stat().st_mode & 0o777 == 0o600
    assert ProviderSigner(tmp_path / "provider.key", pqc=False).pq_public_key_b64 == ""
