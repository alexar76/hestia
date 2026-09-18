"""HESTIA control plane and invoke edge."""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from hestia import __version__
from hestia.config import Settings
from hestia.hub import AnnounceError
from hestia.hub import announce as hub_announce
from hestia.limits import BodyLimitMiddleware
from hestia.models import CapabilitySpec, DeployRequest, TemplateSource
from hestia.payments import (
    PaymentError,
    PaymentTerms,
    is_address,
    is_nonce,
    to_units,
    verify_transfer,
)
from hestia.policy import profile_from_settings
from hestia.runtime.docker import DockerRuntime
from hestia.runtime.stub import StubRuntime
from hestia.scan import HandlerDenied, admit_handler
from hestia.signing import ProviderSigner
from hestia.store import TenantRow, TenantStore
from hestia.themis import AdmissionDenied
from hestia.themis import admit as themis_admit

logger = logging.getLogger(__name__)

PRODUCT_ID = "hestia"
_EDGE_FORWARD_HEADERS = frozenset({"content-type", "accept"})


@dataclass(frozen=True)
class Claim:
    """What a settled call holds so a platform-side failure can give it back.

    The transaction stops one payment buying two calls; the invoice nonce stops a
    payment buying a call it was not signed for. Releasing has to hand back both,
    or the buyer keeps the money spent and the invoice burned."""

    tx_hash: str
    nonce: str = ""


def tenant_handle(source_kind: str, slug: str) -> str:
    """The runtime handle for a slug, derived — never remembered.

    DockerRuntime.start returns `hestia-{slug}`, StubRuntime.start returns the
    slug, so this reproduces both. An in-memory map would go empty on restart
    and `stop` would then hand docker the bare slug and rm nothing while the
    ledger recorded "stopped".
    """
    return f"hestia-{slug}" if source_kind == "image" else slug


def edge_forward_headers(headers: Any) -> dict[str, str]:
    """Edge proxy allowlist. Authorization must never reach the tenant."""
    return {
        key: value
        for key, value in headers.items()
        if key.lower() in _EDGE_FORWARD_HEADERS
    }


CAPS = (
    {
        "capability_id": "hestia.host.deploy@v1",
        "name": "Deploy a provider onto the hearth",
        "description": "Start an isolated AIMarket provider. Not a Hub listing. Not a job board.",
        "price_per_call_usd": 0.05,
    },
    {
        "capability_id": "hestia.host.status@v1",
        "name": "Tenant status",
        "description": "Status of one slug on this hearth.",
        "price_per_call_usd": 0.001,
    },
    {
        "capability_id": "hestia.hearth.list@v1",
        "name": "Hearth roster",
        "description": "Public list of tenants this hearth is actually running.",
        "price_per_call_usd": 0.0,
    },
    {
        "capability_id": "hestia.host.stop@v1",
        "name": "Stop a tenant",
        "description": "Stop a tenant you own. Does not unlist a Hub capability by itself.",
        "price_per_call_usd": 0.01,
    },
)


