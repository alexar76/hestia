"""Owner domains (hestia/domains.py): a proven domain's word belongs to its owner.

Proofs are read over the network in production (DNS-over-HTTPS, an HTTPS file); here the two
readers are replaced, and the parsing and the address guard are tested on their own.
"""

from __future__ import annotations

import json

import httpx
import pytest

from hestia import domains
from hestia.owner_cli import main as owner_cli
from tests.conftest import auth, client
from tests.test_names import ours, signed, theirs, tools
from tests.test_owners import Owner, admit

# ------------------------------------------------------------------ which domains bind


@pytest.mark.parametrize(("raw", "canonical"), [
    ("AttestedMemory.NET.", "attestedmemory.net"),
    ("example.co.uk", "example.co.uk"),
    ("my-shop.io", "my-shop.io"),
    ("example.xn--p1ai", "example.xn--p1ai"),
])
def test_a_registrable_domain_binds(raw, canonical) -> None:
    assert domains.registrable(raw) == canonical


@pytest.mark.parametrize("raw", [
    "tools.example.com",        # a subdomain would hand out the word "tools"
    "user.github.io",           # a shared platform's subdomain
    "co.uk",                    # a public suffix itself
    "xn--80ak6aa92e.com",       # an IDN word reads as another script's letters
    "127.0.0.1", "1.2", "localhost", "https://example.com", "example.com:443",
    "exa_mple.com", "-x.com", "", "a" * 64 + ".com",
])
def test_anything_else_is_refused(raw) -> None:
    with pytest.raises(domains.DomainError):
        domains.registrable(raw)


# ------------------------------------------------------------------ reading the proofs


def doh(answers):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["name"] == "_hestia.example.com"
        assert request.url.params["type"] == "TXT"
        return httpx.Response(200, json={"Status": 0, "Answer": answers})
    return httpx.MockTransport(handler)


def test_txt_proofs_read_the_owner_keys() -> None:
    alice, bob = Owner(), Owner()
    answers = [
        {"name": "_hestia.example.com.", "type": 16, "data": f'"hestia-owner={alice.pub}"'},
        {"name": "_hestia.example.com.", "type": 16,
         "data": f'"hestia-owner={bob.pub[:20]}" "{bob.pub[20:]}"'},   # split strings
        {"name": "_hestia.example.com.", "type": 16, "data": '"v=spf1 -all"'},
        {"name": "_hestia.example.com.", "type": 16, "data": '"hestia-owner=not-a-key"'},
        {"name": "x", "type": 5, "data": "cname.example."},
    ]
    keys = domains.txt_proofs("example.com", doh_url="https://doh.test/dns-query",
                              transport=doh(answers))
    assert keys == {alice.pub, bob.pub}


def test_https_proofs_read_the_file_and_nothing_else() -> None:
    alice = Owner()

    def serve(status=200, body=None, headers=None):
        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "https://example.com/.well-known/hestia-owner.json"
            return httpx.Response(status, content=body, headers=headers or {})
        return httpx.MockTransport(handler)

    good = json.dumps({"owner_pubkeys": [alice.pub, "junk"]}).encode()
    assert domains.https_proofs("example.com", transport=serve(body=good)) == {alice.pub}
    moved = serve(302, b"", {"location": "http://127.0.0.1/.well-known/hestia-owner.json"})
    assert domains.https_proofs("example.com", transport=moved) == set()
    huge = json.dumps({"owner_pubkeys": [alice.pub], "pad": "x" * 20000}).encode()
    assert domains.https_proofs("example.com", transport=serve(body=huge)) == set()
    assert domains.https_proofs("example.com", transport=serve(body=b"<html>")) == set()


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "172.20.0.2", "192.168.1.1",
                                     "100.64.1.1", "169.254.169.254", "192.0.2.10", "::1",
                                     "fd00::1", "fe80::1", "224.0.0.1", "0.0.0.0"])
def test_a_domain_that_resolves_inward_is_not_fetched(monkeypatch, address) -> None:
    monkeypatch.setattr(domains, "resolve", lambda d: ["93.184.215.14", address])
    with pytest.raises(domains.DomainError, match="non-public"):
        domains.require_public("example.com")


def test_a_public_domain_passes_the_guard(monkeypatch) -> None:
    monkeypatch.setattr(domains, "resolve", lambda d: ["93.184.215.14", "2606:2800:21f:cb07::1"])
    domains.require_public("example.com")


