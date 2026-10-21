"""POST /ai-market/v2/invoke: payment terms, settlement, compute calls with their gate, and
hub-billed calls to a tenant.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from hestia.compute import (
    COMPUTE_CAPS,
    OK,
    PLATFORM_FAILURE,
    REPLICAS,
    ComputeRefused,
    Limits,
    build_receipt,
    find_function,
    hub_key_matches,
    judge,
    parse_request,
    refusal_body,
    run_replicas,
)
from hestia.compute import (
    price_of as compute_price_of,
)
from hestia.config import Settings
from hestia.hub_billing import billing_block, hub_for_key
from hestia.models import DeployRequest
from hestia.owners import OwnerProofError, canonical_owner_key
from hestia.payments import (
    PaymentError,
    PaymentTerms,
    is_address,
    is_nonce,
    is_secret,
    mint_secret,
    nonce_for_secret,
    opens,
    to_units,
    verify_transfer,
)
from hestia.runtime.stub import StubRuntime
from hestia.runtime.wasm import WasmRuntime, is_wasm
from hestia.signing import ProviderSigner
from hestia.store import TenantRow, TenantStore


def register(
    *,
    _deploy: Callable[..., Any],
    _offered: Callable[..., Any],
    _principal: Callable[..., Any],
    _receipt: Callable[..., Any],
    _stop: Callable[..., Any],
    _suspended: Callable[..., Any],
    app: FastAPI,
    compute_limits: Limits,
    compute_slots: Any,
    hearth: Callable[..., Any],
    settings: Settings,
    signer: ProviderSigner,
    store: TenantStore,
    stub: StubRuntime | WasmRuntime,
) -> SimpleNamespace:
    """Attach POST /ai-market/v2/invoke to ``app``."""
    # app.py's module-level helpers; imported here because app.py imports this module.
    from hestia.app import (
        Claim,
    )

    @app.post("/ai-market/v2/invoke")
    async def invoke(request: Request) -> dict[str, Any]:
        started = time.monotonic()
        raw = await request.body()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="body is not JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        cap = str(payload.get("capability_id") or "")
        inner = payload.get("input") if isinstance(payload.get("input"), dict) else payload
        if cap == "hestia.hearth.list@v1":
            return _receipt(hearth(), cap, inner, started=started)
        principal = None
        if cap in {"hestia.host.deploy@v1", "hestia.host.stop@v1"}:
            principal = await run_in_threadpool(_principal, request, raw)
        if cap == "hestia.host.deploy@v1":
            try:
                body = DeployRequest.model_validate(inner)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return _receipt(
                await run_in_threadpool(_deploy, body, principal), cap, inner, started=started
            )
        if cap == "hestia.host.status@v1":
            slug = str(inner.get("slug") or "")
            row = store.get(slug)
            if row is None:
                raise HTTPException(status_code=404, detail="unknown slug")
            return _receipt(row.public_dict(), cap, inner, started=started)
        if cap == "hestia.host.stop@v1":
            slug = str(inner.get("slug") or "")
            return _receipt(
                await run_in_threadpool(_stop, slug, principal), cap, inner, started=started
            )
        if cap in COMPUTE_CAPS and settings.compute_enabled:
            # Off the event loop: the chain lookup and the replicas take seconds.
            return await run_in_threadpool(_compute, cap, inner, request.headers, started)
        # A hub routes by capability_id against this one endpoint, so anything a
        # listed tenant advertises has to be dispatched to that tenant here. Its
        # own Ed25519 envelope is returned untouched: the receipt names the
        # provider that did the work, not the hearth that carried the call.
        tenant = _tenant_for_capability(cap)
        if tenant is not None:
            api_key = request.headers.get("x-api-key", "").strip()
            if api_key:
                # A hub selling this tenant on its owner's behalf (hestia/hub_billing.py).
                return await _hub_billed_invoke(
                    tenant, cap, inner, api_key, request.headers.get("x-aimarket-hub-charged", "")
                )
            claim = None
            terms = _terms_for(tenant)
            if terms is not None:
                refusal, claim = _settle(
                    tenant,
                    terms,
                    request.headers.get("x-payment", "").strip(),
                    request.headers.get("x-payment-nonce", "").strip(),
                    request.headers.get("x-payment-secret", "").strip(),
                )
                if refusal is not None:
                    return refusal
            return await _invoke_tenant(tenant, inner, claim)
        raise HTTPException(status_code=404, detail="unknown capability")

    def _terms_for(row: TenantRow) -> PaymentTerms | None:
        """What this tenant charges, or None when the call is free.

        Free is the default and stays free: payments are off unless the operator
        enabled them AND the tenant published a payout address AND the price is
        above zero. Any one of those missing means the call is not billed —
        rather than billed to nobody.
        """
        if not settings.payments_enabled:
            return None
        price = float(row.capability.get("price_per_call_usd") or 0.0)
        if price <= 0 or not is_address(row.payout_address):
            return None
        return PaymentTerms(
            chain=settings.payment_chain,
            token=settings.payment_token,
            token_contract=settings.payment_token_contract,
            decimals=settings.payment_decimals,
            pay_to=row.payout_address,
            amount_units=to_units(price, settings.payment_decimals),
            amount_usd=price,
            min_confirmations=settings.payment_min_confirmations,
            chain_id=settings.payment_chain_id,
            eip712_name=settings.payment_token_eip712_name,
            eip712_version=settings.payment_token_eip712_version,
        )

    def _payment_required(row: TenantRow, terms: PaymentTerms, detail: str) -> JSONResponse:
        """x402-shaped 402 naming the tenant owner's own address.

        A hub relays a peer's 402 verbatim, so this is what a buyer sees no
        matter which door they came through.

        Every 402 mints a fresh invoice nonce. When binding is required that
        nonce is what the buyer signs into the payment itself, so the payment
        can only ever settle THIS call.

        The nonce is sha256 of a secret that goes to this caller and nowhere else
        (payments.py, "who paid"): once the payment is mined the nonce is public,
        and only the secret shows that the one redeeming it is the one who asked.
        """
        resource = f"{row.public_url}/invoke"
        accept = terms.as_x402_accept()
        accept["resource"] = resource
        secret = mint_secret()
        nonce = nonce_for_secret(secret)
        invoice = store.mint_invoice(
            nonce=nonce,
            slug=row.slug,
            capability_id=str(row.capability.get("capability_id") or ""),
            chain=terms.chain,
            token=terms.token,
            token_contract=terms.token_contract,
            pay_to=terms.pay_to,
            amount_units=terms.amount_units,
            ttl_s=settings.payment_invoice_ttl_s,
        )
        accept.setdefault("extra", {})
        accept["extra"] = {**accept["extra"], "nonce": nonce}
        how = (
            f"pay {terms.amount_usd} {terms.token} to {terms.pay_to} on "
            f"{terms.chain} with the EIP-3009 call "
            f"transferWithAuthorization(..., nonce={nonce}), then retry with "
            "headers X-Payment: <tx hash> and X-Payment-Secret: <payment_secret "
            "from this answer>. Signing that nonce is what binds the payment to "
            "this call; a plain transfer is refused because it could have been "
            "made for something else. Keep payment_secret to yourself: the nonce "
            "and the transaction are public once mined, and the secret is what "
            "shows that you, not someone watching the chain, are the one who "
            "paid. The hearth holds no funds and needs no key: the token "
            "contract verifies your signature on chain."
        )
        return JSONResponse(
            status_code=402,
            content={
                "ok": False,
                "error": "payment_required",
                "detail": detail,
                "x402Version": 1,
                "resource": {"url": resource, "description": row.capability.get("name", "")},
                "accepts": [accept],
                "pay_to": terms.pay_to,
                "amount_usd": terms.amount_usd,
                "amount_units": str(terms.amount_units),
                "chain": terms.chain,
                "token": terms.token,
                "token_contract": terms.token_contract,
                "nonce": nonce,
                "binding": "eip3009",
                # Only in this body, never in a header a proxy might log.
                "payment_secret": secret,
                "expires_at": invoice["expires_at"],
                "how": how,
            },
            headers={"X-Payment-Required": "exact", "X-Payment-Nonce": nonce},
        )

    def _release_claim(claim: Claim | None) -> None:
        """Give back everything a failed call had taken: the spent transaction
        and the invoice nonce. Releasing only the transaction would leave the
        buyer with a burned nonce and no way to retry."""
        if claim is None:
            return
        store.release_payment(claim.tx_hash)
        if claim.nonce:
            store.release_invoice(claim.nonce)

    def _release_on_platform_failure(claim: Claim | None, status: int) -> None:
        """Un-spend a claim when the tenant failed on the platform's side and
        delivered nothing. A 5xx other than 504 (the per-call deadline, which the
        caller's own input consumed) is a platform/provider failure; a 4xx
        rejected the caller's input. Only the former releases."""
        if claim and 500 <= status < 600 and status != 504:
            _release_claim(claim)

    def _settle(
        row: TenantRow, terms: PaymentTerms, tx_hash: str, nonce: str = "", secret: str = ""
    ) -> tuple[JSONResponse | None, Claim | None]:
        """(refusal, claim). refusal is None when the call may proceed; the claim
        names what to release if the tenant then fails on the platform's side."""
        if not tx_hash:
            return _payment_required(row, terms, "this capability is priced"), None

        nonce = (nonce or "").strip().lower()
        secret = (secret or "").strip().lower()
        # The nonce identifies WHICH call this payment settles. Without it the
        # payment is just "a transfer to that address", which a transfer made
        # for something else also satisfies. The secret shows WHO may settle it:
        # the nonce is on chain for anyone to copy once the payment is mined.
        if not is_secret(secret):
            return _payment_required(
                row, terms,
                "send X-Payment-Secret with the payment_secret from the 402 you "
                "paid: the nonce and the transaction are public once mined, so "
                "they cannot show who paid",
            ), None
        if not nonce:
            nonce = nonce_for_secret(secret)
        if not is_nonce(nonce):
            return _payment_required(
                row, terms,
                "send X-Payment-Nonce with the nonce from this 402 and pay with "
                "transferWithAuthorization signed over it",
            ), None
        if not opens(secret, nonce):
            return _payment_required(
                row, terms, "that payment secret does not open this payment nonce",
            ), None
        invoice = store.invoice(nonce)
        if invoice is None or invoice["slug"] != row.slug:
            return _payment_required(row, terms, "unknown payment nonce"), None
        if float(invoice["consumed_at"] or 0) > 0:
            return _payment_required(
                row, terms, "that payment nonce has already been spent"
            ), None
        if time.time() > float(invoice["expires_at"]):
            return _payment_required(
                row, terms, "that payment nonce has expired; take a fresh 402"
            ), None
        if int(invoice["amount_units"]) > terms.amount_units:
            # The quoted price is what the buyer must meet, even if the
            # tenant has since been re-deployed cheaper.
            terms = replace(terms, amount_units=int(invoice["amount_units"]))
        try:
            settled = verify_transfer(
                rpc_url=settings.payment_rpc_url,
                tx_hash=tx_hash,
                terms=terms,
                max_age_s=settings.payment_max_age_s,
                require_nonce=nonce,
            )
        except PaymentError as exc:
            return _payment_required(row, terms, str(exc)), None
        except httpx.HTTPError:
            # The chain is unreachable. Serving the call would give the work away;
            # claiming it was paid would be a lie. Refuse, and say which.
            raise HTTPException(
                status_code=503, detail="payment verifier unavailable"
            ) from None
        if not store.consume_invoice(nonce, settled["tx_hash"]):
            # Two calls raced on one nonce; the database picked the winner.
            return _payment_required(
                row, terms, "that payment nonce has already been spent"
            ), None
        # One claim per payment, the authorization: a bundle of several in one transaction
        # is several payments, and keying on the hash let whoever redeemed first deny every
        # other payer in it.
        claim_key = f"{settled['tx_hash']}:{nonce}"
        if not store.claim_payment(
            tx_hash=claim_key, slug=row.slug, chain=terms.chain,
            token=terms.token, paid_units=settled["paid_units"], pay_to=terms.pay_to,
        ):
            store.release_invoice(nonce)
            return _payment_required(
                row, terms, "that payment has already been spent on another call"
            ), None
        return None, Claim(tx_hash=claim_key, nonce=nonce)

    # ------------------------------------------------------------ compute

    def _compute_terms(capability_id: str) -> PaymentTerms | None:
        """What a direct compute call costs, or None on a hub-key-only hearth."""
        if not is_address(settings.compute_payout_address):
            return None
        price = compute_price_of(settings, capability_id)
        return PaymentTerms(
            chain=settings.payment_chain,
            token=settings.payment_token,
            token_contract=settings.payment_token_contract,
            decimals=settings.payment_decimals,
            pay_to=settings.compute_payout_address,
            amount_units=to_units(price, settings.payment_decimals),
            amount_usd=price,
            min_confirmations=settings.payment_min_confirmations,
            chain_id=settings.payment_chain_id,
            eip712_name=settings.payment_token_eip712_name,
            eip712_version=settings.payment_token_eip712_version,
        )

    def _compute_unpaid(capability_id: str, detail: str) -> JSONResponse:
        """Refuse an unpaid compute call — WITHOUT minting a nonce.

        On the production rail the hub is the till: it mints its own nonce, the
        buyer's one EIP-3009 authorization carries that nonce, and the hub forwards
        the transaction here with its key. A second nonce from this hearth is
        exactly the two-till failure (docs/hestia-hub-market-rail.md §1: one
        authorization can satisfy only one of them). So this 402 quotes terms and
        never invoices.

        A direct buyer, with no key, picks the nonce: sha256 of a secret only they
        hold (binding "secret"). A bare transaction hash is not enough at this door
        — it is public the moment the payment is mined, and whoever presented it
        first used to get the call the buyer paid for.
        """
        terms = _compute_terms(capability_id)
        if terms is None:
            return JSONResponse(
                status_code=401,
                content={
                    "ok": False,
                    "error": "hub_key_required",
                    "detail": (
                        f"{detail}. Compute on this hearth is sold through its hub, which "
                        "presents a key (X-API-Key); there is no direct paid door."
                    ),
                },
            )
        resource = f"{settings.public_base}/ai-market/v2/invoke"
        accept = terms.as_x402_accept()
        accept["resource"] = resource
        return JSONResponse(
            status_code=402,
            content={
                "ok": False,
                "error": "payment_required",
                "detail": detail,
                "x402Version": 1,
                "resource": {"url": resource, "description": capability_id},
                "accepts": [accept],
                "pay_to": terms.pay_to,
                "amount_usd": terms.amount_usd,
                "amount_units": str(terms.amount_units),
                "chain": terms.chain,
                "token": terms.token,
                "token_contract": terms.token_contract,
                "binding": "secret",
                "nonce_rule": "sha256(secret)",
                "max_age_s": settings.payment_max_age_s,
                "how": (
                    "choose 32 random bytes S and keep them to yourself; pay "
                    f"{terms.amount_usd} {terms.token} to {terms.pay_to} on {terms.chain} with "
                    "the EIP-3009 call transferWithAuthorization(..., nonce=sha256(S)); then "
                    "retry with headers X-Payment: <tx hash> and X-Payment-Secret: 0x<S hex>. "
                    "A plain transfer, or a nonce you cannot open, is refused at this door: "
                    "the transaction and its nonce are public once mined, and only the secret "
                    "shows that you are the one who paid. One transaction pays for one call. "
                    "The usual door is the hub's catalogue, which invoices and forwards the "
                    "payment with its key."
                ),
            },
            headers={"X-Payment-Required": "exact"},
        )

    def _compute_gate(capability_id: str, headers: Any) -> tuple[str, JSONResponse | None]:
        """(claim key or "", refusal). Compute is never free when it is on.

        Two ways in, both of which mean someone was paid:
        - X-API-Key equal to a configured hub key: that hub billed the buyer on
          its credits rail (a mandate included) and vouches for the call;
        - X-Payment with X-Payment-Secret: a transaction this hearth reads on
          chain — an EIP-3009 transfer of at least the price to the dedicated
          compute address whose nonce is sha256 of that secret, fresh, and claimed
          here exactly once (the ledger's primary key decides, not a read-then-write).
        HESTIA_PAYMENTS_ENABLED is not consulted: it is the TENANT till switch, off
        on the production rail, and leaving it off must not make compute free.

        A hub forwards its key WITH a buyer's transfer (the peer authenticates the hub
        either way). The transfer is then what pays, so it is verified and claimed as if
        it came alone: accepted on the key without being claimed, it would stay unspent
        on chain for anyone who saw it to redeem here.
        """
        presented_key = (headers.get("x-api-key") or "").strip()
        if presented_key and not hub_key_matches(presented_key, settings.compute_hub_keys):
            return "", JSONResponse(
                status_code=401,
                content={"ok": False, "error": "invalid_api_key", "detail": "unknown X-API-Key"},
            )
        tx_hash = (headers.get("x-payment") or "").strip()
        terms = _compute_terms(capability_id)
        if presented_key and not (tx_hash and terms is not None):
            return "", None
        if not tx_hash:
            return "", _compute_unpaid(capability_id, "compute is priced")
        if terms is None:
            return "", _compute_unpaid(capability_id, "this hearth takes no direct payment")
        # Who may redeem this transaction, and which payment in it is theirs.
        # - No key: the caller must show it is the payer. The nonce it paid under must
        #   open to its secret; the tx hash and the nonce alone are public once mined.
        # - The hub's key: the hub vouches that it settled this payment against its own
        #   invoice, and names that invoice's nonce (X-Payment-Nonce).
        # Either way an authorization is named. A plain transfer names none, so nothing
        # says who paid it or for what: whoever presented it first would take the call.
        secret = (headers.get("x-payment-secret") or "").strip().lower()
        named = (headers.get("x-payment-nonce") or "").strip().lower()
        if not presented_key:
            if not is_secret(secret):
                return "", _compute_unpaid(
                    capability_id,
                    "send X-Payment-Secret with the secret whose sha256 you signed as the "
                    "payment nonce: a transaction hash alone is public once mined",
                )
            if len(set(bytes.fromhex(secret[2:]))) < 16:
                # 32 random bytes hold ~30 distinct values; fewer than 16 has odds far below
                # any key's. A pattern like that is one a watcher can precompute.
                return "", _compute_unpaid(
                    capability_id,
                    "that payment secret is guessable (fewer than 16 distinct bytes): "
                    "anyone could open its nonce. Pay again with 32 random bytes",
                )
            if named and not opens(secret, named):
                return "", _compute_unpaid(
                    capability_id, "that payment secret does not open X-Payment-Nonce",
                )
            bound_to = nonce_for_secret(secret)
        else:
            if named and not is_nonce(named):
                return "", _compute_unpaid(capability_id, "X-Payment-Nonce is not a payment nonce")
            if secret and not is_secret(secret):
                return "", _compute_unpaid(capability_id, "X-Payment-Secret is not a payment secret")
            if secret and not opens(secret, named or nonce_for_secret(secret)):
                return "", _compute_unpaid(capability_id, "that payment secret does not open its nonce")
            bound_to = named or (nonce_for_secret(secret) if secret else "")
            if not bound_to:
                return "", _compute_unpaid(
                    capability_id,
                    "name the authorization that pays with X-Payment-Nonce: a plain "
                    "transfer cannot show who paid it, or for what",
                )
        try:
            settled = verify_transfer(
                rpc_url=settings.payment_rpc_url,
                tx_hash=tx_hash,
                terms=terms,
                max_age_s=settings.payment_max_age_s,
                # No nonce of OURS exists (see _compute_unpaid): the payer's (direct) or
                # the hub's invoice nonce (forwarded) names the authorization that pays.
                require_nonce=bound_to,
            )
        except PaymentError as exc:
            return "", _compute_unpaid(capability_id, str(exc))
        except httpx.HTTPError:
            raise HTTPException(status_code=503, detail="payment verifier unavailable") from None
        # One claim per payment: an authorization is its own payment (a bundle of them in
        # one transaction is several).
        claim_key = f"{settled['tx_hash']}:{bound_to}"
        if not store.claim_payment(
            tx_hash=claim_key,
            # A capability id, never a tenant slug (those cannot hold '.' or '@'),
            # in the same table: one payment cannot pay a tenant AND compute.
            slug=capability_id,
            chain=terms.chain,
            token=terms.token,
            paid_units=settled["paid_units"],
            pay_to=terms.pay_to,
        ):
            return "", _compute_unpaid(
                capability_id, "that payment has already been spent on another call"
            )
        return claim_key, None

    def _compute(
        capability_id: str, inner: Any, headers: Any, started: float
    ) -> dict[str, Any] | JSONResponse:
        """One compute call. Every refusal that can be decided without running
        anything is decided BEFORE the payment is taken, so a bad request never
        spends a buyer's transaction; after it is taken, it is given back on every
        path where the hearth, not the input, is why nothing came back."""
        try:
            request_ = parse_request(inner)
        except ComputeRefused as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from None
        function = find_function(settings, store, stub, request_.slug)
        if function is None:
            raise HTTPException(
                status_code=404,
                detail=f"no compute function '{request_.slug}' on this hearth (GET /v1/compute)",
            )
        if request_.function_sha256 and request_.function_sha256 != function.function_sha256:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"function_sha256 mismatch: this hearth runs {function.function_sha256} "
                    f"for '{function.slug}', not the function you pinned"
                ),
            )
        if function.refused:
            raise HTTPException(
                status_code=422,
                detail=f"'{function.slug}' is not admissible for compute: {function.refused}",
            )
        claimed, refusal = _compute_gate(capability_id, headers)
        if refusal is not None:
            return refusal
        count = REPLICAS[capability_id]
        if not compute_slots.acquire(count, timeout=compute_limits.wall_ms / 1000):
            if claimed:
                store.release_payment(claimed)
            raise HTTPException(status_code=503, detail="compute is at capacity; retry shortly")
        try:
            replicas = run_replicas(function.source, request_.payload, compute_limits, count)
        except BaseException:
            if claimed:
                store.release_payment(claimed)
            raise
        finally:
            compute_slots.release(count)
        verdict = judge(replicas)
        if verdict.outcome == PLATFORM_FAILURE:
            # The hearth failed to run it: same rule as a tenant 5xx on the edge.
            if claimed:
                store.release_payment(claimed)
            raise HTTPException(
                status_code=502, detail=f"compute failed on the hearth's side: {verdict.detail}"
            )
        if verdict.outcome != OK:
            # The work ran and the honest answer is "no": a limit the input hit,
            # the function's own error, or replicas that disagree. The payment
            # stands (seller-direct has no refunds, and a released transaction
            # would buy the same refused work again for free); nothing is signed.
            return refusal_body(
                capability_id=capability_id,
                function=function,
                request=request_,
                judgement=verdict,
                replicas=replicas,
                limits=compute_limits,
            )
        receipt = build_receipt(
            capability_id=capability_id,
            hearth=settings.public_base,
            function=function,
            request=request_,
            judgement=verdict,
            replicas=replicas,
            limits=compute_limits,
        )
        receipt["signature"] = signer.sign_object(receipt)
        return _receipt(
            {"output": verdict.output, "compute_receipt": receipt},
            capability_id,
            inner,
            started=started,
        )

    async def _read_capped(response: httpx.Response) -> bytes:
        """Read a tenant response, refusing one that blows past the ceiling.

        The edge caps what comes IN (BodyLimitMiddleware) but used to read what a
        tenant sends back without any bound and then re-serialise it two or three
        more times, so a handler that amplifies a small input into a huge output
        (rules-decide's trace does exactly this) could exhaust the control plane.
        A per-agent memory cap kills the child; this protects the parent.
        """
        limit = settings.max_tenant_response_bytes
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > limit:
                await response.aclose()
                raise HTTPException(status_code=502, detail="tenant response too large")
            chunks.append(chunk)
        return b"".join(chunks)

    def _tenant_for_capability(capability_id: str) -> TenantRow | None:
        """The agent a routed call reaches: only one offered in the manifest. A held agent, or
        a suspended owner's, is not for sale through a hub."""
        if not capability_id:
            return None
        suspended = _suspended()
        for row in store.list_running():
            if row.capability.get("capability_id") == capability_id and _offered(row, suspended):
                return row
        return None

    def _still_serving(row: TenantRow) -> bool:
        current = store.get(row.slug)
        return (
            current is not None
            and current.status == "running"
            and current.listen_url == row.listen_url
        )

    def _open_owner(row: TenantRow) -> str | None:
        """The owner whose agent runs in the open lane, or None for everyone else's.

        Open: an owner that admitted itself (admitted_by "open", which is also what a row
        without the column reads as). Everyone else runs outside the lane: the operator's own
        agent (a key nobody can own), an owner the operator admitted, and an agent the
        operator deployed in the name of a key that has no owner row. Only the operator can
        write such a row: a key that signs its own deploy is given a row first.
        """
        try:
            owner = canonical_owner_key(row.owner_pubkey)
        except OwnerProofError:
            return None
        record = store.get_owner(owner)
        if record is None or record.get("admitted_by") == "operator":
            return None
        return owner

    def _hub_may_bill(row: TenantRow, hub: str) -> tuple[str, str]:
        """(account to credit at that hub, refusal). An operator tenant: ("", "")."""
        try:
            owner = canonical_owner_key(row.owner_pubkey)
        except OwnerProofError:
            # The placeholder and other keys nobody can own: the operator's tenant. The operator
            # configured this hub's key, so the hub sells it and keeps what it charges.
            return "", ""
        record = store.get_owner(owner)
        if record is None or record["status"] != "active":
            return "", "this agent's owner is not active on this hearth"
        account = store.owner_billing(owner).get(hub, "")
        if not account:
            return "", (f"this agent's owner has not chosen {hub} to sell it on their behalf; "
                        "pay the agent directly (x402)")
        return account, ""

    async def _hub_billed_invoke(
        row: TenantRow, cap: str, inner: dict[str, Any], api_key: str, charged_header: str = ""
    ) -> Any:
        hub = hub_for_key(api_key, settings.tenant_hub_keys)
        if hub is None:
            return JSONResponse(status_code=401, content={
                "ok": False, "error": "invalid_api_key", "detail": "unknown X-API-Key"})
        bill_to, refusal = await run_in_threadpool(_hub_may_bill, row, hub)
        # A hub says what it charged its buyer (aimarket-hub: X-AIMarket-Hub-Charged). Zero is a
        # free trial: the owner would work for nothing, so an owner's agent is not served that way.
        # An operator tenant is — giving trials away is the operator's own call. A hub that sends
        # no header is taken at its word that it charged.
        try:
            charged = float(charged_header) if charged_header.strip() else None
        except ValueError:
            charged = None
        trial = charged is not None and charged <= 0
        if trial and bill_to and not refusal:
            refusal = "this agent's owner is not paid for free trials; it is not served as one"
        if refusal:
            # A 402 tells the hub its key does not pay here: it charges its buyer nothing.
            return JSONResponse(status_code=402, content={
                "ok": False, "error": "payment_required", "detail": refusal})
        result = await _invoke_tenant(row, inner, None)
        price = float(row.capability.get("price_per_call_usd") or 0.0)
        delivered = (isinstance(result, dict) and result.get("ok") is not False
                     and result.get("success") is not False)
        if price > 0 and delivered and not trial:
            block = billing_block(
                signer, hearth=settings.public_base, hub=hub, slug=row.slug, capability_id=cap,
                owner_pubkey=row.owner_pubkey, bill_to_account=bill_to, price_usd=price,
                input_payload=inner,
            )
            await run_in_threadpool(store.record_hub_call, block)
            result["hub_billing"] = block
        return result

    async def _invoke_tenant(
        row: TenantRow, payload: dict[str, Any], claim: Claim | None = None
    ) -> dict[str, Any]:
        if not _still_serving(row):
            _release_claim(claim)
            raise HTTPException(status_code=503, detail="tenant changed during the request; retry")
        if is_wasm(row.listen_url):
            status, answer = await stub.invoke(row.slug, row.capability, payload,
                                               open_owner=_open_owner(row))
            if status != 200:
                _release_on_platform_failure(claim, status)
                raise HTTPException(
                    status_code=status,
                    detail=json.dumps(answer, separators=(",", ":"), ensure_ascii=False)[:300],
                )
            return answer
        target = f"{row.listen_url.rstrip('/')}/invoke"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                async with client.stream("POST", target, json=payload) as streamed:
                    body = await _read_capped(streamed)
                    status = streamed.status_code
        except httpx.HTTPError as exc:
            _release_claim(claim)
            raise HTTPException(status_code=502, detail="tenant unreachable") from exc
        except HTTPException:
            # _read_capped refused an oversized tenant response: the provider
            # over-produced and the buyer got nothing back. That is a platform
            # failure, so the claim is released rather than spent on a call that
            # was never delivered.
            _release_claim(claim)
            raise
        if status != 200:
            _release_on_platform_failure(claim, status)
            raise HTTPException(status_code=status, detail=body.decode(errors="replace")[:300])
        import json as _json

        return _json.loads(body)

    async def _wasm_edge(
        row: TenantRow, path: str, request: Request, claim: Claim | None
    ) -> Response:
        """The tenant's own door for a sandboxed tenant: /health and /invoke, nothing else."""
        if path == "health" and request.method == "GET":
            return JSONResponse(stub.health(row.slug))
        if path != "invoke" or request.method != "POST":
            _release_claim(claim)
            raise HTTPException(status_code=404, detail="not found")
        raw = await request.body()
        try:
            body = json.loads(raw.decode() or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid json"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400,
                                content={"ok": False, "error": "body must be an object"})
        if not _still_serving(row):
            _release_claim(claim)
            raise HTTPException(status_code=503, detail="tenant changed during the request; retry")
        status, answer = await stub.invoke(row.slug, row.capability, body,
                                           open_owner=_open_owner(row))
        _release_on_platform_failure(claim, status)
        return JSONResponse(status_code=status, content=answer)

    return SimpleNamespace(
        _read_capped=_read_capped,
        _release_claim=_release_claim,
        _release_on_platform_failure=_release_on_platform_failure,
        _settle=_settle,
        _still_serving=_still_serving,
        _terms_for=_terms_for,
        _wasm_edge=_wasm_edge,
    )
