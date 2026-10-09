"""Hubs that sell a tenant on its owner's behalf (hestia/hub_billing.py).

2026-10-04: with hearth payments on, modelmarket.dev — which resells this hearth with a hub key —
answered `upstream_unpaid` for every tenant, and no hub could hire a hearth tenant inside a
subcontracting job at all, because a tenant took nothing but a per-call on-chain payment.
"""

from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from hestia.signing import object_canonical
from tests.conftest import auth, deploy_payload
from tests.conftest import client as _client
from tests.test_owners import CODE, Owner, admit

HUB = "https://hub.test"
HUB_KEY = "aimk_hubkey_for_tests"
OTHER_HUB = "https://other.test"
OTHER_KEY = "aimk_other_hub_key"
PLACEHOLDER = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
ACCOUNT = "acct_0123456789abcdef"


def client(tmp_path):
    return _client(tmp_path, tenant_hub_keys=((HUB, HUB_KEY), (OTHER_HUB, OTHER_KEY)))


def hub_call(api, cap="demo.echo@v1", key=HUB_KEY, payload=None):
    return api.post("/ai-market/v2/invoke", json={"capability_id": cap, "input": payload or {"n": 2}},
                    headers={"X-API-Key": key})


def operator_tenant(api, slug="ours", handler=CODE):
    body = deploy_payload(slug=slug, handler=handler)
    body["owner_pubkey"] = PLACEHOLDER
    assert api.post("/v1/tenants", json=body, headers=auth(api)).status_code == 200


def verify(api, block):
    key = api.get("/.well-known/ai-market.json").json()["signer_public_key"]
    sig = block["signature"]
    assert sig["public_key"] == key
    Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(
        base64.b64decode(sig["value"]), object_canonical(block).encode())


def set_billing(api, owner, hubs):
    raw = json.dumps({"hubs": hubs}).encode()
    return api.post("/v1/owners/me/billing", content=raw,
                    headers={**owner.headers("POST", "/v1/owners/me/billing", raw),
                             "content-type": "application/json"})


def test_an_unknown_hub_key_is_refused(tmp_path):
    api = client(tmp_path)
    operator_tenant(api)
    assert hub_call(api, key="aimk_nobody").status_code == 401


def test_a_hub_sells_the_operators_tenant_and_the_hearth_signs_the_bill(tmp_path):
    api = client(tmp_path)
    operator_tenant(api)
    res = hub_call(api)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["result"]["doubled"] == 4
    block = body["hub_billing"]
    assert block["hub"] == HUB and block["bill_to_account"] == "" and block["price_usd"] == 0.001
    verify(api, block)                                   # the key the hub pinned


def test_an_owner_who_did_not_choose_the_hub_is_not_sold(tmp_path):
    api = client(tmp_path)
    alice = Owner()
    admit(api, alice)
    assert alice.deploy(api, "alice-agent", handler=CODE).status_code == 200
    res = hub_call(api)
    assert res.status_code == 402                        # the hub charges its buyer nothing
    assert "has not chosen" in res.json()["detail"]


def test_an_owner_chooses_a_hub_and_is_billed_to_its_account(tmp_path):
    api = client(tmp_path)
    alice = Owner()
    admit(api, alice)
    alice.deploy(api, "alice-agent", handler=CODE)
    assert set_billing(api, alice, {HUB: ACCOUNT}).status_code == 200
    body = hub_call(api).json()
    assert body["hub_billing"]["bill_to_account"] == ACCOUNT
    assert body["hub_billing"]["owner_pubkey"] == alice.pub
    verify(api, body["hub_billing"])
    assert hub_call(api, key=OTHER_KEY).status_code == 402        # only the hub it chose
    statement = api.get("/v1/owners/me/statement",
                        headers=alice.headers("GET", "/v1/owners/me/statement")).json()
    assert statement["totals"] == [{"hub": HUB, "owner_pubkey": alice.pub,
                                    "bill_to_account": ACCOUNT, "calls": 1, "billed_usd": 0.001}]


def test_withdrawing_the_choice_stops_the_hub(tmp_path):
    api = client(tmp_path)
    alice = Owner()
    admit(api, alice)
    alice.deploy(api, "alice-agent", handler=CODE)
    set_billing(api, alice, {HUB: ACCOUNT})
    assert hub_call(api).status_code == 200
    set_billing(api, alice, {HUB: ""})
    assert hub_call(api).status_code == 402


def test_the_choice_is_checked(tmp_path):
    api = client(tmp_path)
    alice = Owner()
    admit(api, alice)
    assert set_billing(api, alice, {"https://unknown.test": ACCOUNT}).status_code == 400
    assert set_billing(api, alice, {HUB: "not-an-account"}).status_code == 400


def test_a_refused_or_failed_call_is_not_billed(tmp_path):
    api = client(tmp_path)
    failing = "def handle(payload):\n    raise ValueError('n required')\n"
    operator_tenant(api, handler=failing)
    assert hub_call(api).status_code == 400
    rows = api.get("/v1/admin/hub-billing", headers=auth(api)).json()
    assert rows["totals"] == [] and rows["recent"] == []


def test_one_owners_statement_is_its_own(tmp_path):
    api = client(tmp_path)
    alice, bob = Owner(), Owner()
    admit(api, alice)
    admit(api, bob)
    alice.deploy(api, "alice-agent", handler=CODE)
    set_billing(api, alice, {HUB: ACCOUNT})
    hub_call(api)
    mine = api.get("/v1/owners/me/statement", headers=bob.headers("GET", "/v1/owners/me/statement"))
    assert mine.json()["totals"] == []
    everyone = api.get("/v1/admin/hub-billing", headers=auth(api)).json()
    assert everyone["totals"][0]["calls"] == 1


def test_without_a_key_the_buyer_path_is_unchanged(tmp_path):
    api = client(tmp_path)
    operator_tenant(api)
    res = api.post("/ai-market/v2/invoke", json={"capability_id": "demo.echo@v1", "input": {"n": 3}})
    assert res.status_code == 200 and "hub_billing" not in res.json()     # payments off: free


def test_a_free_trial_through_a_hub_does_not_make_an_owner_work_for_nothing(tmp_path):
    api = client(tmp_path)
    alice = Owner()
    admit(api, alice)
    alice.deploy(api, "alice-agent", handler=CODE)
    set_billing(api, alice, {HUB: ACCOUNT})
    trial = {"X-API-Key": HUB_KEY, "X-AIMarket-Hub-Charged": "0.000000"}
    res = api.post("/ai-market/v2/invoke", json={"capability_id": "demo.echo@v1", "input": {"n": 1}},
                   headers=trial)
    assert res.status_code == 402
    paid = {"X-API-Key": HUB_KEY, "X-AIMarket-Hub-Charged": "0.001000"}
    res = api.post("/ai-market/v2/invoke", json={"capability_id": "demo.echo@v1", "input": {"n": 1}},
                   headers=paid)
    assert res.status_code == 200 and res.json()["hub_billing"]["bill_to_account"] == ACCOUNT


def test_the_operators_tenant_may_be_given_as_a_trial_but_is_not_billed(tmp_path):
    api = client(tmp_path)
    operator_tenant(api)
    res = api.post("/ai-market/v2/invoke", json={"capability_id": "demo.echo@v1", "input": {"n": 1}},
                   headers={"X-API-Key": HUB_KEY, "X-AIMarket-Hub-Charged": "0"})
    assert res.status_code == 200 and "hub_billing" not in res.json()
