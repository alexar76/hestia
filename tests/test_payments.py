"""Paid invoke: the hearth verifies on chain and never holds the money.

The RPC is stubbed so these assert the settlement rules, not the network. What
they must pin down is the money behaviour: an unpaid call is refused, an
underpaid one is refused, a payment to the wrong address is refused, one
payment buys exactly one call, and a payment is always the EIP-3009
authorization over the nonce a 402 minted — there is no unbound payment.
"""

from __future__ import annotations

import hashlib

import pytest

from hestia.config import Settings
from hestia.payments import PaymentError, PaymentTerms, to_units, verify_transfer
from tests.conftest import auth, client, deploy_payload

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
AUTH_USED = "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BUYER = "0x6E94c380d908531f9822035d6cc4c8D2B0186C9c"
NONCE = "0x" + "c3" * 32
OWNER = "0x1218000000000000000000000000000000000000"
HANDLER = "def handle(payload):\n    return {'served': True}\n"


def _topic_addr(address: str) -> str:
    return "0x" + "0" * 24 + address[2:].lower()


def _bound_logs(*, to: str, units: int, nonce: str, token: str = USDC) -> list[dict]:
    """FiatToken's order: the authorization over `nonce` is marked used, then its transfer."""
    return [
        {"address": token, "topics": [AUTH_USED, _topic_addr(BUYER), nonce], "data": "0x"},
        {"address": token, "topics": [TRANSFER, _topic_addr(BUYER), _topic_addr(to)],
         "data": hex(units)},
    ]


def fake_rpc(monkeypatch, *, to: str, units: int, token: str = USDC, nonce: str = NONCE,
             status: str = "0x1", block: int = 100, head: int = 101) -> dict:
    """Stand in for a Base node whose receipt is a transferWithAuthorization over `nonce`.
    Returns the call log so tests can inspect it."""
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
                        "logs": _bound_logs(to=to, units=units, nonce=nonce, token=token),
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


def _quote(api, slug="paid") -> dict:
    """Take a 402 and read the invoice it mints."""
    res = api.post(f"/t/{slug}/invoke", json={})
    assert res.status_code == 402
    return res.json()


def pay(api, monkeypatch, *, slug="paid", tx="0x" + "b" * 64, units=4000, to=OWNER,
        quote: dict | None = None) -> dict:
    """Quote, put the buyer's authorization over that 402's nonce on chain, and return the
    headers that redeem it."""
    q = quote or _quote(api, slug)
    fake_rpc(monkeypatch, to=to, units=units, nonce=q["nonce"])
    return {"X-Payment": tx, "X-Payment-Secret": q["payment_secret"]}


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
        verify_transfer(rpc_url="http://rpc.invalid", tx_hash="0x" + "a" * 64, terms=terms,
                        require_nonce=NONCE)


def test_verify_rejects_an_underpayment(monkeypatch) -> None:
    fake_rpc(monkeypatch, to=OWNER, units=3999)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    with pytest.raises(PaymentError, match="paid 3999 base units"):
        verify_transfer(rpc_url="http://rpc.invalid", tx_hash="0x" + "a" * 64, terms=terms,
                        require_nonce=NONCE)


def test_verify_rejects_a_reverted_transaction(monkeypatch) -> None:
    fake_rpc(monkeypatch, to=OWNER, units=4000, status="0x0")
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    with pytest.raises(PaymentError, match="reverted"):
        verify_transfer(rpc_url="http://rpc.invalid", tx_hash="0x" + "a" * 64, terms=terms,
                        require_nonce=NONCE)


def test_verify_waits_for_confirmations(monkeypatch) -> None:
    fake_rpc(monkeypatch, to=OWNER, units=4000, block=100, head=100)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 3)
    with pytest.raises(PaymentError, match="3 required"):
        verify_transfer(rpc_url="http://rpc.invalid", tx_hash="0x" + "a" * 64, terms=terms,
                        require_nonce=NONCE)


def test_verify_ignores_a_transfer_of_a_different_token(monkeypatch) -> None:
    fake_rpc(monkeypatch, to=OWNER, units=4000, token="0x" + "de" * 20)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    with pytest.raises(PaymentError, match="not bound to this call"):
        verify_transfer(rpc_url="http://rpc.invalid", tx_hash="0x" + "a" * 64, terms=terms,
                        require_nonce=NONCE)


