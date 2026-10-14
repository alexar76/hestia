"""Names a hub shows buyers, and who may use them (hestia/names.py).

Each test is one way a stranger on an open hearth tried to pass an agent off as ours, take its
routed calls, or crowd the hearth — found by an independent review of the first, literal
family rule (2026-10-04) — and what the hearth does about it now.
"""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from hestia.app import build_app
from hestia.config import Settings
from hestia.listing import family_key, mixed_script_word, near, skeleton
from hestia.names import CLOSED, OPERATOR, holder_of
from hestia.owners import sign_owner_request
from tests.conftest import auth, client, deploy_payload
from tests.test_owners import HEARTH, PLACEHOLDER, Owner, admit

TREASURY = "0x1218ff36C5d2e3B6A565CdB1A8B1AcCFc606Ad0a"
MERKLE = "Merkle roots and proofs (RFC 6962 and OpenZeppelin)"
IDC = "Identifier check digits: IBAN, ISBN, GTIN, ISIN, LEI, EVM, Bitcoin"
HUB = "https://hub.example"
HUB_KEY = "k" * 40
B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def ours(slug="merkle-proof", cap_id="merkle.proof@v1", name=MERKLE, **cap):
    """Shaped like hestia_agents.manifests.deploy_body: placeholder key, our product."""
    p = deploy_payload(slug=slug)
    p["owner_pubkey"] = PLACEHOLDER
    p["payout_address"] = TREASURY
    p["capability"].update(capability_id=cap_id, name=name, product_id="hestia-agents",
                           publisher_id="aicom", provider_pubkey="",
                           description="Merkle roots and inclusion proofs.")
    p["capability"].update(cap)
    return p


def theirs(owner, slug, cap_id, name=None, payout="", **cap):
    p = deploy_payload(slug=slug)
    p["owner_pubkey"] = owner.pub
    p["payout_address"] = payout
    mark = owner.pub[:6].replace("+", "x").replace("/", "y")
    p["capability"].update(capability_id=cap_id, name=name or f"Agent {cap_id}",
                           product_id=f"p-{mark}", publisher_id=f"pub-{mark}",
                           provider_pubkey="")
    p["capability"].update(cap)
    return p


def op(api, payload):
    return api.post("/v1/tenants", json=payload, headers=auth(api))


def signed(api, owner, payload, path="/v1/tenants"):
    raw = json.dumps(payload).encode()
    headers = sign_owner_request(owner.key, hearth=HEARTH, method="POST", path=path, body=raw)
    return api.post(path, content=raw, headers={**headers, "content-type": "application/json"})


def tools(api):
    return {t["capability_id"]: t for t in api.get("/ai-market/v2/manifest").json()["tools"]}


def routed(api, cap_id, **headers):
    return api.post("/ai-market/v2/invoke", json={"capability_id": cap_id, "input": {"probe": 1}},
                    headers=headers)


def open_hearth(tmp_path, **kw):
    kw.setdefault("max_tenants", 32)
    return client(tmp_path, open_owners=True, **kw)


def respell(canonical: str) -> str:
    """Another base64 spelling of the same 32 bytes (non-zero padding bits)."""
    i = B64.index(canonical[-2])
    return canonical[:-2] + B64[(i & ~3) | ((i + 1) & 3)] + "="


# ------------------------------------------------------------------ folding


def test_one_family_however_it_is_spelled() -> None:
    ours_key = family_key("merkle.proof@v1")
    for cap_id in ("merkle-proof@v2", "merkle_proof@v1.1", "merkleproof@v1+rfc6962",
                   "merkle.proof.v2@v1", "merkle.proof-@v1", "merkle.proof.@v2",
                   "Merkle.Pr0of@v1", "MERKLE.PROOF@V1", "merkle-proof2@v1", "rnerkle.proof@v1"):
        assert family_key(cap_id) == ours_key, cap_id
    for cap_id in ("merkle.tree@v1", "proof.merkle@v1", "merkle@v1"):
        assert family_key(cap_id) != ours_key, cap_id
    assert family_key("hestia-host-deploy@v2").startswith(CLOSED)
    assert family_key("x402.authorization.check@v1") != family_key("x.authorization.check@v1")


