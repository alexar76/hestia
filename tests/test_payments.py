"""Paid invoke: the hearth verifies on chain and never holds the money.

The RPC is stubbed so these assert the settlement rules, not the network. What
they must pin down is the money behaviour: an unpaid call is refused, an
underpaid one is refused, a payment to the wrong address is refused, and one
payment buys exactly one call.
"""

from __future__ import annotations

import pytest

from hestia.config import Settings
from hestia.payments import PaymentError, PaymentTerms, to_units, verify_transfer
from tests.conftest import auth, client, deploy_payload

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BUYER = "0x6E94c380d908531f9822035d6cc4c8D2B0186C9c"
OWNER = "0x1218000000000000000000000000000000000000"
HANDLER = "def handle(payload):\n    return {'served': True}\n"


def _topic_addr(address: str) -> str:
    return "0x" + "0" * 24 + address[2:].lower()


def fake_rpc(monkeypatch, *, to: str, units: int, token: str = USDC,
             status: str = "0x1", block: int = 100, head: int = 101) -> dict:
    """Stand in for a Base node. Returns the call log so tests can inspect it."""
    seen: dict = {"receipts": 0}

    def _post(url, json=None, timeout=None):  # noqa: A002
        method = json["method"]

        class R:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                if method == "eth_blockNumber":
                    return {"result": hex(head)}
                seen["receipts"] += 1
                return {
                    "result": {
                        "status": status,
                        "blockNumber": hex(block),
                        "logs": [
                            {
                                "address": token,
                                "topics": [TRANSFER, _topic_addr(BUYER), _topic_addr(to)],
                                "data": hex(units),
                            }
                        ],
                    }
                }

        return R()

    monkeypatch.setattr("hestia.payments.httpx.post", _post)
    return seen


def paid_settings(tmp_path) -> Settings:
    base = Settings.for_test(tmp_path)
    return Settings(
        **{
            **base.__dict__,
            "payments_enabled": True,
            "payment_rpc_url": "http://rpc.invalid",
            "payment_min_confirmations": 1,
        }
    )


def deploy_priced(api, slug="paid", price=0.004, payout=OWNER):
    body = deploy_payload(slug, HANDLER)
    body["capability"]["capability_id"] = f"{slug}.thing@v1"
    body["capability"]["price_per_call_usd"] = price
    body["payout_address"] = payout
    assert api.post("/v1/tenants", json=body, headers=auth(api)).status_code == 200


# ------------------------------------------------------------------ units


def test_price_rounds_up_so_the_provider_never_eats_the_remainder() -> None:
    # $0.0015 is 1500 units exactly; a price that does not land on a unit
    # boundary must cost the buyer the extra unit, not the seller.
    assert to_units(0.0015, 6) == 1500
    assert to_units(0.0000015, 6) == 2
    assert to_units(0.004, 6) == 4000


# ------------------------------------------------------------ verification


def test_verify_rejects_a_transfer_to_someone_else(monkeypatch) -> None:
    fake_rpc(monkeypatch, to=BUYER, units=4000)  # paid to the wrong address
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    with pytest.raises(PaymentError, match="no USDC transfer"):
        verify_transfer(rpc_url="http://rpc.invalid", tx_hash="0x" + "a" * 64, terms=terms)


def test_verify_rejects_an_underpayment(monkeypatch) -> None:
    fake_rpc(monkeypatch, to=OWNER, units=3999)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    with pytest.raises(PaymentError, match="paid 3999 base units"):
        verify_transfer(rpc_url="http://rpc.invalid", tx_hash="0x" + "a" * 64, terms=terms)


def test_verify_rejects_a_reverted_transaction(monkeypatch) -> None:
    fake_rpc(monkeypatch, to=OWNER, units=4000, status="0x0")
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    with pytest.raises(PaymentError, match="reverted"):
        verify_transfer(rpc_url="http://rpc.invalid", tx_hash="0x" + "a" * 64, terms=terms)