def test_verify_refuses_a_reference_that_is_not_a_tx_hash() -> None:
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    with pytest.raises(PaymentError, match="0x transaction hash"):
        verify_transfer(rpc_url="http://rpc.invalid", tx_hash="i-paid-honest", terms=terms,
                        require_nonce=NONCE)


@pytest.mark.parametrize("nonce", ["", "0x1234", "not-a-nonce"])
def test_verify_settles_nothing_without_a_nonce(monkeypatch, nonce) -> None:
    """A plain transfer to the payout address is money from somebody, for something."""
    seen = fake_rpc(monkeypatch, to=OWNER, units=1_000_000)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    with pytest.raises(PaymentError, match="not bound to a call"):
        verify_transfer(rpc_url="http://rpc.invalid", tx_hash="0x" + "a" * 64, terms=terms,
                        require_nonce=nonce)
    assert seen["receipts"] == 0, "refused before the chain is even asked"


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
    res = api.post("/t/paid/invoke", json={}, headers=pay(api, monkeypatch))
    assert res.status_code == 200
    assert res.json()["result"] == {"served": True}


def test_one_payment_buys_exactly_one_call(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    deploy_priced(api)
    tx = pay(api, monkeypatch, tx="0x" + "c" * 64)
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
    paid = api.post(
        "/ai-market/v2/invoke",
        json={"capability_id": "paid.thing@v1", "input": {}},
        headers=pay(api, monkeypatch, tx="0x" + "d" * 64, quote=unpaid.json()),
    )
    assert paid.status_code == 200


def test_an_unreachable_chain_refuses_rather_than_serving_free_work(tmp_path, monkeypatch) -> None:
    import httpx

    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    deploy_priced(api)
    headers = pay(api, monkeypatch, tx="0x" + "e" * 64)

    def _boom(*_a, **_k):
        raise httpx.ConnectError("no route to node")

    monkeypatch.setattr("hestia.payments.httpx.post", _boom)
    res = api.post("/t/paid/invoke", json={}, headers=headers)
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
                        "logs": _bound_logs(to=OWNER, units=4000, nonce=NONCE),
                    }
                }

        return R()

    monkeypatch.setattr("hestia.payments.httpx.post", _post)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    settled = verify_transfer(
        rpc_url="http://broken, http://working", tx_hash="0x" + "a" * 64, terms=terms,
        require_nonce=NONCE,
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
        verify_transfer(rpc_url="http://only", tx_hash="0x" + "a" * 64, terms=terms,
                        require_nonce=NONCE)


def test_payment_is_released_when_the_tenant_is_unreachable(tmp_path, monkeypatch) -> None:
    """A wedged or crashed tenant is the platform's fault. The claimed payment
    is released so the buyer can retry the same transaction — otherwise an
    honest caller pays and gets a 502 with nothing to show for it."""
    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    deploy_priced(api)
    headers = pay(api, monkeypatch, tx="0x" + "f" * 64)

    # Kill the tenant process so the edge cannot reach it.
    api.app.state.stub._stop_all()
    res = api.post("/t/paid/invoke", json={}, headers=headers)
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
    res = api.post("/t/slow/invoke", json={},
                   headers=pay(api, monkeypatch, slug="slow", tx="0x" + "1" * 64))
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
                  block: int = 100, head: int = 101, nonce: str = NONCE) -> None:
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
                        "logs": _bound_logs(to=to, units=units, nonce=nonce),
                    }
                }

        return R()

    monkeypatch.setattr("hestia.payments.httpx.post", _post)


def test_a_fresh_payment_within_the_window_is_accepted(monkeypatch) -> None:
    import time as _time

    fake_rpc_aged(monkeypatch, to=OWNER, units=4000, mined_ts=int(_time.time()) - 30)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    settled = verify_transfer(
        rpc_url="http://rpc", tx_hash="0x" + "a" * 64, terms=terms, max_age_s=3600,
        require_nonce=NONCE,
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
            rpc_url="http://rpc", tx_hash="0x" + "b" * 64, terms=terms, max_age_s=3600,
            require_nonce=NONCE,
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
                        "logs": _bound_logs(to=OWNER, units=4000, nonce=NONCE),
                    }
                }

        return R()

    monkeypatch.setattr("hestia.payments.httpx.post", _post)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    with pytest.raises(PaymentError, match="age"):
        verify_transfer(
            rpc_url="http://rpc", tx_hash="0x" + "c" * 64, terms=terms, max_age_s=3600,
            require_nonce=NONCE,
        )