def restore_stub_tenants(store: TenantStore, stub: StubRuntime, profile) -> dict[str, int]:
    """Start again, at boot, the stub tenants the ledger says were running.

    A stub tenant is a child of the control-plane process, so a restart kills
    every one of them while the ledger still says `running` — and the recorded
    loopback port is then free for any other local process to take, which the
    unauthenticated /t/{slug} edge would happily proxy. That hazard used to be
    closed by marking the rows stopped, which was safe but meant every restart
    silently emptied the host and someone had to redeploy by hand.

    Everything needed to start them again is on disk in the data volume:
    capability.json, handler.py, and tenant.key — so a restored tenant keeps its
    OWN signing identity and a buyer who pinned its key sees no change.

    Restoring is per-tenant and best-effort. A tenant that will not come back is
    marked stopped with the reason: the one thing that must never happen is a row
    left `running` while pointing at a port this process does not own.
    """
    restored = 0
    failed = 0
    for row in store.list_running():
        if row.source_kind == "image":
            continue  # a container outlives the control plane; leave it alone
        try:
            running = stub.start(
                slug=row.slug,
                capability=CapabilitySpec.model_validate(row.capability),
                handler=stub.stored_handler(row.slug),
                image_digest="",
                profile=profile,
                env={},
            )
        except Exception as exc:  # noqa: BLE001 — one bad tenant must not stop the hearth
            store.set_status(
                row.slug, "stopped", error=f"restore after restart failed: {exc}"[:500]
            )
            failed += 1
            logger.warning("tenant %s did not come back after restart: %s", row.slug, exc)
            continue
        # The port is new on every start, so the row has to be rewritten. This is
        # the whole reason the old code refused to trust a surviving row.
        row.listen_url = running.listen_url
        row.updated_at = time.time()
        row.last_error = ""
        store.upsert(row)
        restored += 1
    if restored or failed:
        logger.info("restored %d stub tenant(s) after restart, %d failed", restored, failed)
    return {"restored": restored, "failed": failed}


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = TenantStore.open(data_dir=settings.data_dir, database_url=settings.database_url)
    signer = ProviderSigner(settings.data_dir / "provider.key")
    profile = profile_from_settings(settings)
    stub = StubRuntime(settings.data_dir)
    # A stub tenant is a child of THIS process on an ephemeral loopback port.
    # Shared Postgres does not make those children multi-AZ: a row left running
    # by a previous process names a port the OS has since handed out, and the
    # public unauthenticated /t/{slug} edge would then proxy whoever bound it.
    # HESTIA_REPLICAS>1 is refused: API workers cannot each spawn a farm.
    # Docker rows are left alone — those containers outlive the plane.
    restore_stub_tenants(store, stub, profile)
    docker = DockerRuntime(
        settings.docker_bin,
        settings.allow_image_digests,
        client_env=settings.docker_client_env(),
        oci_runtime=settings.docker_runtime,
        require_gvisor=settings.require_gvisor,
    )
    app = FastAPI(title="HESTIA", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    console_dir = Path(__file__).resolve().parent.parent / "console"
    landing_dir = Path(__file__).resolve().parent.parent / "docs" / "landing"
    fonts_dir = landing_dir / "fonts"
    if console_dir.is_dir():
        app.mount("/ui/assets", StaticFiles(directory=console_dir), name="ui-assets")
    if fonts_dir.is_dir():
        app.mount("/ui/fonts", StaticFiles(directory=fonts_dir), name="ui-fonts")
        app.mount("/fonts", StaticFiles(directory=fonts_dir), name="landing-fonts")

    def _auth(request: Request) -> None:
        header = request.headers.get("authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        expected = (settings.deploy_token or "").strip()
        # Empty expected refuses every write — even a typed Bearer. No open-hearth mode.
        if not expected:
            raise HTTPException(status_code=401, detail="deploy token required")
        try:
            matched = hmac.compare_digest(token, expected)
        except ValueError:
            matched = False
        if not matched:
            raise HTTPException(status_code=401, detail="deploy token required")

    def _receipt(result: dict[str, Any], capability_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "result": result,
            "provider_pubkey": signer.public_key_b64,
            "signature": signer.sign_result(
                result,
                capability_id=capability_id,
                product_id=PRODUCT_ID,
                input_payload=payload,
            ),
        }

    @app.get("/")
    def landing() -> FileResponse:
        index = landing_dir / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="landing missing")
        return FileResponse(index)

    @app.get("/hearth.js")
    def landing_hearth_js() -> FileResponse:
        script = landing_dir / "hearth.js"
        if not script.is_file():
            raise HTTPException(status_code=404, detail="landing missing")
        return FileResponse(script, media_type="text/javascript")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "hestia",
            "version": __version__,
            "runtime": settings.runtime,
            "profile": settings.profile,
            "ledger": store.backend_type,
            "payments": "on" if settings.payments_enabled else "off",
            "tenants": store.count(),
        }

    @app.get("/.well-known/ai-market.json")
    def well_known() -> dict[str, Any]:
        return {
            "protocol": "aimarket/2",
            "name": "HESTIA",
            "hub_url": settings.public_base,
            # The crawler pins a peer by `signer_public_key` and finds the
            # catalogue through `manifest_url`. Without them a hub cannot pin
            # this hearth, and an unpinned peer is never indexed.
            "signer_public_key": signer.public_key_b64,
            "manifest_url": f"{settings.public_base}/ai-market/v2/manifest",
            "provider_pubkey": signer.public_key_b64,
            "capabilities": [c["capability_id"] for c in CAPS],
        }

    def _tenant_tools() -> list[dict[str, Any]]:
        """Running tenants, in the shape a hub indexes.

        A hub stores no per-tool URL: it routes by calling this peer's single
        invoke endpoint with a capability_id, so every tool advertises the same
        `invoke_url` and dispatch happens here.

        The gate is "running", not "announced". `/v1/hearth` already publishes
        every running tenant, so gating the manifest on a hub handshake would
        only make two public views of this hearth disagree — and `announced`
        cannot even be set on a hearth with no HESTIA_HUB_URL, which is how the
        reference host is configured. What a hub does with this document is
        still its own decision: it indexes a peer only after the operator there
        pins it.
        """
        tools = []
        for row in store.list_running():
            cap = dict(row.capability)
            cap.pop("provider_pubkey", None)
            tools.append(
                {
                    **cap,
                    "invoke_url": f"{settings.public_base}/ai-market/v2/invoke",
                    "hearth_url": row.public_url,
                    "provider_pubkey": signer.public_key_b64,
                    "payout_address": row.payout_address,
                    "publisher_id": row.payout_address or cap.get("publisher_id") or "hestia",
                }
            )
        return tools

    @app.get("/ai-market/v2/manifest")
    def manifest() -> dict[str, Any]:
        tools = []
        for cap in CAPS:
            tools.append(
                {
                    **cap,
                    "product_id": PRODUCT_ID,
                    "invoke_url": f"{settings.public_base}/ai-market/v2/invoke",
                    "publisher_id": "hestia",
                    "provider_pubkey": signer.public_key_b64,
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                }
            )
        tools.extend(_tenant_tools())
        document: dict[str, Any] = {
            "protocol": "aimarket/2",
            "protocol_version": "v2",
            "name": "HESTIA",
            "hub_url": settings.public_base,
            "total_capabilities": len(tools),
            "capabilities_count": len(tools),
            # Freshness is signed: a hub rejects a replayed manifest by its age.
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tools": tools,
            "by_hub": {},
        }
        document["signature"] = signer.sign_manifest(document)
        return document

    @app.get("/ai-market/v2/search")
    def search(q: str = "") -> dict[str, Any]:
        needle = q.lower().strip()
        tools = [
            t
            for t in manifest()["tools"]
            if not needle
            or needle in t["capability_id"]
            or needle in t["name"].lower()
            or needle in t["description"].lower()
        ]
        return {"total": len(tools), "tools": tools}

    @app.get("/v1/hearth")
    def hearth() -> dict[str, Any]:
        rows = [row.public_dict() for row in store.list_running()]
        return {"ok": True, "hearth": settings.public_base, "tenants": rows}

    @app.get("/ui/")
    def ui() -> FileResponse:
        index = console_dir / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="console missing")
        return FileResponse(index)

    @app.get("/ui")
    def ui_redirect() -> FileResponse:
        return ui()

    @app.get("/ui/admin/")
    def ui_admin() -> FileResponse:
        page = console_dir / "admin.html"
        if not page.is_file():
            raise HTTPException(status_code=404, detail="admin console missing")
        return FileResponse(page)

    @app.get("/ui/admin")
    def ui_admin_redirect() -> FileResponse:
        return ui_admin()

    @app.get("/v1/admin/overview")
    def admin_overview(request: Request) -> dict[str, Any]:
        _auth(request)
        rows = store.list_all()
        running = sum(1 for row in rows if row.status == "running")
        stopped = sum(1 for row in rows if row.status == "stopped")
        return {
            "ok": True,
            "hearth": settings.public_base,
            "runtime": settings.runtime,
            "tenants": len(rows),
            "running": running,
            "stopped": stopped,
        }

    @app.post("/v1/tenants")
    async def deploy(request: Request) -> dict[str, Any]:
        _auth(request)
        payload = await request.json()
        try:
            body = DeployRequest.model_validate(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _deploy(body)

    def _deploy(body: DeployRequest) -> dict[str, Any]:
        if store.count() >= settings.max_tenants and store.get(body.slug) is None:
            raise HTTPException(status_code=429, detail="hearth is full")
        handler = ""
        digest = ""
        if isinstance(body.source, TemplateSource):
            if settings.require_sandbox:
                raise HTTPException(
                    status_code=400,
                    detail="HESTIA_REQUIRE_SANDBOX refuses template-handler stub tenants",
                )
            handler = body.source.handler
            if handler.strip():
                try:
                    admit_handler(handler)
                except HandlerDenied as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            digest = body.source.image_digest
            if digest not in settings.allow_image_digests:
                raise HTTPException(status_code=400, detail="image digest is not allowlisted")
            if settings.runtime != "docker":
                raise HTTPException(status_code=400, detail="pinned images require HESTIA_RUNTIME=docker")

        if settings.themis_url:
            try:
                themis_admit(
                    url=settings.themis_url,
                    capability=body.capability,
                    admit_review=settings.admit_review,
                )
            except AdmissionDenied as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc

        existing = store.get(body.slug)
        if existing and existing.status == "running":
            previous = docker if existing.source_kind == "image" else stub
            previous.stop(tenant_handle(existing.source_kind, body.slug))

        runtime = docker if digest else stub
        try:
            running = runtime.start(
                slug=body.slug,
                capability=body.capability,
                handler=handler,
                image_digest=digest,
                profile=profile,
                env={"HESTIA_TENANT_MAX_BODY_BYTES": str(settings.max_body_bytes)},
            )
        except Exception as exc:
            # The old tenant is already stopped. Leaving the row "running" would
            # keep a dead listen_url on the public roster and behind the edge.
            if existing is not None:
                store.set_status(body.slug, "stopped", error=f"start failed: {exc}"[:500])
            raise HTTPException(status_code=502, detail=f"tenant failed to start: {exc}") from exc
        public_url = f"{settings.public_base}/t/{body.slug}"
        now = time.time()
        tenant = TenantRow(
            slug=body.slug,
            status="running",
            capability=body.capability.model_dump(),
            source_kind=body.source.kind,
            owner_pubkey=body.owner_pubkey,
            listen_url=running.listen_url,
            public_url=public_url,
            announced=0,
            note=body.note,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            last_error="",
            image_digest=digest,
            payout_address=body.payout_address,
        )
        announced = None
        if body.announce or settings.auto_announce:
            if not settings.hub_url:
                tenant.last_error = "announce requested but HESTIA_HUB_URL is empty"
            else:
                try:
                    announced = hub_announce(hub_url=settings.hub_url, hearth_url=settings.public_base)
                    tenant.announced = 1
                except AnnounceError as exc:
                    tenant.last_error = str(exc)
        store.upsert(tenant)
        return {
            "ok": True,
            "slug": body.slug,
            "status": tenant.status,
            "public_url": public_url,
            "invoke_url": f"{public_url}/invoke",
            "announced": bool(tenant.announced),
            "announce": announced,
            "note": (
                "This is a hearth URL, not a Hub listing. "
                "The Hub catalogue only changes after a successful announce."
            ),
        }

    @app.get("/v1/tenants")
    def list_tenants(request: Request) -> dict[str, Any]:
        _auth(request)
        rows = []
        for tenant in store.list_all():
            item = asdict(tenant)
            item.pop("listen_url", None)
            rows.append(item)
        return {"ok": True, "tenants": rows}

    @app.get("/v1/tenants/{slug}")
    def get_tenant(slug: str) -> dict[str, Any]:
        row = store.get(slug)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown slug")
        return {"ok": True, "tenant": row.public_dict() | {"status": row.status, "note": row.note}}

    @app.post("/v1/tenants/{slug}/stop")
    def stop_tenant(slug: str, request: Request) -> dict[str, Any]:
        _auth(request)
        row = store.get(slug)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown slug")
        runtime = docker if row.source_kind == "image" else stub
        runtime.stop(tenant_handle(row.source_kind, slug))
        store.set_status(slug, "stopped")
        return {"ok": True, "slug": slug, "status": "stopped"}

    @app.post("/v1/tenants/{slug}/announce")
    def announce_tenant(slug: str, request: Request) -> dict[str, Any]:
        _auth(request)
        row = store.get(slug)
        if row is None or row.status != "running":
            raise HTTPException(status_code=404, detail="running tenant required")
        if not settings.hub_url:
            raise HTTPException(status_code=400, detail="HESTIA_HUB_URL is empty")
        try:
            body = hub_announce(hub_url=settings.hub_url, hearth_url=settings.public_base)
        except AnnounceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        store.set_status(slug, "running", announced=1)
        return {"ok": True, "announce": body}

    @app.post("/ai-market/v2/invoke")
    async def invoke(request: Request) -> dict[str, Any]:
        payload = await request.json()
        cap = str(payload.get("capability_id") or "")
        inner = payload.get("input") if isinstance(payload.get("input"), dict) else payload
        if cap == "hestia.hearth.list@v1":
            return _receipt(hearth(), cap, inner)
        if cap in {"hestia.host.deploy@v1", "hestia.host.stop@v1"}:
            _auth(request)
        if cap == "hestia.host.deploy@v1":
            try:
                body = DeployRequest.model_validate(inner)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return _receipt(_deploy(body), cap, inner)
        if cap == "hestia.host.status@v1":
            slug = str(inner.get("slug") or "")
            row = store.get(slug)
            if row is None:
                raise HTTPException(status_code=404, detail="unknown slug")
            return _receipt(row.public_dict(), cap, inner)
        if cap == "hestia.host.stop@v1":
            slug = str(inner.get("slug") or "")
            return _receipt(stop_tenant(slug, request), cap, inner)
        # A hub routes by capability_id against this one endpoint, so anything a
        # listed tenant advertises has to be dispatched to that tenant here. Its
        # own Ed25519 envelope is returned untouched: the receipt names the
        # provider that did the work, not the hearth that carried the call.
        tenant = _tenant_for_capability(cap)
        if tenant is not None:
            claim = None
            terms = _terms_for(tenant)
            if terms is not None:
                refusal, claim = _settle(
                    tenant,
                    terms,
                    request.headers.get("x-payment", "").strip(),
                    request.headers.get("x-payment-nonce", "").strip(),
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
        """
        resource = f"{row.public_url}/invoke"
        accept = terms.as_x402_accept()
        accept["resource"] = resource
        nonce = "0x" + secrets.token_hex(32)
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
        if settings.payment_require_binding:
            how = (
                f"pay {terms.amount_usd} {terms.token} to {terms.pay_to} on "
                f"{terms.chain} with the EIP-3009 call "
                f"transferWithAuthorization(..., nonce={nonce}), then retry with "
                "headers X-Payment: <tx hash> and X-Payment-Nonce: "
                f"{nonce}. Signing that nonce is what binds the payment to this "
                "call; a plain transfer is refused because it could have been "
                "made for something else. The hearth holds no funds and needs no "
                "key: the token contract verifies your signature on chain."
            )
        else:
            how = (
                f"send {terms.amount_usd} {terms.token} to {terms.pay_to} on "
                f"{terms.chain}, then retry with header X-Payment: <tx hash>. "
                "The hearth holds no funds; this pays the tenant owner directly."
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
                "binding": "eip3009" if settings.payment_require_binding else "none",
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
        row: TenantRow, terms: PaymentTerms, tx_hash: str, nonce: str = ""
    ) -> tuple[JSONResponse | None, Claim | None]:
        """(refusal, claim). refusal is None when the call may proceed; the claim
        names what to release if the tenant then fails on the platform's side."""
        if not tx_hash:
            return _payment_required(row, terms, "this capability is priced"), None

        invoice = None
        nonce = (nonce or "").strip().lower()
        if settings.payment_require_binding:
            # The nonce identifies WHICH call this payment settles. Without it the
            # payment is just "a transfer to that address", which a transfer made
            # for something else also satisfies.
            if not is_nonce(nonce):
                return _payment_required(
                    row, terms,
                    "send X-Payment-Nonce with the nonce from this 402 and pay with "
                    "transferWithAuthorization signed over it",
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
                require_nonce=nonce if settings.payment_require_binding else "",
            )
        except PaymentError as exc:
            return _payment_required(row, terms, str(exc)), None
        except httpx.HTTPError:
            # The chain is unreachable. Serving the call would give the work away;
            # claiming it was paid would be a lie. Refuse, and say which.
            raise HTTPException(
                status_code=503, detail="payment verifier unavailable"
            ) from None
        if settings.payment_require_binding and not store.consume_invoice(
            nonce, settled["tx_hash"]
        ):
            # Two calls raced on one nonce; the database picked the winner.
            return _payment_required(
                row, terms, "that payment nonce has already been spent"
            ), None
        if not store.claim_payment(
            tx_hash=settled["tx_hash"], slug=row.slug, chain=terms.chain,
            token=terms.token, paid_units=settled["paid_units"], pay_to=terms.pay_to,
        ):
            if settings.payment_require_binding:
                store.release_invoice(nonce)
            return _payment_required(
                row, terms, "that payment has already been spent on another call"
            ), None
        return None, Claim(tx_hash=settled["tx_hash"], nonce=nonce)

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
        if not capability_id:
            return None
        for row in store.list_running():
            if row.capability.get("capability_id") == capability_id:
                return row
        return None

    async def _invoke_tenant(
        row: TenantRow, payload: dict[str, Any], claim: Claim | None = None
    ) -> dict[str, Any]:
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

    @app.api_route("/t/{slug}/{path:path}", methods=["GET", "POST"])
    async def edge(slug: str, path: str, request: Request) -> Response:
        row = store.get(slug)
        if row is None or row.status != "running":
            raise HTTPException(status_code=404, detail="tenant is not running")
        # Only the paid door is gated. /health stays free so a buyer can see a
        # tenant is alive before paying it anything.
        claim = None
        if path == "invoke" and request.method == "POST":
            terms = _terms_for(row)
            if terms is not None:
                refusal, claim = _settle(
                    row,
                    terms,
                    request.headers.get("x-payment", "").strip(),
                    request.headers.get("x-payment-nonce", "").strip(),
                )
                if refusal is not None:
                    return refusal
        target = f"{row.listen_url.rstrip('/')}/{path}"
        query = request.url.query
        if query:
            # Already percent-encoded by the caller; dropping it silently blanked
            # every query parameter a tenant was handed.
            target = f"{target}?{query}"
        raw = await request.body()
        headers = edge_forward_headers(request.headers)
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                async with client.stream(
                    request.method, target, content=raw, headers=headers
                ) as streamed:
                    body = await _read_capped(streamed)
                    media = streamed.headers.get("content-type", "application/json")
                    status = streamed.status_code
        except httpx.HTTPError as exc:
            # The tenant is unreachable — the platform's fault, not the caller's.
            # A payment already claimed for this call is released so the same
            # transaction can be retried once the tenant is back.
            _release_claim(claim)
            raise HTTPException(status_code=502, detail="tenant unreachable") from exc
        except HTTPException:
            # An oversized tenant response (from _read_capped) delivered nothing —
            # release the claim, same as an unreachable tenant.
            _release_claim(claim)
            raise
        # A provider-side failure (5xx that is not the per-call deadline) gave the
        # buyer nothing, so the claim is released; a 4xx (the caller's input was
        # rejected) and a 504 (the call ran to its budget on that input) are billed.
        _release_on_platform_failure(claim, status)
        return Response(
            content=body,
            status_code=status,
            media_type=media,
        )



    app.state.settings = settings
    app.state.store = store
    app.state.stub = stub
    return app