def test_verify_waits_for_confirmations(monkeypatch) -> None:
    fake_rpc(monkeypatch, to=OWNER, units=4000, block=100, head=100)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 3)
    with pytest.raises(PaymentError, match="3 required"):
        verify_transfer(rpc_url="http://rpc.invalid", tx_hash="0x" + "a" * 64, terms=terms)


def test_verify_ignores_a_transfer_of_a_different_token(monkeypatch) -> None:
    fake_rpc(monkeypatch, to=OWNER, units=4000, token="0x" + "de" * 20)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    with pytest.raises(PaymentError, match="no USDC transfer"):
        verify_transfer(rpc_url="http://rpc.invalid", tx_hash="0x" + "a" * 64, terms=terms)


def test_verify_refuses_a_reference_that_is_not_a_tx_hash() -> None:
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    with pytest.raises(PaymentError, match="0x transaction hash"):
        verify_transfer(rpc_url="http://rpc.invalid", tx_hash="i-paid-honest", terms=terms)


# ------------------------------------------------------------------ edge


def test_a_free_tenant_is_still_free(tmp_path) -> None:
    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    body = deploy_payload("free", HANDLER)
    body["capability"]["price_per_call_usd"] = 0.0
    body["payout_address"] = OWNER
    api.post("/v1/tenants", json=body, headers=auth(api))
    assert api.post("/t/free/invoke", json={}).status_code == 200


def test_a_priced_tenant_without_a_payout_address_is_not_billed_to_nobody(tmp_path) -> None:
    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    body = deploy_payload("orphan", HANDLER)
    body["capability"]["price_per_call_usd"] = 0.004
    api.post("/v1/tenants", json=body, headers=auth(api))
    assert api.post("/t/orphan/invoke", json={}).status_code == 200


def test_an_unpaid_call_gets_x402_terms_naming_the_owner(tmp_path) -> None:
    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    deploy_priced(api)
    res = api.post("/t/paid/invoke", json={})
    assert res.status_code == 402
    body = res.json()
    assert body["pay_to"] == OWNER
    assert body["amount_units"] == "4000"
    assert body["accepts"][0]["scheme"] == "exact"
    assert body["accepts"][0]["payTo"] == OWNER
    assert body["accepts"][0]["asset"] == USDC


def test_health_stays_free_so_a_buyer_can_look_before_paying(tmp_path) -> None:
    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    deploy_priced(api)
    assert api.get("/t/paid/health").status_code == 200