def test_max_age_zero_keeps_the_old_behaviour(monkeypatch) -> None:
    # With the window off, no block lookup happens and an old tx still verifies.
    fake_rpc(monkeypatch, to=OWNER, units=4000)
    terms = PaymentTerms("base", "USDC", USDC, 6, OWNER, 4000, 0.004, 1)
    settled = verify_transfer(
        rpc_url="http://rpc", tx_hash="0x" + "d" * 64, terms=terms, max_age_s=0,
        require_nonce=NONCE,
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
    res = api.post("/t/amp/invoke", json={},
                   headers=pay(api, monkeypatch, slug="amp", tx="0x" + "e" * 64))
    assert res.status_code == 502
    # The buyer got nothing, so the claim is released — the tx is not spent.
    assert api.app.state.store.payment_count("amp") == 0


# ------------------------------------------- EIP-3009 binding (payment ↔ call)

from hestia.payments import nonce_for_secret  # noqa: E402


def bound_settings(tmp_path) -> dict:
    return {
        **paid_settings(tmp_path).__dict__,
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
                # FiatToken's order: the authorization is marked used, then its transfer.
                logs = []
                if nonce is not None:
                    logs.append(
                        {
                            "address": token,
                            "topics": [AUTH_USED, _topic_addr(BUYER), nonce],
                            "data": "0x",
                        }
                    )
                logs.append(
                    {
                        "address": token,
                        "topics": [TRANSFER, _topic_addr(BUYER), _topic_addr(to)],
                        "data": hex(units),
                    }
                )
                return {"result": {"status": "0x1", "blockNumber": hex(100), "logs": logs}}

        return R()

    monkeypatch.setattr("hestia.payments.httpx.post", _post)


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
    q = _quote(api)
    nonce = q["nonce"]
    # A plain transfer: right token, right recipient, 250x the price, no auth log.
    fake_rpc_bound(monkeypatch, to=OWNER, units=1_000_000, nonce=None)
    res = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "a" * 64, "X-Payment-Nonce": nonce, "X-Payment-Secret": q["payment_secret"]},
    )
    assert res.status_code == 402
    assert "not bound to this call" in res.json()["detail"]
    assert api.app.state.store.payment_count("paid") == 0


def test_a_bound_payment_is_served(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    q = _quote(api)
    nonce = q["nonce"]
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=nonce)
    res = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "b" * 64, "X-Payment-Nonce": nonce, "X-Payment-Secret": q["payment_secret"]},
    )
    assert res.status_code == 200, res.text
    assert res.json()["result"] == {"served": True}


def test_an_authorization_for_a_different_nonce_is_refused(tmp_path, monkeypatch) -> None:
    """A payment signed for some OTHER call (another invoice, another hearth)
    must not settle this one."""
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    q = _quote(api)
    mine = q["nonce"]
    someone_elses = "0x" + "c" * 64
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=someone_elses)
    res = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "d" * 64, "X-Payment-Nonce": mine, "X-Payment-Secret": q["payment_secret"]},
    )
    assert res.status_code == 402
    assert "not bound to this call" in res.json()["detail"]


def test_a_nonce_buys_exactly_one_call(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    q = _quote(api)
    nonce = q["nonce"]
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=nonce)
    first = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "e" * 64, "X-Payment-Nonce": nonce, "X-Payment-Secret": q["payment_secret"]},
    )
    assert first.status_code == 200
    # Same authorization, a different transaction hash: the nonce is spent.
    second = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "f" * 64, "X-Payment-Nonce": nonce, "X-Payment-Secret": q["payment_secret"]},
    )
    assert second.status_code == 402
    assert "already been spent" in second.json()["detail"]


def test_a_payment_without_its_secret_is_refused_with_instructions(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    q = _quote(api)
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=q["nonce"])
    for headers in ({"X-Payment": "0x" + "1" * 64},
                    {"X-Payment": "0x" + "1" * 64, "X-Payment-Nonce": q["nonce"]}):
        res = api.post("/t/paid/invoke", json={}, headers=headers)
        assert res.status_code == 402
        assert "X-Payment-Secret" in res.json()["detail"]
    assert api.app.state.store.payment_count("paid") == 0