def test_prove_names_what_to_publish(monkeypatch) -> None:
    alice = Owner()
    monkeypatch.setattr(domains, "txt_proofs", lambda d, doh_url: set())
    monkeypatch.setattr(domains, "https_proofs", lambda d: {alice.pub})
    assert domains.prove("example.com", alice.pub) == "https"
    monkeypatch.setattr(domains, "txt_proofs", lambda d, doh_url: {alice.pub})
    assert domains.prove("example.com", alice.pub) == "dns"
    monkeypatch.setattr(domains, "txt_proofs", lambda d, doh_url: set())
    monkeypatch.setattr(domains, "https_proofs", lambda d: set())
    with pytest.raises(domains.DomainError) as refused:
        domains.prove("example.com", alice.pub)
    assert f"hestia-owner={alice.pub}" in str(refused.value)
    assert "/.well-known/hestia-owner.json" in str(refused.value)


# ------------------------------------------------------------------ in the hearth


@pytest.fixture
def proofs(monkeypatch):
    """domain -> owner keys its proof names; both readers consult it."""
    published: dict[str, set[str]] = {}
    monkeypatch.setattr(domains, "txt_proofs", lambda d, doh_url: published.get(d, set()))
    monkeypatch.setattr(domains, "https_proofs", lambda d: set())
    return published


def bind(api, owner, domain, path="/v1/owners/me/domain"):
    raw = json.dumps({"domain": domain}).encode()
    return api.post(path, content=raw, headers={**owner.headers("POST", path, raw),
                                                "content-type": "application/json"})


def test_a_proven_domain_is_its_owners_namespace_zone_included(tmp_path, proofs) -> None:
    api = client(tmp_path, max_tenants=32, open_owners=True)
    att = Owner()
    admit(api, att, label="Attested Memory")
    proofs["attestedmemory.net"] = {att.pub}
    out = bind(api, att, "AttestedMemory.net")
    assert out.status_code == 200, out.text
    assert out.json()["namespaces"] == ["attestedmemory.net", "net.attestedmemory"]
    assert out.json()["proved_by"] == "dns"
    # Its own names under the domain: deployed and listed, with the domain beside them.
    own = signed(api, att, theirs(att, "am-deal", "attestedmemory.net.deal@v1", name="Deal"))
    assert own.status_code == 200 and own.json()["listed"] is True
    assert tools(api)["attestedmemory.net.deal@v1"]["publisher_domain"] == "attestedmemory.net"
    roster = {t["slug"]: t["owner"] for t in api.get("/v1/hearth").json()["tenants"]}
    assert roster["am-deal"]["domains"] == ["attestedmemory.net"]
    # Nobody else's, in either order, however written.
    for i, (cap_id, fields) in enumerate((
            ("attestedmemory.net.claims@v1", {}),
            ("net.attestedmemory.escrow@v1", {}),
            ("attestedmemory-net.escrow@v1", {}),
            ("Attested_Memory.Net.x@v2", {}),
            ("attestedrnemory.net.x@v1", {}),
            ("plain.tool@v1", {"product_id": "Attestedmemory.net Labs"}),
            ("plain.tool@v1", {"publisher_id": "attestedmemory.net"}))):
        stranger = Owner()
        res = signed(api, stranger, theirs(stranger, f"s{i}-x", cap_id, **fields))
        assert res.status_code == 409 and "attestedmemory.net" in res.text, (cap_id, res.text)
    # The bare word, or another zone, is not this owner's: attestedmemory.com may be someone else.
    # (Whether each is listed is the look-alike review's business among these strangers.)
    for i, cap_id in enumerate(("attestedmemory.deal@v1", "attestedmemory.com.deal@v1",
                                "com.attestedmemory.deal@v1")):
        stranger = Owner()
        res = signed(api, stranger, theirs(stranger, f"free{i}-x", cap_id))
        assert res.status_code == 200, (cap_id, res.text)
    assert "attestedmemory.deal@v1" in tools(api)
    # Only the same letters run on: held for review as a look-alike, not refused.
    stranger = Owner()
    near = signed(api, stranger, theirs(stranger, "near-x", "attestedmemorynetwork.tool@v1"))
    assert near.status_code == 200 and near.json()["listed"] is False


