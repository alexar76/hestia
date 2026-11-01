"""Tenant egress (hestia.egress): a sandboxed agent reaches the network only through the hearth,
only to the hosts its deploy named, only over HTTPS to a public address it was checked against,
within a per-agent budget. These tests are the escape attempts."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from hestia.egress import (
    MAX_RESPONSE_BYTES,
    Egress,
    EgressRefused,
    EgressRequest,
    RateLimit,
    normalize_host,
    normalize_hosts,
    request_key,
)
from tests.conftest import auth, deploy_payload
from tests.test_wasm_runtime import scripted_runner, wasm_client  # noqa: F401  (fixture)

PUBLIC = "93.184.215.14"
HOSTS = ["api.example.org"]


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ deploy-time hosts


@pytest.mark.parametrize("raw", ["api.example.org", "API.Example.ORG.", "mainnet.base.org"])
def test_public_names_are_accepted_and_normalized(raw):
    assert normalize_host(raw) == raw.lower().rstrip(".")


@pytest.mark.parametrize("raw", [
    "127.0.0.1", "[::1]", "169.254.169.254", "localhost", "metadata.google.internal",
    "db.internal", "printer.local", "*.example.org", "intranet", "api.example.org:8443",
    "evil..org", "-bad.example.org", "x" * 300 + ".org", "10.0.0.1.nip.io.0",
])
def test_private_odd_or_wildcard_hosts_are_refused(raw):
    with pytest.raises(ValueError):
        normalize_host(raw)


def test_at_most_eight_hosts_and_duplicates_collapse():
    assert normalize_hosts(["a.example.org", "A.example.org"]) == ["a.example.org"]
    with pytest.raises(ValueError, match="at most 8"):
        normalize_hosts([f"h{i}.example.org" for i in range(9)])


# ------------------------------------------------------------------ what a handler may ask for


@pytest.mark.parametrize("raw,reason", [
    ({"url": "http://api.example.org/x"}, "https"),
    ({"url": "https://other.example.org/x"}, "not in this agent"),
    ({"url": "https://api.example.org:8443/x"}, "port 443"),
    ({"url": "https://user:pw@api.example.org/x"}, "credentials"),
    ({"url": "https://api.example.org/x", "method": "PUT"}, "not allowed"),
    ({"url": "https://api.example.org/x", "body": "x"}, "GET carries no body"),
    ({"url": "https://api.example.org/x", "method": "POST", "body": "x" * 70_000}, "larger"),
    ({"url": "https://127.0.0.1/x"}, "not in this agent"),
    ("https://api.example.org/x", "object"),
])
def test_requests_outside_the_terms_are_refused(raw, reason):
    with pytest.raises(EgressRefused, match=reason):
        EgressRequest.parse(raw, HOSTS)


def test_only_whitelisted_request_headers_survive():
    req = EgressRequest.parse({"url": "https://api.example.org/v1?q=1#frag", "headers": {
        "Host": "internal", "Cookie": "s=1", "X-Forwarded-For": "10.0.0.1", "Authorization": "Bearer k",
        "Content-Type": "application/json", "X-Evil": "a\r\nInjected: 1"}}, HOSTS)
    assert req.headers == {"authorization": "Bearer k", "content-type": "application/json"}
    assert req.url == "https://api.example.org/v1?q=1"  # no fragment, normalized host


# ------------------------------------------------------------------ the fetch itself


def _egress(resolve, handler, per_minute=60):
    return Egress(per_minute=per_minute, resolve=resolve, transport=httpx.MockTransport(handler))


def test_a_name_that_resolves_to_a_private_address_is_refused():
    egress = _egress(lambda h: ["10.1.2.3"], lambda r: httpx.Response(200))
    with pytest.raises(EgressRefused, match="non-public"):
        run(egress.fetch("t", EgressRequest.parse({"url": "https://api.example.org/"}, HOSTS)))


def test_one_private_address_among_public_ones_refuses_the_name():
    """The DNS-rebinding shape: a public and a private answer for one name."""
    egress = _egress(lambda h: [PUBLIC, "127.0.0.1"], lambda r: httpx.Response(200))
    with pytest.raises(EgressRefused, match="non-public"):
        run(egress.fetch("t", EgressRequest.parse({"url": "https://api.example.org/"}, HOSTS)))


@pytest.mark.parametrize("address", [
    "::ffff:127.0.0.1",          # IPv4-mapped loopback
    "64:ff9b::7f00:1",           # NAT64 of 127.0.0.1 (Python calls 64:ff9b::/96 global)
    "64:ff9b::a9fe:a9fe",        # NAT64 of 169.254.169.254, the cloud metadata address
    "64:ff9b:1::a00:1",          # local-use NAT64 (RFC 8215)
    "2002:a00:1::1",             # 6to4 of 10.0.0.1
    "2002:7f00:1::1",            # 6to4 of 127.0.0.1
    "fd00::1", "fe80::1", "::1", "0.0.0.0", "100.64.0.1", "192.0.0.8", "198.18.0.1",
])
def test_addresses_that_lead_inside_are_not_public(address):
    egress = _egress(lambda h: [address], lambda r: httpx.Response(200))
    with pytest.raises(EgressRefused, match="non-public"):
        run(egress.fetch("t", EgressRequest.parse({"url": "https://api.example.org/"}, HOSTS)))


def test_nat64_of_a_public_address_is_allowed():
    egress = _egress(lambda h: ["64:ff9b::5db8:d70e"], lambda r: httpx.Response(200, text="ok"))
    out = run(egress.fetch("t", EgressRequest.parse({"url": "https://api.example.org/"}, HOSTS)))
    assert out["status"] == 200


def test_the_request_goes_to_the_checked_address_with_the_host_name_for_tls():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), host=request.headers["host"],
                    sni=request.extensions.get("sni_hostname"),
                    ua=request.headers["user-agent"])
        return httpx.Response(200, headers={"content-type": "application/json", "set-cookie": "s=1"},
                              text='{"ok": true}')

    egress = _egress(lambda h: [PUBLIC], handler)
    out = run(egress.fetch("t", EgressRequest.parse({"url": "https://api.example.org/v1?q=1"}, HOSTS)))
    assert seen == {"url": f"https://{PUBLIC}/v1?q=1", "host": "api.example.org",
                    "sni": "api.example.org", "ua": "hestia-egress/1"}
    assert out["status"] == 200 and out["text"] == '{"ok": true}'
    assert out["headers"]["content-type"] == "application/json"
    assert "set-cookie" not in out["headers"]  # a handler never sees cookies


def test_redirects_are_returned_not_followed():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    out = run(_egress(lambda h: [PUBLIC], handler).fetch(
        "t", EgressRequest.parse({"url": "https://api.example.org/"}, HOSTS)))
    assert out["status"] == 302 and len(calls) == 1
    assert out["headers"]["location"].startswith("http://169.254")  # visible, not fetched


def test_a_large_response_is_cut():
    big = b"x" * (MAX_RESPONSE_BYTES + 1)
    egress = _egress(lambda h: [PUBLIC], lambda r: httpx.Response(200, content=big))
    with pytest.raises(EgressRefused, match="larger"):
        run(egress.fetch("t", EgressRequest.parse({"url": "https://api.example.org/"}, HOSTS)))


def test_each_agent_has_its_own_budget_per_minute():
    limit = RateLimit(2)
    assert limit.take("a", 0) and limit.take("a", 1) and not limit.take("a", 2)
    assert limit.take("b", 2)            # another agent is not affected
    assert limit.take("a", 61)           # the window slides


def test_an_unreachable_host_is_an_answer_not_a_crash():
    def handler(request):
        raise httpx.ConnectError("refused")

    out = run(_egress(lambda h: [PUBLIC], handler).fetch(
        "t", EgressRequest.parse({"url": "https://api.example.org/"}, HOSTS)))
    assert out["status"] == 0 and "could not reach api.example.org" in out["error"]


def test_the_guest_and_the_hearth_agree_on_the_request_key():
    from hestia.sandbox import GUEST

    namespace: dict = {}
    exec(GUEST.split("def _main():")[0], namespace)  # the guest's helpers, without running _main
    raw = {"method": "post", "url": "https://api.example.org/x", "headers": {"Content-Type": "a/b"},
           "body": "{}"}
    assert namespace["_key"](raw["method"], raw["url"], {"content-type": "a/b"}, raw["body"]) == \
        request_key(raw)


# ------------------------------------------------------------------ end to end, scripted runner


class FakeEgress(Egress):
    def __init__(self):
        super().__init__(resolve=lambda h: [PUBLIC],
                         transport=httpx.MockTransport(lambda r: httpx.Response(200, text="gas=0.005")))


def _deploy(c, **extra):
    body = {**deploy_payload(handler="def handle(p):\n    return {}\n"), **extra}
    return c.post("/v1/tenants", json=body, headers=auth(c))


def test_egress_hosts_need_the_hearth_to_allow_egress(tmp_path, scripted_runner):
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path)
    out = _deploy(c, egress_hosts=HOSTS)
    assert out.status_code == 400 and "HESTIA_TENANT_EGRESS" in out.text


def test_a_bad_host_is_refused_at_deploy(tmp_path, scripted_runner):
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path, tenant_egress=True)
    out = _deploy(c, egress_hosts=["169.254.169.254"])
    assert out.status_code in (400, 422) and "IP address" in out.text


def test_a_handler_fetches_through_the_hearth_and_the_roster_shows_the_hosts(tmp_path, scripted_runner):
    socket_path, sandbox = scripted_runner
    c = wasm_client(tmp_path, socket_path, tenant_egress=True)
    c.app.state.stub.egress = FakeEgress()
    assert _deploy(c, egress_hosts=HOSTS).status_code == 200
    answer = c.post("/t/demo-echo/invoke", json={"mode": "fetch", "url": "https://api.example.org/gas"})
    assert answer.status_code == 200, answer.text
    assert answer.json()["result"] == {"status": 200, "text": "gas=0.005"}
    roster = c.get("/v1/hearth").json()["tenants"]
    assert roster[0]["egress_hosts"] == HOSTS


def test_a_host_outside_the_list_reaches_the_handler_as_a_refusal(tmp_path, scripted_runner):
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path, tenant_egress=True)
    c.app.state.stub.egress = FakeEgress()
    assert _deploy(c, egress_hosts=HOSTS).status_code == 200
    answer = c.post("/t/demo-echo/invoke", json={"mode": "fetch", "url": "https://evil.example.net/"})
    assert answer.status_code == 400 and "not in this agent" in answer.json()["error"]


def test_without_egress_hosts_a_fetch_is_refused(tmp_path, scripted_runner):
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path, tenant_egress=True)
    c.app.state.stub.egress = FakeEgress()
    assert _deploy(c).status_code == 200
    answer = c.post("/t/demo-echo/invoke", json={"mode": "fetch", "url": "https://api.example.org/"})
    assert answer.status_code == 400 and "not in this agent" in answer.json()["error"]


def test_a_redeploy_without_hosts_takes_the_network_away(tmp_path, scripted_runner):
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path, tenant_egress=True)
    assert _deploy(c, egress_hosts=HOSTS).status_code == 200
    assert c.app.state.stub.egress_hosts("demo-echo") == HOSTS
    assert _deploy(c).status_code == 200
    assert c.app.state.stub.egress_hosts("demo-echo") == []


def test_all_agents_together_have_a_hearth_budget():
    egress = Egress(per_minute=100, hearth_per_minute=2, resolve=lambda h: [PUBLIC],
                    transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok")))
    req = EgressRequest.parse({"url": "https://api.example.org/"}, HOSTS)
    run(egress.fetch("a", req))
    run(egress.fetch("b", req))
    with pytest.raises(EgressRefused, match="hearth's egress budget"):
        run(egress.fetch("c", req))