def test_the_secret_alone_names_the_nonce(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    q = _quote(api)
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=q["nonce"])
    res = api.post("/t/paid/invoke", json={},
                   headers={"X-Payment": "0x" + "1" * 64, "X-Payment-Secret": q["payment_secret"]})
    assert res.status_code == 200, res.text


def test_an_unknown_nonce_is_refused(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    secret = "0x" + hashlib.sha256(b"never quoted").hexdigest()
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=nonce_for_secret(secret))
    res = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "2" * 64, "X-Payment-Secret": secret},
    )
    assert res.status_code == 402
    assert "unknown payment nonce" in res.json()["detail"]


def test_an_expired_nonce_is_refused(tmp_path, monkeypatch) -> None:
    settings = {**bound_settings(tmp_path), "payment_invoice_ttl_s": 0}
    api = client(tmp_path, **settings)
    deploy_priced(api)
    q = _quote(api)
    nonce = q["nonce"]
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=nonce)
    res = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "3" * 64, "X-Payment-Nonce": nonce, "X-Payment-Secret": q["payment_secret"]},
    )
    assert res.status_code == 402
    assert "expired" in res.json()["detail"]


def test_a_nonce_from_another_tenant_is_refused(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api, slug="paid")
    deploy_priced(api, slug="other", price=0.001)
    q = _quote(api, slug="other")
    nonce = q["nonce"]
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=nonce)
    res = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "4" * 64, "X-Payment-Nonce": nonce, "X-Payment-Secret": q["payment_secret"]},
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
    q = _quote(api)
    nonce = q["nonce"]
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=nonce)
    api.app.state.stub._stop_all()  # tenant is gone: platform failure
    res = api.post(
        "/t/paid/invoke", json={},
        headers={"X-Payment": "0x" + "5" * 64, "X-Payment-Nonce": nonce, "X-Payment-Secret": q["payment_secret"]},
    )
    assert res.status_code == 502
    assert api.app.state.store.payment_count("paid") == 0
    invoice = api.app.state.store.invoice(nonce)
    assert float(invoice["consumed_at"]) == 0, "the nonce must be reusable"


# ----------------------------------------- who may redeem (the nonce is public once mined)


def test_the_402_commits_its_nonce_to_a_secret_only_its_caller_gets(tmp_path) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    res = api.post("/t/paid/invoke", json={})
    body = res.json()
    assert body["nonce"] == nonce_for_secret(body["payment_secret"])
    assert "X-Payment-Secret" in body["how"]
    # never in a header, where a proxy or a log might keep it
    assert body["payment_secret"] not in " ".join(f"{k}: {v}" for k, v in res.headers.items())
    # and every quote has its own
    assert _quote(api)["payment_secret"] != body["payment_secret"]


def test_someone_watching_the_chain_cannot_redeem_the_buyers_payment(tmp_path, monkeypatch) -> None:
    """The buyer's transferWithAuthorization is mined; its hash and its nonce are public.
    A watcher presenting them first used to get the call, and the buyer was then refused
    as "already spent". Without the secret the watcher is refused, and the buyer is not."""
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    q = _quote(api)
    fake_rpc_bound(monkeypatch, to=OWNER, units=4000, nonce=q["nonce"])
    tx = "0x" + "6" * 64
    other = _quote(api)["payment_secret"]            # a real secret, for another quote
    for attempt in ({"X-Payment": tx, "X-Payment-Nonce": q["nonce"]},
                    {"X-Payment": tx, "X-Payment-Nonce": q["nonce"], "X-Payment-Secret": q["nonce"]},
                    {"X-Payment": tx, "X-Payment-Nonce": q["nonce"], "X-Payment-Secret": other}):
        watcher = api.post("/t/paid/invoke", json={}, headers=attempt)
        assert watcher.status_code == 402, attempt
    assert api.app.state.store.payment_count("paid") == 0
    assert float(api.app.state.store.invoice(q["nonce"])["consumed_at"]) == 0
    buyer = api.post("/t/paid/invoke", json={},
                     headers={"X-Payment": tx, "X-Payment-Secret": q["payment_secret"]})
    assert buyer.status_code == 200, buyer.text
    assert api.app.state.store.payment_count("paid") == 1