@pytest.mark.parametrize("variant", [
    MERKLE.replace("roots", "rօots"),        # Armenian oh
    MERKLE.replace("and", "ɑnd"),            # Latin alpha
    MERKLE.replace("roots", "rooтs"),        # Cyrillic te
    MERKLE.replace("Merkle", "Merkӏe"),      # Cyrillic palochka for l
    MERKLE.replace("Merkle", "Merk1e"),           # digit one for l
    MERKLE.replace("roots", "rοots"),        # Greek omicron
    MERKLE.replace("roots", "róots"),        # an accent
    MERKLE.replace(" (", ": ").replace(")", ""),  # punctuation only
    "Ｍerkle roots and proofs (RFC 6962 and OpenZeppelin)",   # fullwidth M
])
def test_look_alike_names_share_a_skeleton(variant) -> None:
    assert skeleton(variant) == skeleton(MERKLE)


@pytest.mark.parametrize("variant", [
    IDC.replace("Identifier", "Іdentifier"),   # Cyrillic I
    IDC.replace("Identifier", "Ιdentifier"),   # Greek Iota
    IDC.replace("Identifier", "ldentifier"),        # lowercase L
    IDC.replace("Identifier", "1dentifier"),        # digit one
    IDC.replace("Identifier", "|dentifier"),        # vertical bar
])
def test_every_i_shaped_letter_is_one_class(variant) -> None:
    assert skeleton(variant) == skeleton(IDC)


def test_a_word_mixing_scripts_is_named() -> None:
    assert mixed_script_word("Pay with Bitсoin") == "Bitсoin"
    assert mixed_script_word("Проверка IBAN и ISIN") == ""
    assert mixed_script_word("使用JSON格式") == ""
    assert near("merkleproofs", "merkleproof") and near("fastmerkleproof", "merkleproof")
    assert not near("money", "moneytransfer") and not near("statstest", "weathernow")


# ------------------------------------------------------------------ in another owner's family


def test_a_look_alike_family_is_refused_not_listed(tmp_path) -> None:
    api = open_hearth(tmp_path)
    assert op(api, ours()).status_code == 200
    for i, cap_id in enumerate(("merkle-proof@v1", "merkle_proof@v1", "merkle-proof@v2",
                                "merkle_proof@v1.1", "merkleproof@v1+rfc6962",
                                "merkle.proof.v2@v1", "merkle.proof-@v1", "merkle.proof.@v2",
                                "Merkle.Pr0of@v1")):
        stranger = Owner()
        res = signed(api, stranger, theirs(stranger, f"mp{i}-x", cap_id))
        assert res.status_code == 409 and "another owner" in res.text, (cap_id, res.text)
    near_one = Owner()
    res = signed(api, near_one, theirs(near_one, "near-x", "merkle.proofs@v1"))
    assert res.status_code == 200 and res.json()["listed"] is False
    assert "merkle.proofs@v1" not in tools(api)


def test_the_hearths_namespace_is_closed_however_it_is_spelled(tmp_path) -> None:
    api = open_hearth(tmp_path, compute_enabled=True, compute_hub_keys=("c" * 32,))
    for i, cap_id in enumerate(("hestia@v1", "hestia-host-deploy@v2", "hestia_host_deploy@v1",
                                "hestia-compute-run@v1", "hestiahost.deploy@v1",
                                "Hestia-Hearth-List@v1", "hestia_compute_run@v1",
                                "HESTlA.x@v1")):
        stranger = Owner()
        res = signed(api, stranger, theirs(stranger, f"ns{i}-x", cap_id))
        assert res.status_code == 409 and "namespace" in res.text, (cap_id, res.text)
    assert op(api, ours(slug="op-ns", cap_id="hestia-tools@v1")).status_code == 409


def test_a_clone_cannot_borrow_our_product_publisher_or_payout(tmp_path) -> None:
    api = open_hearth(tmp_path, tenant_hub_keys=((HUB, HUB_KEY),))
    assert op(api, ours()).status_code == 200
    cases = {
        "our product": dict(product_id="hestia-agents"),
        "a product under our prefix": dict(product_id="hestia_agents_plus"),
        "our publisher": dict(publisher_id="aicom"),
        "a publisher under a reserved prefix": dict(publisher_id="ModelMarket Labs"),
        "our treasury as publisher": dict(publisher_id=TREASURY.lower()),
    }
    for i, (label, fields) in enumerate(cases.items()):
        stranger = Owner()
        res = signed(api, stranger, theirs(stranger, f"cl{i}-x", f"clone{i}.tool@v1", **fields))
        assert res.status_code == 409, (label, res.text)
    stranger = Owner()
    res = signed(api, stranger, theirs(stranger, "cl-pay", "clone.pay@v1", payout=TREASURY))
    assert res.status_code == 409 and "publisher" in res.text
    # Its own names are its own, and stay so.
    res = signed(api, stranger, theirs(stranger, "cl-own", "clone.own@v1", payout="0x" + "5" * 40))
    assert res.status_code == 200
    other = Owner()
    res = signed(api, other, theirs(other, "cl-own2", "other.own@v1", payout="0x" + "5" * 40))
    assert res.status_code == 409 and "publisher" in res.text


