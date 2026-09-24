"""HESTIA control plane and invoke edge."""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

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
from hestia.runtime.stub import StubRuntime, tenant_image_ref
from hestia.scan import HandlerDenied, admit_handler
from hestia.signing import ProviderSigner
from hestia.store import QUARANTINED, TenantRow, TenantStore
from hestia.themis import AdmissionDenied
from hestia.themis import admit as themis_admit

logger = logging.getLogger(__name__)

PRODUCT_ID = "hestia"
# Longest a deploy/stop/reconcile waits for another one to finish before 409.
LIFECYCLE_WAIT_S = 15.0
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


def quarantine_image_rows_without_docker(store: TenantStore) -> int:
    """A stub hearth never calls docker, so it cannot manage an image tenant.

    Its docker CLI talks to whatever the default context is (often the host
    socket), and the host-socket opt-in is only checked for HESTIA_RUNTIME=docker.
    So under stub an image row is neither served nor touched: it is quarantined,
    which a later docker boot verifies and restores. Nothing is deleted.
    """
    count = 0
    for row in store.list_running():
        if row.source_kind != "image":
            continue
        store.set_status(
            row.slug,
            QUARANTINED,
            error="image tenant needs HESTIA_RUNTIME=docker; not served, container left untouched",
        )
        count += 1
    if count:
        logger.warning("quarantined %d image tenant(s): this hearth runs the stub runtime", count)
    return count


def reconcile_docker_tenants(store: TenantStore, docker: DockerRuntime, profile) -> dict[str, Any]:
    """Make every image row the ledger would serve match an isolated container.

    Runs at boot and on POST /v1/admin/reconcile. Per row:
    - running, on its own verified private network: kept, and its listen_url is
      re-read from the engine (an address recorded long ago can be stale);
    - anything else (on the former shared bridge, exited, missing, or on a
      network that fails the ownership checks): migrated. Every such container is
      removed before any is started again, so no tenant still on the shared bridge
      can reach one that has already moved.
    Then the former shared bridge is taken apart, including containers the
    ledger no longer reaches.

    A row that cannot be verified or restored is quarantined with the reason and
    retried next time. One bad container or an unreachable engine never stops the
    hearth: that took every stub tenant down with it and locked the operator out
    of the API that could fix it.
    """
    report: dict[str, Any] = {
        "verified": [],
        "restored": [],
        "quarantined": [],
        "legacy_network": None,
    }

    def quarantine(row: TenantRow, reason: str) -> None:
        store.set_status(row.slug, QUARANTINED, error=reason[:500])
        report["quarantined"].append(row.slug)
        logger.warning("tenant %s quarantined: %s", row.slug, reason)

    rows = [
        row
        for row in store.list_all()
        if row.source_kind == "image" and row.status in {"running", QUARANTINED}
    ]
    # One short probe first. A hung engine would otherwise cost a long timeout
    # per tenant, all before the hearth serves anything.
    problem = docker.engine_problem()
    if problem:
        for row in rows:
            quarantine(row, f"cannot verify isolation: the Docker engine did not answer: {problem}")
        report["legacy_network"] = {"errors": [f"engine did not answer: {problem}"]}
        return report

    to_migrate: list[TenantRow] = []
    for row in rows:
        if row.image_digest not in docker.allow_digests:
            # The operator withdrew this image. It must not keep running just
            # because its container outlived the plane.
            try:
                docker.stop(tenant_handle(row.source_kind, row.slug))
            except Exception as exc:  # noqa: BLE001
                quarantine(row, f"image digest is no longer allowlisted; removal failed: {exc}")
                continue
            quarantine(row, "image digest is no longer on HESTIA_ALLOW_IMAGE_DIGESTS; container removed")
            continue
        try:
            state = docker.tenant_state(row.slug)
        except Exception as exc:  # noqa: BLE001 — per-row, never the whole hearth
            quarantine(row, f"cannot verify isolation: {exc}")
            continue
        if (
            state is not None
            and state.running
            and state.isolated
            and state.image == tenant_image_ref(row.image_digest)
        ):
            store.set_running(row.slug, state.listen_url)
            report["verified"].append(row.slug)
        else:
            to_migrate.append(row)

    stopped: list[TenantRow] = []
    for row in to_migrate:
        try:
            docker.stop(tenant_handle(row.source_kind, row.slug))
        except Exception as exc:  # noqa: BLE001
            quarantine(row, f"cannot remove the unisolated container: {exc}")
            continue
        stopped.append(row)

    try:
        legacy = docker.retire_legacy_network()
    except Exception as exc:  # noqa: BLE001
        legacy = {"errors": [str(exc)]}
    report["legacy_network"] = legacy
    if legacy.get("disconnected") or legacy.get("errors"):
        logger.warning("former shared tenant network: %s", legacy)

    for row in stopped:
        try:
            running = docker.start(
                slug=row.slug,
                capability=CapabilitySpec.model_validate(row.capability),
                handler="",
                image_digest=row.image_digest,
                profile=profile,
                env={},
            )
        except Exception as exc:  # noqa: BLE001
            quarantine(row, f"restore on a private network failed: {exc}")
            continue
        store.set_running(row.slug, running.listen_url)
        report["restored"].append(row.slug)

    if report["restored"] or report["quarantined"]:
        logger.info(
            "docker tenants: %d verified, %d restored, %d quarantined",
            len(report["verified"]),
            len(report["restored"]),
            len(report["quarantined"]),
        )
    return report