# ------------------------------------ a transaction bundling several authorizations

ATTACKER = "0x" + "ad" * 20


def fake_rpc_logs(monkeypatch, logs: list[dict]) -> None:
    """A node whose receipt carries exactly `logs`, in order."""
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
                return {"result": {"status": "0x1", "blockNumber": hex(100), "logs": logs}}

        return R()

    monkeypatch.setattr("hestia.payments.httpx.post", _post)


def auth_used(who: str, nonce: str) -> dict:
    return {"address": USDC, "topics": [AUTH_USED, _topic_addr(who), nonce], "data": "0x"}


def transfer(frm: str, to: str, units: int) -> dict:
    return {"address": USDC, "topics": [TRANSFER, _topic_addr(frm), _topic_addr(to)], "data": hex(units)}


def test_a_buyers_authorization_bundled_by_someone_else_pays_only_the_buyer(tmp_path, monkeypatch) -> None:
    """Whoever holds a buyer's signed authorization before it is mined can submit it inside
    their own transaction, next to a 1-unit authorization over an invoice THEY took. Counted
    as "some authorization for my nonce" plus "transfers to the payee add up", the buyer's
    money paid for the attacker's call. Only the transfer an authorization itself moved pays."""
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    buyer, attacker = _quote(api), _quote(api)
    fake_rpc_logs(monkeypatch, [
        auth_used(BUYER, buyer["nonce"]), transfer(BUYER, OWNER, 4000),
        auth_used(ATTACKER, attacker["nonce"]), transfer(ATTACKER, OWNER, 1),
    ])
    tx = "0x" + "3" * 64
    stolen = api.post("/t/paid/invoke", json={},
                      headers={"X-Payment": tx, "X-Payment-Secret": attacker["payment_secret"]})
    assert stolen.status_code == 402 and "paid 1 base units" in stolen.json()["detail"]
    served = api.post("/t/paid/invoke", json={},
                      headers={"X-Payment": tx, "X-Payment-Secret": buyer["payment_secret"]})
    assert served.status_code == 200, served.text


