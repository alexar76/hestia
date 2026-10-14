"""Owners: who may change which agent on a shared hearth (hestia/owners.py).

Before this, one operator token did every write and owner_pubkey was stored and never read,
so a second business given the token could stop or overwrite every other business's agents.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time

import pytest
from conftest import auth, deploy_payload
from conftest import client as _client
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hestia.owners import (
    OwnerProofError,
    canonical_owner_key,
    sign_owner_request,
)


def client(tmp_path, **overrides):
    """SQLite by default; a fresh schema on HESTIA_TEST_DATABASE_URL when it is set, so the
    owner rules (unique nonces, quotas) are proven on the Postgres ledger prod runs too."""
    url = (os.environ.get("HESTIA_TEST_DATABASE_URL") or "").strip()
    if url:
        import psycopg

        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        overrides = {"database_url": url, **overrides}
    return _client(tmp_path, **overrides)

HEARTH = "http://127.0.0.1:9480"
PLACEHOLDER = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="   # hestia-agents ships it
CODE = "def handle(payload):\n    return {'doubled': int(payload.get('n', 0)) * 2}\n"


class Owner:
    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.pub = base64.b64encode(self.key.public_key().public_bytes_raw()).decode()

    def headers(self, method: str, path: str, body: bytes = b"", **kw) -> dict[str, str]:
        return sign_owner_request(self.key, hearth=kw.pop("hearth", HEARTH), method=method,
                                  path=path, body=body, **kw)

    def body(self, slug: str, handler: str = "", cap_id: str = "") -> bytes:
        payload = deploy_payload(slug=slug, handler=handler)
        payload["owner_pubkey"] = self.pub
        # A product and a publisher belong to their first holder (hestia/names.py), so each
        # owner here sells under its own.
        mark = hashlib.sha256(self.pub.encode()).hexdigest()[:10]
        payload["capability"]["product_id"] = f"product-{mark}"
        payload["capability"]["publisher_id"] = f"publisher-{mark}"
        if cap_id:
            # One capability id per running agent: the hearth refuses a second one under an id
            # it already serves.
            payload["capability"]["capability_id"] = cap_id
            payload["capability"]["name"] = "Agent " + cap_id
        return json.dumps(payload).encode()

    def deploy(self, api, slug: str, handler: str = "", cap_id: str = ""):
        raw = self.body(slug, handler, cap_id)
        return api.post("/v1/tenants", content=raw,
                        headers={**self.headers("POST", "/v1/tenants", raw),
                                 "content-type": "application/json"})

    def post(self, api, path: str):
        return api.post(path, headers=self.headers("POST", path))


def admit(api, owner: Owner, **fields):
    res = api.post("/v1/owners", json={"pubkey": owner.pub, **fields}, headers=auth(api))
    assert res.status_code == 200, res.text
    return res.json()["owner"]


# ── who may deploy ─────────────────────────────────────────────────────────────

def test_an_owner_the_operator_did_not_admit_is_refused(tmp_path) -> None:
    api = client(tmp_path)
    assert Owner().deploy(api, "lone-agent").status_code == 403


def test_an_admitted_owner_deploys_as_itself(tmp_path) -> None:
    api = client(tmp_path)
    alice = Owner()
    admit(api, alice)
    res = alice.deploy(api, "alice-echo", handler=CODE)
    assert res.status_code == 200, res.text
    assert api.post("/t/alice-echo/invoke", json={"n": 4}).json()["result"]["doubled"] == 8
    row = [t for t in api.get("/v1/tenants", headers=auth(api)).json()["tenants"]
           if t["slug"] == "alice-echo"][0]
    assert row["owner_pubkey"] == alice.pub


def test_an_owner_cannot_deploy_in_another_keys_name(tmp_path) -> None:
    api = client(tmp_path)
    alice, bob = Owner(), Owner()
    admit(api, alice)
    raw = bob.body("framed")                       # owner_pubkey = bob, signed by alice
    res = api.post("/v1/tenants", content=raw,
                   headers={**alice.headers("POST", "/v1/tenants", raw),
                            "content-type": "application/json"})
    assert res.status_code == 403


# ── the proof itself ───────────────────────────────────────────────────────────

def test_a_replayed_request_is_refused(tmp_path) -> None:
    api = client(tmp_path)
    alice = Owner()
    admit(api, alice)
    raw = alice.body("once")
    headers = {**alice.headers("POST", "/v1/tenants", raw), "content-type": "application/json"}
    assert api.post("/v1/tenants", content=raw, headers=headers).status_code == 200
    assert api.post("/v1/tenants", content=raw, headers=headers).status_code == 409


@pytest.mark.parametrize("tamper", ["body", "hearth", "path", "clock"])
def test_a_proof_for_anything_else_does_not_verify(tmp_path, tamper) -> None:
    api = client(tmp_path)
    alice = Owner()
    admit(api, alice)
    raw = alice.body("target")
    if tamper == "body":
        headers = alice.headers("POST", "/v1/tenants", alice.body("other-slug"))
    elif tamper == "hearth":
        headers = alice.headers("POST", "/v1/tenants", raw, hearth="https://another.hearth")
    elif tamper == "path":
        headers = alice.headers("POST", "/v1/tenants/target/stop", raw)
    else:
        headers = alice.headers("POST", "/v1/tenants", raw, now=time.time() - 3600)
    res = api.post("/v1/tenants", content=raw,
                   headers={**headers, "content-type": "application/json"})
    assert res.status_code == 401, res.text


# ── one owner, its own agents ──────────────────────────────────────────────────

def test_an_owner_cannot_touch_another_owners_agent(tmp_path) -> None:
    api = client(tmp_path)
    alice, bob = Owner(), Owner()
    admit(api, alice)
    admit(api, bob)
    assert alice.deploy(api, "alice-agent").status_code == 200
    assert bob.post(api, "/v1/tenants/alice-agent/stop").status_code == 403
    assert bob.post(api, "/v1/tenants/alice-agent/announce").status_code == 403
    assert bob.deploy(api, "alice-agent").status_code == 403          # no overwrite
    assert alice.post(api, "/v1/tenants/alice-agent/stop").status_code == 200


def test_the_operator_keeps_every_power(tmp_path) -> None:
    api = client(tmp_path)
    alice = Owner()
    admit(api, alice)
    assert alice.deploy(api, "alice-agent").status_code == 200
    assert api.post("/v1/tenants/alice-agent/stop", headers=auth(api)).status_code == 200


def test_a_placeholder_owned_agent_is_the_operators_alone(tmp_path) -> None:
    """The reference agents carry the all-zero placeholder key: nobody may own it."""
    api = client(tmp_path)
    payload = deploy_payload(slug="reference")
    payload["owner_pubkey"] = PLACEHOLDER
    assert api.post("/v1/tenants", json=payload, headers=auth(api)).status_code == 200
    res = api.post("/v1/owners", json={"pubkey": PLACEHOLDER}, headers=auth(api))
    assert res.status_code == 400
    alice = Owner()
    admit(api, alice)
    assert alice.post(api, "/v1/tenants/reference/stop").status_code == 403
    assert alice.deploy(api, "reference").status_code == 403


def test_an_owner_lists_only_its_own_agents(tmp_path) -> None:
    api = client(tmp_path)
    alice, bob = Owner(), Owner()
    admit(api, alice)
    admit(api, bob)
    alice.deploy(api, "alice-one", cap_id="alice.one@v1")
    bob.deploy(api, "bob-one", cap_id="bob.one@v1")
    mine = api.get("/v1/tenants", headers=alice.headers("GET", "/v1/tenants")).json()["tenants"]
    assert [t["slug"] for t in mine] == ["alice-one"]
    everything = api.get("/v1/tenants", headers=auth(api)).json()["tenants"]
    assert {t["slug"] for t in everything} == {"alice-one", "bob-one"}


def test_the_quota_counts_agents_not_redeploys(tmp_path) -> None:
    api = client(tmp_path)
    alice = Owner()
    admit(api, alice, max_tenants=1)
    assert alice.deploy(api, "only-one").status_code == 200
    assert alice.deploy(api, "only-one").status_code == 200         # redeploy is fine
    assert alice.deploy(api, "second-one").status_code == 429


def test_a_suspended_owner_is_refused(tmp_path) -> None:
    api = client(tmp_path)
    alice = Owner()
    admit(api, alice)
    assert alice.deploy(api, "alice-agent").status_code == 200
    admit(api, alice, status="suspended")
    assert alice.post(api, "/v1/tenants/alice-agent/stop").status_code == 403


def test_an_owner_without_code_rights_deploys_only_sealed_stubs(tmp_path) -> None:
    api = client(tmp_path)
    alice = Owner()
    admit(api, alice, may_run_code=False)
    assert alice.deploy(api, "with-code", handler=CODE).status_code == 403
    assert alice.deploy(api, "sealed").status_code == 200


# ── open mode ──────────────────────────────────────────────────────────────────

def test_open_mode_admits_any_key_without_code_rights(tmp_path) -> None:
    api = client(tmp_path, open_owners=True)
    stranger = Owner()
    assert stranger.deploy(api, "stranger-code", handler=CODE).status_code == 403
    assert stranger.deploy(api, "stranger-stub").status_code == 200
    owners = api.get("/v1/owners", headers=auth(api)).json()["owners"]
    assert [(o["pubkey"], o["may_run_code"]) for o in owners] == [(stranger.pub, 0)]
    # A self-admitted key is recorded as such: its agents share the open sandbox lane.
    assert owners[0]["admitted_by"] == "open"
    me = api.get("/v1/owners/me", headers=stranger.headers("GET", "/v1/owners/me")).json()
    assert me["admitted_by"] == "open"


def test_open_mode_still_refuses_the_placeholder(tmp_path) -> None:
    """A permissive verifier accepts forged signatures under a small-order key."""
    with pytest.raises(OwnerProofError):
        canonical_owner_key(PLACEHOLDER)


# ── the rest of the surface ────────────────────────────────────────────────────

def test_owners_are_managed_by_the_operator_only(tmp_path) -> None:
    api = client(tmp_path)
    alice = Owner()
    admit(api, alice)
    raw = json.dumps({"pubkey": alice.pub, "max_tenants": 8}).encode()
    res = api.post("/v1/owners", content=raw,
                   headers={**alice.headers("POST", "/v1/owners", raw),
                            "content-type": "application/json"})
    assert res.status_code == 401
    assert api.get("/v1/owners").status_code == 401


def test_an_owner_reads_its_own_standing(tmp_path) -> None:
    api = client(tmp_path)
    alice = Owner()
    admit(api, alice, max_tenants=2)
    alice.deploy(api, "alice-agent")
    me = api.get("/v1/owners/me", headers=alice.headers("GET", "/v1/owners/me")).json()
    assert me["admitted"] and me["max_tenants"] == 2 and me["tenants"] == ["alice-agent"]
    assert me["admitted_by"] == "operator"


def test_the_invoke_door_enforces_ownership_too(tmp_path) -> None:
    api = client(tmp_path)
    alice, bob = Owner(), Owner()
    admit(api, alice)
    admit(api, bob)
    alice.deploy(api, "alice-agent")
    raw = json.dumps({"capability_id": "hestia.host.stop@v1",
                      "input": {"slug": "alice-agent"}}).encode()
    for who, expected in ((bob, 403), (alice, 200)):
        res = api.post("/ai-market/v2/invoke", content=raw,
                       headers={**who.headers("POST", "/ai-market/v2/invoke", raw),
                                "content-type": "application/json"})
        assert res.status_code == expected, res.text


# ── the owner tool ─────────────────────────────────────────────────────────────

def test_the_owner_tool_makes_a_private_key_and_deploys_with_it(tmp_path, monkeypatch, capsys):
    import stat

    import httpx

    from hestia import owner_cli

    key_file = tmp_path / "owner.key"
    assert owner_cli.main(["keygen", "--out", str(key_file)]) == 0
    pub = capsys.readouterr().out.strip()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert owner_cli.main(["keygen", "--out", str(key_file)]) == 1     # never overwritten

    api = client(tmp_path)
    assert api.post("/v1/owners", json={"pubkey": pub}, headers=auth(api)).status_code == 200
    body_file = tmp_path / "deploy.json"
    body_file.write_text(json.dumps(deploy_payload(slug="tool-agent")))   # placeholder owner
    monkeypatch.setattr(httpx, "request", lambda method, url, **kw: api.request(
        method, url.replace(HEARTH, ""), content=kw.get("content"), headers=kw.get("headers")))
    assert owner_cli.main(["deploy", "--hearth", HEARTH, "--key", str(key_file),
                           "--body", str(body_file)]) == 0
    assert owner_cli.main(["stop", "--hearth", HEARTH, "--key", str(key_file),
                           "--slug", "tool-agent"]) == 0


def test_the_public_roster_says_whose_agent_each_is(tmp_path) -> None:
    """The hearth's roster splits the operator's own agents from admitted owners' — a
    dashboard counting "ours" against "third-party" reads it from here."""
    api = client(tmp_path)
    payload = deploy_payload(slug="reference")
    payload["owner_pubkey"] = PLACEHOLDER
    assert api.post("/v1/tenants", json=payload, headers=auth(api)).status_code == 200
    alice = Owner()
    admit(api, alice, label="Alice Labs")
    assert alice.deploy(api, "alice-one", cap_id="alice.one@v1").status_code == 200
    stopped = Owner()
    admit(api, stopped, label="gone")
    assert stopped.deploy(api, "gone-one", cap_id="gone.one@v1").status_code == 200
    assert api.post("/v1/tenants/gone-one/stop", headers=auth(api)).status_code == 200
    roster = api.get("/v1/hearth").json()
    owners = {t["slug"]: t["owner"] for t in roster["tenants"]}
    assert owners == {"reference": {"kind": "operator"}, "alice-one": {"kind": "owner", "label": "Alice Labs"}}
    assert (roster["running"], roster["operator_tenants"], roster["owner_tenants"]) == (2, 1, 1)