def test_a_verified_payment_buys_the_call(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    deploy_priced(api)
    fake_rpc(monkeypatch, to=OWNER, units=4000)
    res = api.post("/t/paid/invoke", json={}, headers={"X-Payment": "0x" + "b" * 64})
    assert res.status_code == 200
    assert res.json()["result"] == {"served": True}


def test_one_payment_buys_exactly_one_call(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    deploy_priced(api)
    fake_rpc(monkeypatch, to=OWNER, units=4000)
    tx = {"X-Payment": "0x" + "c" * 64}
    assert api.post("/t/paid/invoke", json={}, headers=tx).status_code == 200
    second = api.post("/t/paid/invoke", json={}, headers=tx)
    assert second.status_code == 402
    assert "already been spent" in second.json()["detail"]


def test_the_routed_hub_door_charges_the_same(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    deploy_priced(api)
    unpaid = api.post(
        "/ai-market/v2/invoke", json={"capability_id": "paid.thing@v1", "input": {}}
    )
    assert unpaid.status_code == 402
    fake_rpc(monkeypatch, to=OWNER, units=4000)
    paid = api.post(
        "/ai-market/v2/invoke",
        json={"capability_id": "paid.thing@v1", "input": {}},
        headers={"X-Payment": "0x" + "d" * 64},
    )
    assert paid.status_code == 200


def test_an_unreachable_chain_refuses_rather_than_serving_free_work(tmp_path, monkeypatch) -> None:
    import httpx

    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    deploy_priced(api)

    def _boom(*_a, **_k):
        raise httpx.ConnectError("no route to node")

    monkeypatch.setattr("hestia.payments.httpx.post", _boom)
    res = api.post("/t/paid/invoke", json={}, headers={"X-Payment": "0x" + "e" * 64})
    assert res.status_code == 503


def test_payments_on_without_an_rpc_refuses_to_start(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_PAYMENTS_ENABLED", "1")
    monkeypatch.delenv("HESTIA_PAYMENT_RPC_URL", raising=False)
    with pytest.raises(RuntimeError, match="HESTIA_PAYMENT_RPC_URL"):
        Settings.from_env()


def test_a_bad_payout_address_is_refused_at_deploy(tmp_path) -> None:
    api = client(tmp_path)
    body = deploy_payload("bad", HANDLER)
    body["payout_address"] = "not-an-address"
    res = api.post("/v1/tenants", json=body, headers=auth(api))
    assert res.status_code == 400
    assert "payout_address" in res.text


def test_the_payout_address_is_published_so_a_buyer_can_check_it(tmp_path) -> None:
    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    deploy_priced(api)
    roster = api.get("/v1/hearth").json()["tenants"]
    assert roster[0]["payout_address"] == OWNER


def test_verification_fails_over_to_the_next_endpoint(monkeypatch) -> None:
    """One provider refusing a method must not break every paid call.

    base-rpc.publicnode.com answers eth_blockNumber but 403s
    eth_getTransactionReceipt, which took the live hearth's whole money path
    down while looking like an unreachable chain.
    """
    import httpx as real_httpx

    calls: list[str] = []

    def _post(url, json=None, timeout=None):  # noqa: A002
        calls.append(url)
        method = json["method"]

        class R:
            @staticmethod
            def raise_for_status() -> None:
                if url == "http://broken" and method == "eth_getTransactionReceipt":
                    raise real_httpx.HTTPStatusError("403", request=None, response=None)

            @staticmethod
            def json() -> dict:
                if method == "eth_blockNumber":
                    return {"result": hex(101)}
                return {
                    "result": {
                        "status": "0x1",
                        "blockNumber": hex(100),
                        "logs": [
                            {
                                "address": USDC,
                                "topics": [TRANSFER, _topic_addr(BUYER), _topic_addr(OWNER)],
                                "data": hex(4000),
                            }
                        ],
                    }
                }

        return R()

    monkeypatch.setattr("hestia.payments.httpx.post", _post)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    settled = verify_transfer(
        rpc_url="http://broken, http://working", tx_hash="0x" + "a" * 64, terms=terms
    )
    assert settled["paid_units"] == 4000
    assert calls[0] == "http://broken" and "http://working" in calls


def test_a_single_broken_endpoint_still_reports_the_transport_failure(monkeypatch) -> None:
    import httpx as real_httpx

    def _boom(*_a, **_k):
        raise real_httpx.ConnectError("down")

    monkeypatch.setattr("hestia.payments.httpx.post", _boom)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    with pytest.raises(real_httpx.HTTPError):
        verify_transfer(rpc_url="http://only", tx_hash="0x" + "a" * 64, terms=terms)


def test_payment_is_released_when_the_tenant_is_unreachable(tmp_path, monkeypatch) -> None:
    """A wedged or crashed tenant is the platform's fault. The claimed payment
    is released so the buyer can retry the same transaction — otherwise an
    honest caller pays and gets a 502 with nothing to show for it."""
    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    deploy_priced(api)
    fake_rpc(monkeypatch, to=OWNER, units=4000)

    # Kill the tenant process so the edge cannot reach it.
    api.app.state.stub._stop_all()
    res = api.post("/t/paid/invoke", json={}, headers={"X-Payment": "0x" + "f" * 64})
    assert res.status_code == 502
    # The claim was released, so the ledger does not record the tx as spent.
    assert api.app.state.store.payment_count("paid") == 0


def test_a_timed_out_call_keeps_the_payment(tmp_path, monkeypatch) -> None:
    """A 504 is the caller's own slow input, served to its budget — it is billed.
    Only a transport failure (the platform) releases the payment."""
    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    slow = (
        "import re\n"
        "def handle(payload):\n"
        "    return {'m': re.fullmatch('(a+)+$', 'a'*40+'!') is not None}\n"
    )
    body = deploy_payload("slow", slow)
    body["capability"]["capability_id"] = "slow.thing@v1"
    body["capability"]["price_per_call_usd"] = 0.004
    body["payout_address"] = OWNER
    api.post("/v1/tenants", json=body, headers=auth(api))
    monkeypatch.setenv("HESTIA_TENANT_CALL_TIMEOUT_S", "1")
    fake_rpc(monkeypatch, to=OWNER, units=4000)
    res = api.post("/t/slow/invoke", json={}, headers={"X-Payment": "0x" + "1" * 64})
    assert res.status_code == 504
    # The call ran to its budget; the payment stands (the caller supplied the input).
    assert api.app.state.store.payment_count("slow") == 1


def test_an_oversized_tenant_response_is_refused(tmp_path) -> None:
    """The edge caps what a tenant sends back, so an amplifying handler cannot
    exhaust the control plane even when its own memory cap would allow the
    allocation."""
    small = {**Settings.for_test(tmp_path).__dict__, "max_tenant_response_bytes": 4096}
    api = client(tmp_path, **small)
    big = "def handle(payload):\n    return {'x': 'a' * 200000}\n"
    api.post("/v1/tenants", json=deploy_payload("big", big), headers=auth(api))
    res = api.post("/t/big/invoke", json={})
    assert res.status_code == 502
    assert "too large" in res.json()["detail"]


def test_a_normal_response_is_under_the_cap(tmp_path) -> None:
    api = client(tmp_path)
    ok = "def handle(payload):\n    return {'ok': True}\n"
    api.post("/v1/tenants", json=deploy_payload("norm", ok), headers=auth(api))
    assert api.post("/t/norm/invoke", json={}).status_code == 200


# --------------------------------------------------- freshness / call binding


def fake_rpc_aged(monkeypatch, *, to: str, units: int, mined_ts: int,
                  block: int = 100, head: int = 101) -> None:
    """A node that also answers eth_getBlockByNumber, so freshness can be tested."""

    def _post(url, json=None, timeout=None):  # noqa: A002
        method = json["method"]

        class R:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                if method == "eth_blockNumber":
                    return {"result": hex(head)}
                if method == "eth_getBlockByNumber":
                    return {"result": {"timestamp": hex(mined_ts)}}
                return {
                    "result": {
                        "status": "0x1",
                        "blockNumber": hex(block),
                        "logs": [
                            {
                                "address": USDC,
                                "topics": [TRANSFER, _topic_addr(BUYER), _topic_addr(to)],
                                "data": hex(units),
                            }
                        ],
                    }
                }

        return R()

    monkeypatch.setattr("hestia.payments.httpx.post", _post)


def test_a_fresh_payment_within_the_window_is_accepted(monkeypatch) -> None:
    import time as _time

    fake_rpc_aged(monkeypatch, to=OWNER, units=4000, mined_ts=int(_time.time()) - 30)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    settled = verify_transfer(
        rpc_url="http://rpc", tx_hash="0x" + "a" * 64, terms=terms, max_age_s=3600
    )
    assert settled["paid_units"] == 4000


def test_a_stale_transfer_cannot_be_replayed_as_a_fresh_payment(monkeypatch) -> None:
    """An old, unrelated transfer to a shared payout address of at least the
    price must NOT buy a call once a freshness window is set."""
    import time as _time

    # Mined two days ago — a plausible unrelated treasury deposit.
    fake_rpc_aged(monkeypatch, to=OWNER, units=999999, mined_ts=int(_time.time()) - 172800)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    with pytest.raises(PaymentError, match="old"):
        verify_transfer(
            rpc_url="http://rpc", tx_hash="0x" + "b" * 64, terms=terms, max_age_s=3600
        )


def test_freshness_fails_closed_when_the_block_age_is_unreadable(monkeypatch) -> None:
    def _post(url, json=None, timeout=None):  # noqa: A002
        method = json["method"]

        class R:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                if method == "eth_blockNumber":
                    return {"result": hex(101)}
                if method == "eth_getBlockByNumber":
                    return {"result": None}  # node cannot serve the block
                return {
                    "result": {
                        "status": "0x1",
                        "blockNumber": hex(100),
                        "logs": [
                            {
                                "address": USDC,
                                "topics": [TRANSFER, _topic_addr(BUYER), _topic_addr(OWNER)],
                                "data": hex(4000),
                            }
                        ],
                    }
                }

        return R()

    monkeypatch.setattr("hestia.payments.httpx.post", _post)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    with pytest.raises(PaymentError, match="age"):
        verify_transfer(
            rpc_url="http://rpc", tx_hash="0x" + "c" * 64, terms=terms, max_age_s=3600
        )


def test_max_age_zero_keeps_the_old_behaviour(monkeypatch) -> None:
    # With the window off, no block lookup happens and an old tx still verifies.
    fake_rpc(monkeypatch, to=OWNER, units=4000)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    settled = verify_transfer(
        rpc_url="http://rpc", tx_hash="0x" + "d" * 64, terms=terms, max_age_s=0
    )
    assert settled["paid_units"] == 4000


def test_oversized_response_releases_a_claimed_payment(tmp_path, monkeypatch) -> None:
    """A provider that over-produces delivered nothing to the buyer, so the
    on-chain payment must not be spent on that undelivered call."""
    settings = {
        **paid_settings(tmp_path).__dict__,
        "max_tenant_response_bytes": 4096,
    }
    api = client(tmp_path, **settings)
    body = deploy_payload("amp", "def handle(payload):\n    return {'x': 'a' * 200000}\n")
    body["capability"]["capability_id"] = "amp.thing@v1"
    body["capability"]["price_per_call_usd"] = 0.004
    body["payout_address"] = OWNER
    assert api.post("/v1/tenants", json=body, headers=auth(api)).status_code == 200
    fake_rpc(monkeypatch, to=OWNER, units=4000)
    res = api.post("/t/amp/invoke", json={}, headers={"X-Payment": "0x" + "e" * 64})
    assert res.status_code == 502
    # The buyer got nothing, so the claim is released — the tx is not spent.
    assert api.app.state.store.payment_count("amp") == 0


# ------------------------------------------- EIP-3009 binding (payment ↔ call)

AUTH_USED = "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"


def bound_settings(tmp_path) -> dict:
    return {
        **paid_settings(tmp_path).__dict__,
        "payment_require_binding": True,
        "payment_invoice_ttl_s": 900,
    }


def fake_rpc_bound(monkeypatch, *, to: str, units: int, nonce: str | None,
                   token: str = USDC) -> None:
    """A node whose receipt carries a Transfer and, when `nonce` is given, the
    EIP-3009 AuthorizationUsed log the token contract emits for it."""
    def _post(url, json=None, timeout=None):  # noqa: A002
        method = json["method"]

        class R:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                if method == "eth_blockNumber":
                    return {"result": hex(101)}
                logs = [
                    {
                        "address": token,
                        "topics": [TRANSFER, _topic_addr(BUYER), _topic_addr(to)],
                        "data": hex(units),
                    }
                ]
                if nonce is not None:
                    logs.append(
                        {
                            "address": token,
                            "topics": [AUTH_USED, _topic_addr(BUYER), nonce],
                            "data": "0x",
                        }
                    )
                return {"result": {"status": "0x1", "blockNumber": hex(100), "logs": logs}}

        return R()

    monkeypatch.setattr("hestia.payments.httpx.post", _post)


def _quote(api, slug="paid") -> dict:
    """Take a 402 and read the invoice it mints."""
    res = api.post(f"/t/{slug}/invoke", json={})
    assert res.status_code == 402
    return res.json()


def test_the_402_mints_a_nonce_and_says_how_to_bind(tmp_path) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    body = _quote(api)
    assert body["binding"] == "eip3009"
    assert body["nonce"].startswith("0x") and len(body["nonce"]) == 66
    assert body["accepts"][0]["extra"]["nonce"] == body["nonce"]
    assert "transferWithAuthorization" in body["how"]


def test_two_quotes_mint_different_nonces(tmp_path) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    assert _quote(api)["nonce"] != _quote(api)["nonce"]


def test_an_unrelated_transfer_to_the_shared_payout_buys_nothing(tmp_path, monkeypatch) -> None:
    """THE attack this binding exists for. A real, confirmed, fresh, generous
    USDC transfer to the payout address — made for something else entirely, as
    happens constantly on a shared treasury — carries no authorization for this
    hearth's nonce, so it cannot buy a call."""
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    nonce = _quote(api)["nonce"]
    # A plain transfer: right token, right recipient, 250x the price, no auth log.
    fake_rpc_bound(monkeypatch, to=OWNER, units=1_000_000, nonce=None)
    res = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "a" * 64, "X-Payment-Nonce": nonce},
    )
    assert res.status_code == 402
    assert "not bound to this call" in res.json()["detail"]
    assert api.app.state.store.payment_count("paid") == 0


def test_a_bound_payment_is_served(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    nonce = _quote(api)["nonce"]
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=nonce)
    res = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "b" * 64, "X-Payment-Nonce": nonce},
    )
    assert res.status_code == 200, res.text
    assert res.json()["result"] == {"served": True}