def test_an_authorization_whose_transfer_went_elsewhere_pays_nothing(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    q = _quote(api)
    for logs in (
        # the authorization moved money to someone else; another transfer reached the payee
        [auth_used(BUYER, q["nonce"]), transfer(BUYER, ATTACKER, 4000), transfer(ATTACKER, OWNER, 4000)],
        # the transfer to the payee came from somebody other than the authorizer
        [auth_used(BUYER, q["nonce"]), transfer(ATTACKER, OWNER, 4000)],
        # the authorization is the last log: nothing it moved is in the receipt
        [transfer(BUYER, OWNER, 4000), auth_used(BUYER, q["nonce"])],
    ):
        fake_rpc_logs(monkeypatch, logs)
        res = api.post("/t/paid/invoke", json={},
                       headers={"X-Payment": "0x" + "4" * 64, "X-Payment-Secret": q["payment_secret"]})
        assert res.status_code == 402, logs
    assert api.app.state.store.payment_count("paid") == 0


def test_two_payments_in_one_transaction_buy_two_calls(tmp_path, monkeypatch) -> None:
    """A smart wallet may settle two invoices in one transaction. Each authorization is its
    own payment; keyed on the hash, the first redeemed denied the other."""
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    first, second = _quote(api), _quote(api)
    fake_rpc_logs(monkeypatch, [
        auth_used(BUYER, first["nonce"]), transfer(BUYER, OWNER, 4000),
        auth_used(BUYER, second["nonce"]), transfer(BUYER, OWNER, 4000),
    ])
    tx = "0x" + "5" * 64
    for q in (first, second):
        res = api.post("/t/paid/invoke", json={},
                       headers={"X-Payment": tx, "X-Payment-Secret": q["payment_secret"]})
        assert res.status_code == 200, res.text
    again = api.post("/t/paid/invoke", json={},
                     headers={"X-Payment": tx, "X-Payment-Secret": first["payment_secret"]})
    assert again.status_code == 402
    assert api.app.state.store.payment_count("paid") == 2


def test_the_nonce_rule_is_sha256_of_the_secrets_bytes() -> None:
    """Published (the compute 402's nonce_rule, the docs, the hub, every client): a nonce
    is sha256 of the secret's 32 raw bytes — not of its hex text."""
    assert nonce_for_secret("0x" + "00" * 32) == (
        "0x66687aadf862bd776c8fc18b8e9f8e20089714856ee233b3902a591d0d5f2925"
    )
    from hestia.payments import opens

    secret = "0x" + hashlib.sha256(b"k").hexdigest()
    assert opens(secret, nonce_for_secret(secret))
    assert opens(secret.upper().replace("0X", "0x"), nonce_for_secret(secret))
    assert not opens(secret, nonce_for_secret("0x" + hashlib.sha256(b"j").hexdigest()))
    assert not opens(nonce_for_secret(secret), nonce_for_secret(secret))


def test_a_same_nonce_authorization_placed_first_cannot_deny_the_buyer(tmp_path, monkeypatch) -> None:
    """EIP-3009 nonces are per signer: anyone holding the buyer's authorization before it is
    mined can sign a 1-unit one over the SAME nonce and put it ahead in a bundle. Every
    authorization carrying the nonce is tried, and the one that paid settles."""
    api = client(tmp_path, **bound_settings(tmp_path))
    deploy_priced(api)
    q = _quote(api)
    fake_rpc_logs(monkeypatch, [
        auth_used(ATTACKER, q["nonce"]), transfer(ATTACKER, OWNER, 1),
        auth_used(BUYER, q["nonce"]), transfer(BUYER, OWNER, 4000),
    ])
    res = api.post("/t/paid/invoke", json={},
                   headers={"X-Payment": "0x" + "7" * 64, "X-Payment-Secret": q["payment_secret"]})
    assert res.status_code == 200, res.text


# ------------------------------------------------ there is no unbound payment


def test_the_old_switch_is_refused_loudly_and_changes_nothing(monkeypatch, tmp_path, caplog) -> None:
    """HESTIA_PAYMENT_REQUIRE_BINDING=0 used to accept any transfer to the payout address.
    Nothing in a plain transfer says who paid, so whoever presented it first took the call.
    The switch is gone; an operator who still sets it is told so."""
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path / "env"))
    monkeypatch.setenv("HESTIA_PAYMENT_REQUIRE_BINDING", "0")
    with caplog.at_level("ERROR", logger="hestia.config"):
        settings = Settings.from_env()
    assert not hasattr(settings, "payment_require_binding")
    assert any("REQUIRE_BINDING=0 is ignored" in r.getMessage() for r in caplog.records)
    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    deploy_priced(api)
    q = _quote(api)
    assert q["binding"] == "eip3009" and q["nonce"] == nonce_for_secret(q["payment_secret"])
    fake_rpc_logs(monkeypatch, [transfer(BUYER, OWNER, 1_000_000)])
    for headers in ({"X-Payment": "0x" + "8" * 64},
                    {"X-Payment": "0x" + "8" * 64, "X-Payment-Secret": q["payment_secret"]}):
        res = api.post("/t/paid/invoke", json={}, headers=headers)
        assert res.status_code == 402, headers
    assert api.app.state.store.payment_count("paid") == 0


def test_a_claim_is_one_authorization_and_a_legacy_whole_claim_stays_spent(tmp_path) -> None:
    api = client(tmp_path, **paid_settings(tmp_path).__dict__)
    store = api.app.state.store
    common = {"slug": "paid", "chain": "base", "token": "USDC", "paid_units": 4000, "pay_to": OWNER}
    plain, legacy = "0x" + "9" * 64, "0x" + "a" * 64
    assert not store.claim_payment(tx_hash=plain, **common), "no bare-transaction claim"
    assert store.payment_count("paid") == 0
    # A row keyed on the bare hash was written while unbound payments existed.
    store.backend.execute_write(
        "INSERT INTO tenant_payments (tx_hash, slug, chain, token, paid_units, pay_to, spent_at) "
        "VALUES (?, 'paid', 'base', 'USDC', '4000', ?, 0)", (legacy, OWNER.lower()),
    )
    assert not store.claim_payment(tx_hash=f"{legacy}:{NONCE}", **common)
    assert store.claim_payment(tx_hash=f"{plain}:{NONCE}", **common)