# How long a stopped tenant's address stays off the allocator. A request the edge
# routed just before the stop (still uploading its body, or waiting on the chain
# to settle a payment) must not land on the next tenant given that /29.
RELEASED_ADDRESS_GRACE_S = 600.0


def reserved_tenant_hosts(store: TenantStore, slug: str) -> list[str]:
    """Addresses the edge could still be sent to for OTHER image tenants."""
    hosts = []
    recent = time.time() - RELEASED_ADDRESS_GRACE_S
    for row in store.list_all():
        if row.slug == slug or row.source_kind != "image":
            continue
        if row.status == "stopped" and row.updated_at < recent:
            continue
        host = urlsplit(row.listen_url or "").hostname
        if host:
            hosts.append(host)
    return hosts


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
    restore_stub_tenants(store, stub, profile)
    # Docker containers outlive the plane, so image rows are checked against the
    # engine: verified, moved onto a private network, or quarantined. Only a
    # docker hearth may call docker at all — the host-socket opt-in is checked
    # for HESTIA_RUNTIME=docker only, so a stub hearth's CLI is ungated.
    docker: DockerRuntime | None = None
    if settings.runtime == "docker":
        docker = DockerRuntime(
            settings.docker_bin,
            settings.allow_image_digests,
            client_env=settings.docker_client_env(),
            oci_runtime=settings.docker_runtime,
            require_gvisor=settings.require_gvisor,
            network_prefix=settings.tenant_network,
            subnet_pool=settings.tenant_subnet_pool,
            reserved_hosts=lambda slug: reserved_tenant_hosts(store, slug),
        )
        reconcile_docker_tenants(store, docker, profile)
    else:
        quarantine_image_rows_without_docker(store)
    # Deploy, stop and reconcile change what runs and what the ledger says about
    # it. They run in worker threads and take this lock, so a stop can never land
    # between a start and the row that records it.
    lifecycle_lock = threading.RLock()

    @contextmanager
    def lifecycle():
        # Bounded: a request waiting here holds a threadpool worker, and enough
        # of them queued behind one slow operation would starve every sync route.
        if not lifecycle_lock.acquire(timeout=LIFECYCLE_WAIT_S):
            raise HTTPException(
                status_code=409,
                detail="another deploy, stop or reconcile is in progress; retry shortly",
            )
        try:
            yield
        finally:
            lifecycle_lock.release()
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

    def _price_of(capability_id: str) -> float:
        """The listed price for a capability — a host one, or a running tenant's."""
        for cap in CAPS:
            if cap["capability_id"] == capability_id:
                return float(cap.get("price_per_call_usd") or 0.0)
        for row in store.list_running():
            if str(row.capability.get("capability_id") or "") == capability_id:
                return float(row.capability.get("price_per_call_usd") or 0.0)
        return 0.0

    def _receipt(result: dict[str, Any], capability_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """The answer, signed twice, because two different readers ask for it.

        `signature` is the hearth's own: it binds the INPUT hash, which is what a
        buyer disputing an answer needs. `receipt` is the interop shape a hub's
        federation assay verifies against the key in `.well-known` — without it the
        probe reports "response had no receipt object" and the peer is never admitted.
        """
        started = time.time()
        receipt = {
            "nonce": "0x" + secrets.token_hex(16),
            "product_id": PRODUCT_ID,
            "capability_id": capability_id,
            "price_usd": float(_price_of(capability_id)),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "success": 1,
            "latency_ms": int((time.time() - started) * 1000),
        }
        receipt["signature"] = signer.sign_hub_receipt(receipt)
        return {
            "ok": True,
            "result": result,
            "provider_pubkey": signer.public_key_b64,
            "receipt": receipt,
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
        document: dict[str, Any] = {
            "protocol": "aimarket/2",
            # A hub validates this document against a schema that REQUIRES the plural
            # list. Publishing only the singular `protocol` failed `well_known_schema`
            # on the federation assay ("'protocol_versions' is a required property"),
            # and a failed assay is never approved, so the hearth's agents could not
            # reach a catalogue however well the manifest itself was formed.
            "protocol_versions": ["v2"],
            "name": "HESTIA",
            "hub_url": settings.public_base,
            # The crawler pins a peer by `signer_public_key` and finds the
            # catalogue through `manifest_url`. Without them a hub cannot pin
            # this hearth, and an unpinned peer is never indexed.
            "signer_public_key": signer.public_key_b64,
            "manifest_url": f"{settings.public_base}/ai-market/v2/manifest",
            # Hub routing reads `mcp_endpoint` as "POST the v2 envelope here".
            # Without it a Hub falls back to `/capabilities/{product}/{cap}/invoke`,
            # which this host does not serve — a paid catalogue sale then 502s
            # after the seller has already been paid on-chain.
            "mcp_endpoint": f"{settings.public_base}/ai-market/v2/invoke",
            "provider_pubkey": signer.public_key_b64,
            "capabilities": [c["capability_id"] for c in CAPS],
            # Pending-hub rows show this number as "claimed"; absent, every hub
            # displayed "claimed 0" for a hearth that sells host + tenant tools.
            "capabilities_count": len(CAPS) + len(store.list_running()),
        }
        # Signed whole, hybrid when HESTIA_PQC is on: a hub pins the PQ key out of
        # this block, and relays a peer's gossip only when the document verifies.
        document["signature"] = signer.sign_object(document)
        return document

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
        quarantined = [row.slug for row in rows if row.status == QUARANTINED]
        return {
            "ok": True,
            "hearth": settings.public_base,
            "runtime": settings.runtime,
            "tenants": len(rows),
            "running": running,
            "stopped": stopped,
            "quarantined": len(quarantined),
            "quarantined_slugs": quarantined,
        }

    @app.post("/v1/admin/reconcile")
    def admin_reconcile(request: Request) -> dict[str, Any]:
        """Retry quarantined image tenants without restarting the hearth."""
        _auth(request)
        if docker is None:
            raise HTTPException(status_code=400, detail="reconcile needs HESTIA_RUNTIME=docker")
        with lifecycle():
            return {"ok": True, **reconcile_docker_tenants(store, docker, profile)}

    @app.post("/v1/tenants")
    async def deploy(request: Request) -> dict[str, Any]:
        _auth(request)
        payload = await request.json()
        try:
            body = DeployRequest.model_validate(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Off the event loop: a deploy waits on docker/stub start for seconds, and
        # the whole hearth (edge included) used to freeze for that long.
        return await run_in_threadpool(_deploy, body)

    def _runtime_for(row: TenantRow) -> StubRuntime | DockerRuntime | None:
        """The runtime that manages this row, or None when this hearth cannot."""
        if row.source_kind == "image":
            return docker
        return stub

    def _deploy(body: DeployRequest) -> dict[str, Any]:
        with lifecycle():
            result = _deploy_locked(body)
        # The hub call can take seconds. It runs after the row is written and
        # outside the lock, and only records success if the tenant still runs.
        if body.announce or settings.auto_announce:
            if not settings.hub_url:
                store.record_error(body.slug, "announce requested but HESTIA_HUB_URL is empty")
            else:
                try:
                    result["announce"] = hub_announce(
                        hub_url=settings.hub_url, hearth_url=settings.public_base
                    )
                    result["announced"] = store.mark_announced(body.slug)
                except AnnounceError as exc:
                    store.record_error(body.slug, str(exc))
        return result

    def _deploy_locked(body: DeployRequest) -> dict[str, Any]:
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
        stopped_container = False
        if existing and existing.status in {"running", QUARANTINED}:
            # Unservable BEFORE the old runtime stops. The edge reads the ledger
            # from other threads, and until the new row is written the old
            # address (a freed loopback port, a removed container's IP) could be
            # bound by anyone.
            store.set_status(body.slug, "stopped", error="redeploying")
            previous = _runtime_for(existing)
            if previous is None:
                logger.warning(
                    "redeploying %s over an image tenant this stub hearth cannot stop", body.slug
                )
            else:
                try:
                    previous.stop(tenant_handle(existing.source_kind, body.slug))
                    stopped_container = existing.source_kind == "image"
                except (RuntimeError, OSError) as exc:
                    store.set_status(
                        body.slug, existing.status, error=f"stop before redeploy failed: {exc}"[:500]
                    )
                    raise HTTPException(
                        status_code=502, detail=f"could not stop the running tenant: {exc}"
                    ) from exc
        if digest and docker is not None and not stopped_container:
            # A container can outlive a row already marked stopped: a stub hearth
            # marks one without touching docker, and an older `rm` could fail.
            try:
                docker.stop(tenant_handle("image", body.slug))
            except (RuntimeError, OSError) as exc:
                raise HTTPException(
                    status_code=502, detail=f"could not remove the previous container: {exc}"
                ) from exc

        runtime = docker if digest else stub
        if runtime is None:  # an image digest passed the checks above only under docker
            raise HTTPException(status_code=400, detail="pinned images require HESTIA_RUNTIME=docker")
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
        store.upsert(tenant)
        return {
            "ok": True,
            "slug": body.slug,
            "status": tenant.status,
            "public_url": public_url,
            "invoke_url": f"{public_url}/invoke",
            "announced": False,
            "announce": None,
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
        with lifecycle():
            row = store.get(slug)
            if row is None:
                raise HTTPException(status_code=404, detail="unknown slug")
            runtime = _runtime_for(row)
            note = ""
            if runtime is None:
                # A stub hearth never calls docker (see build_app). The row stops
                # being served; the container, if any, is the operator's to remove.
                note = "image tenant marked stopped; this stub hearth did not touch its container"
            else:
                try:
                    runtime.stop(tenant_handle(row.source_kind, slug))
                except (RuntimeError, OSError) as exc:
                    store.set_status(slug, row.status, error=f"stop failed: {exc}"[:500])
                    raise HTTPException(
                        status_code=502, detail=f"tenant did not stop: {exc}"
                    ) from exc
            store.set_status(slug, "stopped")
        body: dict[str, Any] = {"ok": True, "slug": slug, "status": "stopped"}
        if note:
            body["note"] = note
        return body

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
        if not store.mark_announced(slug):
            raise HTTPException(
                status_code=409, detail="tenant stopped while it was being announced"
            )
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
            return _receipt(await run_in_threadpool(_deploy, body), cap, inner)
        if cap == "hestia.host.status@v1":
            slug = str(inner.get("slug") or "")
            row = store.get(slug)
            if row is None:
                raise HTTPException(status_code=404, detail="unknown slug")
            return _receipt(row.public_dict(), cap, inner)
        if cap == "hestia.host.stop@v1":
            slug = str(inner.get("slug") or "")
            return _receipt(await run_in_threadpool(stop_tenant, slug, request), cap, inner)
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

    def _still_serving(row: TenantRow) -> bool:
        current = store.get(row.slug)
        return (
            current is not None
            and current.status == "running"
            and current.listen_url == row.listen_url
        )

    async def _invoke_tenant(
        row: TenantRow, payload: dict[str, Any], claim: Claim | None = None
    ) -> dict[str, Any]:
        if not _still_serving(row):
            _release_claim(claim)
            raise HTTPException(status_code=503, detail="tenant changed during the request; retry")
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
        if not _still_serving(row):
            # The row was read before the body arrived and before any payment
            # settled. If the tenant was stopped or moved meanwhile, its old
            # address may already belong to someone else.
            _release_claim(claim)
            raise HTTPException(status_code=503, detail="tenant changed during the request; retry")
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