def test_an_authorization_for_a_different_nonce_is_refused(tmp_path, monkeypatch) -> None:
    """A payment signed for some OTHER call (another invoice, another hearth)
    must not settle this one."""
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    mine = _quote(api)["nonce"]
    someone_elses = "0x" + "c" * 64
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=someone_elses)
    res = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "d" * 64, "X-Payment-Nonce": mine},
    )
    assert res.status_code == 402
    assert "not bound to this call" in res.json()["detail"]


def test_a_nonce_buys_exactly_one_call(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    nonce = _quote(api)["nonce"]
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=nonce)
    first = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "e" * 64, "X-Payment-Nonce": nonce},
    )
    assert first.status_code == 200
    # Same authorization, a different transaction hash: the nonce is spent.
    second = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "f" * 64, "X-Payment-Nonce": nonce},
    )
    assert second.status_code == 402
    assert "already been spent" in second.json()["detail"]


def test_a_missing_nonce_header_is_refused_with_instructions(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    _quote(api)
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=None)
    res = api.post("/t/paid/invoke", json={}, headers={"X-Payment": "0x" + "1" * 64})
    assert res.status_code == 402
    assert "X-Payment-Nonce" in res.json()["detail"]


def test_an_unknown_nonce_is_refused(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce="0x" + "9" * 64)
    res = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "2" * 64, "X-Payment-Nonce": "0x" + "9" * 64},
    )
    assert res.status_code == 402
    assert "unknown payment nonce" in res.json()["detail"]