# ------------------------------------------------------------------ for good


def test_a_rename_keeps_the_family(tmp_path) -> None:
    api = open_hearth(tmp_path)
    assert op(api, ours()).status_code == 200
    assert op(api, ours(cap_id="merkle.tree@v1", name="Merkle trees")).status_code == 200
    stranger = Owner()
    res = signed(api, stranger, theirs(stranger, "merkle-x", "merkle.proof@v1"))
    assert res.status_code == 409 and "another owner" in res.text
    assert routed(api, "merkle.proof@v1").status_code == 404
    # The holder itself comes back to the name whenever it likes.
    assert op(api, ours(slug="merkle-proof-2")).status_code == 200


def test_a_stopped_agents_name_is_still_compared(tmp_path) -> None:
    api = open_hearth(tmp_path)
    assert op(api, ours()).status_code == 200
    assert api.post("/v1/tenants/merkle-proof/stop", headers=auth(api)).status_code == 200
    stranger = Owner()
    res = signed(api, stranger, theirs(stranger, "rfc-x", "rfc6962.tree@v1", name=MERKLE))
    assert res.status_code == 200 and res.json()["listed"] is False
    assert any("name looks like" in r for r in res.json()["listing_reasons"])
    assert op(api, ours()).status_code == 200
    assert "rfc6962.tree@v1" not in tools(api)


@pytest.mark.parametrize("name", [
    MERKLE.replace("roots", "rօots"), MERKLE.replace("and", "ɑnd"),
    MERKLE.replace("roots", "rooтs"), MERKLE.replace("Merkle", "Merkӏe"),
])
def test_a_homoglyph_name_is_held(tmp_path, name) -> None:
    api = open_hearth(tmp_path)
    assert op(api, ours()).status_code == 200
    stranger = Owner()
    res = signed(api, stranger, theirs(stranger, "hg-x", "rfc6962.tree@v1", name=name))
    assert res.status_code == 200 and res.json()["listed"] is False


# ------------------------------------------------------------------ processes and old ledgers


def test_two_hearth_processes_on_one_ledger_cannot_both_win_a_family(tmp_path) -> None:
    settings = Settings(**{**Settings.for_test(tmp_path).__dict__, "open_owners": True,
                           "max_tenants": 32})
    one, two = TestClient(build_app(settings)), TestClient(build_app(settings))
    for round_ in range(4):
        a, b = Owner(), Owner()
        gate = threading.Barrier(2)
        codes = {}

        def go(api, owner, tag):
            gate.wait()
            codes[tag] = signed(api, owner, theirs(owner, f"p{round_}{tag}-x",
                                                   f"proc{round_}.fam@v1")).status_code

        threads = [threading.Thread(target=go, args=(one, a, "a")),
                   threading.Thread(target=go, args=(two, b, "b"))]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(codes.values()) == [200, 409], codes
        holders = {n["holder"] for n in one.app.state.store.names("family")
                   if n["name"] == f"proc{round_}.fam"}
        assert len(holders) == 1


def test_one_worker_only(monkeypatch) -> None:
    import uvicorn

    import hestia.__main__ as entry

    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError, match="WEB_CONCURRENCY"):
        Settings.from_env()
    monkeypatch.delenv("WEB_CONCURRENCY")
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.update(kw))
    entry.main()
    assert seen["workers"] == 1


def test_a_ledger_written_before_the_rule_is_checked_at_boot(tmp_path) -> None:
    data = tmp_path / "data"
    settings = Settings(**{**Settings.for_test(data).__dict__, "open_owners": True,
                           "max_tenants": 32})
    api = TestClient(build_app(settings))
    assert op(api, ours()).status_code == 200
    stranger = Owner()
    assert signed(api, stranger, theirs(stranger, "aaa-merkle", "tmp.placeholder@v1")).status_code \
        == 200
    db = sqlite3.connect(data / "hestia.db")      # what an unruled ledger could hold
    row = db.execute("SELECT capability_json FROM tenants WHERE slug='aaa-merkle'").fetchone()
    cap = json.loads(row[0]) | {"capability_id": "merkle.proof@v1"}
    db.execute("UPDATE tenants SET capability_json=? WHERE slug='aaa-merkle'", (json.dumps(cap),))
    db.execute("DELETE FROM hearth_names")
    db.commit()
    db.close()
    api = TestClient(build_app(settings))
    store = api.app.state.store
    assert store.get("aaa-merkle").status == "stopped"
    assert "boot check" in store.get("aaa-merkle").last_error
    assert store.name_holder("family", family_key("merkle.proof@v1"))["holder"] == OPERATOR
    assert op(api, ours()).status_code == 200