def test_one_domain_one_owner_other_zones_are_others_and_incumbents_first(tmp_path, proofs) -> None:
    api = client(tmp_path, max_tenants=32)
    assert api.post("/v1/tenants", json=ours(), headers=auth(api)).status_code == 200
    first, second, third = Owner(), Owner(), Owner()
    for owner in (first, second, third):
        admit(api, owner)
    proofs.update({"attestedmemory.net": {first.pub}, "attestedrnemory.net": {second.pub},
                   "attestedmemory.com": {second.pub}, "merkle.xyz": {second.pub},
                   "modelmarketlabs.com": {second.pub}, "taken.org": {third.pub}})
    assert bind(api, first, "attestedmemory.net").status_code == 200
    again = bind(api, first, "attestedmemory.net")                       # idempotent
    assert again.status_code == 200 and again.json()["domains"] == ["attestedmemory.net"]
    alike = bind(api, second, "attestedrnemory.net")                     # folds to the same
    assert alike.status_code == 409 and "attestedmemory.net" in alike.text
    other_zone = bind(api, second, "attestedmemory.com")                 # another owner's zone
    assert other_zone.status_code == 200, other_zone.text
    assert bind(api, second, "merkle.xyz").status_code == 200            # takes merkle.xyz.*,
    mine = signed(api, first, theirs(first, "m-x", "merkle.tree@v1"))    # not the word merkle
    assert mine.status_code == 200
    reserved = bind(api, second, "modelmarketlabs.com")
    assert reserved.status_code == 409 and "reserved" in reserved.text
    # A name someone already uses under a domain keeps it from a later proof of that domain.
    assert signed(api, first, theirs(first, "t-x", "taken.org.tool@v1")).status_code == 200
    late = bind(api, third, "taken.org")
    assert late.status_code == 409 and "taken.org.tool" in late.text
    # The operator frees a domain; another owner may then prove it.
    free = api.post("/v1/admin/names/release", headers=auth(api),
                    json={"kind": "domain", "name": "attestedmemory.net"})
    assert free.status_code == 200
    proofs["attestedmemory.net"] = {third.pub}
    assert bind(api, third, "attestedmemory.net").status_code == 200


def test_no_proof_no_namespace(tmp_path, proofs) -> None:
    api = client(tmp_path, max_tenants=32, open_owners=True)
    owner = Owner()
    admit(api, owner)
    refused = bind(api, owner, "example.com")
    assert refused.status_code == 403 and f"hestia-owner={owner.pub}" in refused.text
    proofs["example.com"] = {Owner().pub}                    # someone else's key
    assert bind(api, owner, "example.com").status_code == 403
    assert bind(api, owner, "shop.example.com").status_code == 400
    newcomer = Owner()                                       # open hearth, never deployed
    proofs["newcomer.org"] = {newcomer.pub}
    assert bind(api, newcomer, "newcomer.org").status_code == 403
    names = api.get("/v1/admin/names", headers=auth(api)).json()["names"]
    assert not [n for n in names if n["kind"] == "domain"]


def test_the_operator_runs_the_proof_but_cannot_skip_it(tmp_path, proofs) -> None:
    api = client(tmp_path, max_tenants=32)
    owner = Owner()
    admit(api, owner, label="Example")
    url = "/v1/admin/owners/domain"
    assert api.post(url, headers=auth(api),
                    json={"pubkey": owner.pub, "domain": "example.com"}).status_code == 403
    proofs["example.com"] = {owner.pub}
    ok = api.post(url, headers=auth(api), json={"pubkey": owner.pub, "domain": "example.com"})
    assert ok.status_code == 200 and ok.json()["namespaces"] == ["example.com", "com.example"]
    assert api.post(url, headers=auth(api),
                    json={"pubkey": Owner().pub, "domain": "example.com"}).status_code == 404
    assert api.post(url, json={"pubkey": owner.pub, "domain": "example.com"}).status_code == 401
    assert api.post("/v1/admin/names", headers=auth(api),
                    json={"kind": "domain", "name": "other.com"}).status_code == 400


def test_a_name_held_before_a_reservation_stays_its_holders(tmp_path) -> None:
    api = client(tmp_path, max_tenants=32)
    alice, bob = Owner(), Owner()
    admit(api, alice)
    admit(api, bob)
    assert signed(api, alice, theirs(alice, "alice-a", "alice.tool@v1")).status_code == 200
    reserve = api.post("/v1/admin/names", headers=auth(api),
                       json={"kind": "prefix", "name": "alice", "holder": bob.pub})
    assert reserve.status_code == 200
    assert signed(api, alice, theirs(alice, "alice-a", "alice.tool@v2")).status_code == 200
    fresh = signed(api, alice, theirs(alice, "alice-b", "alice.other@v1"))
    assert fresh.status_code == 409 and "reserved" in fresh.text


def test_the_owner_tool_prints_the_proof(tmp_path, capsys) -> None:
    key = tmp_path / "owner.key"
    assert owner_cli(["keygen", "--out", str(key)]) == 0
    public = capsys.readouterr().out.strip()
    assert owner_cli(["proof", "--key", str(key), "--domain", "Example.COM"]) == 0
    printed = capsys.readouterr().out
    assert f'_hestia.example.com  "hestia-owner={public}"' in printed
    assert "https://example.com/.well-known/hestia-owner.json" in printed
    assert json.dumps({"owner_pubkeys": [public]}) in printed
    assert key.read_text().strip() not in printed
    assert owner_cli(["proof", "--key", str(key), "--domain", "a.b.example.com"]) == 1