def test_an_expired_nonce_is_refused(tmp_path, monkeypatch) -> None:
    settings = {**bound_settings(tmp_path), "payment_invoice_ttl_s": 0}
    api = client(tmp_path, **settings)
    deploy_priced(api)
    nonce = _quote(api)["nonce"]
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=nonce)
    res = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "3" * 64, "X-Payment-Nonce": nonce},
    )
    assert res.status_code == 402
    assert "expired" in res.json()["detail"]


def test_a_nonce_from_another_tenant_is_refused(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api, slug="paid")
    deploy_priced(api, slug="other", price=0.001)
    nonce = _quote(api, slug="other")["nonce"]
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=nonce)
    res = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "4" * 64, "X-Payment-Nonce": nonce},
    )
    assert res.status_code == 402
    assert "unknown payment nonce" in res.json()["detail"]


def test_a_platform_failure_gives_back_both_the_payment_and_the_nonce(
    tmp_path, monkeypatch
) -> None:
    """Releasing only the transaction would leave the buyer with a burned nonce
    and no way to retry the payment they already made."""
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    nonce = _quote(api)["nonce"]
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=nonce)
    api.app.state.stub._stop_all()  # tenant is gone: platform failure
    res = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "5" * 64, "X-Payment-Nonce": nonce},
    )
    assert res.status_code == 502
    assert api.app.state.store.payment_count("paid") == 0
    invoice = api.app.state.store.invoice(nonce)
    assert float(invoice["consumed_at"]) == 0, "the nonce must be reusable"