# ------------------------------------------------------------------ the operator's levers


def test_reserved_prefixes_and_release(tmp_path) -> None:
    api = open_hearth(tmp_path, max_tenants=8)
    attested = Owner()
    admit(api, attested, label="Attested")
    reserve = api.post("/v1/admin/names", headers=auth(api),
                       json={"kind": "prefix", "name": "attested", "holder": attested.pub})
    assert reserve.status_code == 200 and reserve.json()["name"]["holder"] == attested.pub
    squatter = Owner()
    res = signed(api, squatter, theirs(squatter, "deal-x", "attested-deal@v1"))
    assert res.status_code == 409 and "reserved" in res.text
    res = signed(api, attested, theirs(attested, "attested-deal", "attested.deal@v1",
                                       name="Escrow on proof"))
    assert res.status_code == 200 and res.json()["listed"] is True
    # A squatter's family: the operator stops its agent, releases the name, takes it.
    assert signed(api, squatter, theirs(squatter, "gaia-x", "gaia.weather@v1")).status_code == 200
    release = {"kind": "family", "name": "gaia.weather"}
    busy = api.post("/v1/admin/names/release", headers=auth(api), json=release)
    assert busy.status_code == 409 and "gaia-x" in busy.text
    assert api.post("/v1/tenants/gaia-x/stop", headers=auth(api)).status_code == 200
    assert api.post("/v1/admin/names/release", headers=auth(api), json=release).status_code == 200
    assert op(api, ours(slug="gaia-weather", cap_id="gaia.weather@v1", name="Weather")) \
        .status_code == 200
    assert signed(api, squatter, theirs(squatter, "gaia-x", "gaia.weather@v1")).status_code == 409
    listing = api.get("/v1/admin/names", headers=auth(api)).json()["names"]
    assert any(n["kind"] == "prefix" and n["name"] == "hestia" and n["holder"] == OPERATOR
               for n in listing)
    assert api.get("/v1/admin/names").status_code == 401
    assert api.post("/v1/admin/names", headers=auth(api),
                    json={"kind": "family", "name": "hestia.x"}).status_code == 400


def test_stopped_agents_free_their_place(tmp_path) -> None:
    api = open_hearth(tmp_path, max_tenants=4)
    assert op(api, ours()).status_code == 200
    strangers = []
    for i in range(3):
        s = Owner()
        strangers.append(s)
        assert signed(api, s, theirs(s, f"sq{i}-x", f"squat{i}.thing@v1")).status_code == 200
    assert op(api, ours(slug="new-agent", cap_id="new.agent@v1", name="New")).status_code == 429
    for i in range(3):
        assert api.post(f"/v1/tenants/sq{i}-x/stop", headers=auth(api)).status_code == 200
    assert op(api, ours(slug="new-agent", cap_id="new.agent@v1", name="New")).status_code == 200
    # A stopped row redeployed takes its place back, and is counted again.
    assert signed(api, strangers[0], theirs(strangers[0], "sq0-x", "squat0.thing@v1")) \
        .status_code == 200
    assert signed(api, strangers[1], theirs(strangers[1], "sq1-x", "squat1.thing@v1")) \
        .status_code == 200
    assert signed(api, strangers[2], theirs(strangers[2], "sq2-x", "squat2.thing@v1")) \
        .status_code == 429


def test_a_suspended_owners_agents_are_off(tmp_path) -> None:
    api = client(tmp_path, max_tenants=32, tenant_hub_keys=((HUB, HUB_KEY),))
    owner = Owner()
    admit(api, owner, label="someone")
    assert signed(api, owner, theirs(owner, "deal-one", "some.deal@v1")).status_code == 200
    assert "some.deal@v1" in tools(api)
    admit(api, owner, label="someone", status="suspended")
    assert "some.deal@v1" not in tools(api)
    assert routed(api, "some.deal@v1").status_code == 404
    assert routed(api, "some.deal@v1", **{"X-API-Key": HUB_KEY}).status_code == 404
    assert api.post("/t/deal-one/invoke", json={"x": 1}).status_code == 403
    assert "deal-one" not in {t["slug"] for t in api.get("/v1/hearth").json()["tenants"]}
    admit(api, owner, label="someone", status="active")
    assert api.post("/t/deal-one/invoke", json={"x": 1}).status_code == 200


def test_the_operators_hold_survives_redeploys(tmp_path) -> None:
    api = open_hearth(tmp_path)
    owner = Owner()
    body = theirs(owner, "spammy-x", "spam.agent@v1")
    assert signed(api, owner, body).json()["listed"] is True
    held = api.post("/v1/admin/tenants/spammy-x/listing",
                    json={"listed": False, "reason": "impersonation"}, headers=auth(api))
    assert held.status_code == 200
    again = signed(api, owner, body)
    assert again.status_code == 200 and again.json()["listed"] is False
    assert "spam.agent@v1" not in tools(api)
    # An operator redeploy ("redeploy everything") does not lift it either; the listing call does.
    assert op(api, body).json()["listed"] is False
    assert api.post("/v1/admin/tenants/spammy-x/listing", json={"listed": True},
                    headers=auth(api)).status_code == 200
    assert "spam.agent@v1" in tools(api)


def test_an_operator_redeploy_does_not_list_a_held_agent(tmp_path) -> None:
    api = open_hearth(tmp_path)
    assert op(api, ours()).status_code == 200
    owner = Owner()
    body = theirs(owner, "copy-x", "rfc6962.tree@v1", name=MERKLE)
    assert signed(api, owner, body).json()["listed"] is False
    assert op(api, body).json()["listed"] is False
    assert "rfc6962.tree@v1" not in tools(api)


def test_the_operator_writes_an_owner_key_in_its_canonical_spelling(tmp_path) -> None:
    for tag, spell in (("padding-bits", respell), ("newline", lambda k: k + "\n")):
        api = client(tmp_path / tag, max_tenants=32)
        alice = Owner()
        admit(api, alice, label="Alice")
        body = theirs(alice, "alice-a", "alice.tool@v1")
        body["owner_pubkey"] = spell(alice.pub)
        assert spell(alice.pub) != alice.pub
        assert op(api, body).status_code == 200
        res = signed(api, alice, theirs(alice, "alice-b", "alice.tool@v2"))
        assert res.status_code == 200, (tag, res.text)
        roster = {t["slug"]: t["owner"] for t in api.get("/v1/hearth").json()["tenants"]}
        assert roster["alice-a"] == {"kind": "owner", "label": "Alice"}
        assert holder_of(api.app.state.store.get("alice-a").owner_pubkey) == alice.pub


def test_a_held_agent_answers_only_at_its_own_door(tmp_path) -> None:
    api = open_hearth(tmp_path)
    assert op(api, ours()).status_code == 200
    owner = Owner()
    assert signed(api, owner, theirs(owner, "near-x", "merkle.proofs@v1")).json()["listed"] is False
    assert routed(api, "merkle.proofs@v1").status_code == 404
    assert api.post("/t/near-x/invoke", json={"probe": 1}).status_code == 200


def test_ids_differing_by_case_or_a_newline_are_one_agent(tmp_path) -> None:
    api = open_hearth(tmp_path)
    owner = Owner()
    assert signed(api, owner, theirs(owner, "tw0-x", "zz.top@v1")).status_code == 200
    assert signed(api, owner, theirs(owner, "tw1-x", "zz.top@v1\n")).status_code == 400
    twin = signed(api, owner, theirs(owner, "tw2-x", "zz.top@V1"))
    assert twin.status_code == 409 and "already served" in twin.text
    assert signed(api, owner, theirs(owner, "tw3-x", "zz.top@v2")).status_code == 200


def test_an_owner_cannot_take_a_compute_slug(tmp_path) -> None:
    api = open_hearth(tmp_path, compute_functions=("fn-score",))
    owner = Owner()
    res = signed(api, owner, theirs(owner, "fn-score", "score.fn@v1"))
    assert res.status_code == 403 and "compute" in res.text


def test_open_signup_cannot_squat_the_brands_reserved_at_boot(tmp_path) -> None:
    api = open_hearth(tmp_path)
    for i, fields in enumerate((dict(cap_id="aicom.tools@v1"), dict(cap_id="modelmarket-x@v1"),
                                dict(cap_id="fine.tool@v1", product_id="AIMarket Pro"))):
        owner = Owner()
        cap_id = fields.pop("cap_id")
        res = signed(api, owner, theirs(owner, f"br{i}-x", cap_id, **fields))
        assert res.status_code == 409 and "reserved" in res.text, res.text
