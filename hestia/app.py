"""HESTIA control plane and invoke edge."""

from __future__ import annotations

import hmac
import json
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

from hestia import __version__, domains
from hestia.compute import (
    COMPUTE_CAPS,
    COMPUTE_RUN,
    COMPUTE_VERIFY,
    METHODS,
    OK,
    PLATFORM_FAILURE,
    REPLICAS,
    ComputeRefused,
    Limits,
    ReplicaSlots,
    build_receipt,
    find_function,
    hub_key_matches,
    judge,
    parse_request,
    published_functions,
    refusal_body,
    run_replicas,
    runtime_facts,
    settings_problem,
)
from hestia.compute import price_of as compute_price_of
from hestia.config import Settings
from hestia.hub import AnnounceError
from hestia.hub import announce as hub_announce
from hestia.hub_billing import ACCOUNT_RE, billing_block, hub_for_key
from hestia.limits import BodyLimitMiddleware
from hestia.listing import HELD, LISTED
from hestia.listing import review as review_listing
from hestia.models import CapabilitySpec, DeployRequest, TemplateSource
from hestia.names import (
    CLOSED,
    DOMAIN,
    FAMILY,
    KINDS,
    OPERATOR,
    PREFIX,
    PUBLISHER,
    RESERVABLE,
    NameTaken,
    claim_domain,
    claims_for,
    domains_of,
    holder_of,
    key_for,
    seed_prefixes,
    users_of,
)
from hestia.names import backfill as backfill_names
from hestia.names import claim as claim_names
from hestia.names import reverse as reverse_domain
from hestia.owners import (
    OwnerProofError,
    canonical_owner_key,
    has_owner_headers,
    verify_owner_request,
)
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
from hestia.policy import profile_from_settings
from hestia.runtime.docker import DockerRuntime
from hestia.runtime.stub import StubRuntime, tenant_image_ref
from hestia.runtime.wasm import WasmRuntime, is_wasm
from hestia.scan import HandlerDenied, admit_handler, lint_wasm_handler
from hestia.signing import ProviderSigner
from hestia.store import LIVE, QUARANTINED, TenantRow, TenantStore
from hestia.tenant_stub import call_timeout_s
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
    problem = settings_problem(settings)
    if problem:
        raise RuntimeError(problem)
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = TenantStore.open(data_dir=settings.data_dir, database_url=settings.database_url)
    signer = ProviderSigner(settings.data_dir / "provider.key")
    profile = profile_from_settings(settings)
    # Names (hestia/names.py) are claimed at deploy. Rows written before that are claimed
    # here, oldest first; a row whose name another holder already had is not served again.
    seed_prefixes(store, settings.reserved_prefixes)
    for slug, reason in backfill_names(store):
        row = store.get(slug)
        if row is not None and row.status in LIVE:
            logger.warning("not restoring %s: %s", slug, reason)
            store.set_status(slug, "stopped", error=f"boot check: {reason}"[:500])
    # HESTIA_RUNTIME=wasm: template tenants have no process; their handlers run in the
    # WebAssembly sandbox runner (hestia.wasm_runner), the boundary the stub never was.
    stub: StubRuntime | WasmRuntime = (
        WasmRuntime(settings.data_dir, settings.runner_socket, timeout_s=float(call_timeout_s()),
                    open_slots=settings.open_lane_slots, open_wait_s=settings.open_lane_wait_s)
        if settings.runtime == "wasm"
        else StubRuntime(settings.data_dir)
    )
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

    compute_limits = Limits.from_settings(settings)
    compute_slots = ReplicaSlots(settings.compute_max_concurrent)
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

    @dataclass(frozen=True)
    class Principal:
        """Who is asking: the operator (deploy token) or one tenant owner (hestia/owners.py)."""

        kind: str                 # "operator" | "owner"
        owner: str = ""           # canonical owner key
        admitted: bool = True     # False: an open-mode owner on its first request
        may_run_code: bool = True

    def _principal(request: Request, body: bytes) -> Principal:
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer ") or not has_owner_headers(request.headers):
            _auth(request)
            return Principal("operator")
        try:
            proof = verify_owner_request(
                headers=request.headers, hearth=settings.public_base, method=request.method,
                path=request.url.path, body=body, skew_s=settings.owner_skew_s,
            )
        except OwnerProofError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        owner = store.get_owner(proof.pubkey)
        if owner is None and not settings.open_owners:
            raise HTTPException(status_code=403, detail="owner is not admitted on this hearth")
        if owner is not None and owner["status"] != "active":
            raise HTTPException(status_code=403, detail="owner is suspended on this hearth")
        # Spent before anything is acted on: a replayed request loses here.
        if not store.spend_owner_nonce(proof.pubkey, proof.nonce, keep_s=2 * settings.owner_skew_s):
            raise HTTPException(status_code=409, detail="owner nonce already used")
        if owner is None:
            return Principal("owner", proof.pubkey, admitted=False, may_run_code=False)
        return Principal("owner", proof.pubkey, may_run_code=bool(owner["may_run_code"]))

    def _owns(principal: Principal, row: TenantRow) -> bool:
        if principal.kind == "operator":
            return True
        try:
            return canonical_owner_key(row.owner_pubkey) == principal.owner
        except OwnerProofError:
            # The placeholder and other small-order keys own nothing: such a tenant is
            # the operator's alone.
            return False

    def _require_owner_of(principal: Principal, row: TenantRow) -> None:
        if not _owns(principal, row):
            raise HTTPException(status_code=403, detail="this agent belongs to another owner")

    def _suspended() -> set[str]:
        """Owners the operator suspended: their agents are neither offered nor served."""
        return {o["pubkey"] for o in store.list_owners() if o["status"] != "active"}

    def _offered(row: TenantRow, suspended: set[str]) -> bool:
        """In the manifest and behind the routed invoke: listed, and its owner not suspended.
        A held agent still answers at its own /t/{slug} door; a suspended owner's does not."""
        return row.listing == LISTED and holder_of(row.owner_pubkey) not in suspended

    def _price_of(capability_id: str) -> float:
        """The listed price for a capability — a host one, or a running tenant's."""
        for cap in CAPS:
            if cap["capability_id"] == capability_id:
                return float(cap.get("price_per_call_usd") or 0.0)
        if settings.compute_enabled and capability_id in COMPUTE_CAPS:
            return compute_price_of(settings, capability_id)
        for row in store.list_running():
            if str(row.capability.get("capability_id") or "") == capability_id:
                return float(row.capability.get("price_per_call_usd") or 0.0)
        return 0.0

    def _receipt(
        result: dict[str, Any],
        capability_id: str,
        payload: dict[str, Any],
        *,
        started: float | None = None,
    ) -> dict[str, Any]:
        """The answer, signed twice, because two different readers ask for it.

        `signature` is the hearth's own: it binds the INPUT hash, which is what a
        buyer disputing an answer needs. `receipt` is the interop shape a hub's
        federation assay verifies against the key in `.well-known` — without it the
        probe reports "response had no receipt object" and the peer is never admitted.

        `started` is the monotonic clock when the request arrived. latency_ms used
        to be measured between two adjacent reads inside this function, so every
        signed receipt claimed ~0 ms however long the work took — a false number
        under the hearth's own signature, and on a compute call (seconds of work)
        an obviously false one.
        """
        elapsed = time.monotonic() - started if started is not None else 0.0
        receipt = {
            "nonce": "0x" + secrets.token_hex(16),
            "product_id": PRODUCT_ID,
            "capability_id": capability_id,
            "price_usd": float(_price_of(capability_id)),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "success": 1,
            "latency_ms": max(0, int(elapsed * 1000)),
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
            "capabilities": [c["capability_id"] for c in CAPS]
            + [c["capability_id"] for c in _compute_caps()],
            # Pending-hub rows show this number as "claimed"; absent, every hub
            # displayed "claimed 0" for a hearth that sells host + tenant tools.
            "capabilities_count": len(CAPS) + len(_compute_caps()) + len(_tenant_tools()),
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
        suspended = _suspended()
        proven = _proven_domains()
        for row in store.list_running():
            if not _offered(row, suspended):
                continue  # runs at its own door; not offered to hubs until reviewed or listed
            cap = dict(row.capability)
            cap.pop("provider_pubkey", None)
            tool = {
                **cap,
                "invoke_url": f"{settings.public_base}/ai-market/v2/invoke",
                "hearth_url": row.public_url,
                "provider_pubkey": signer.public_key_b64,
                "payout_address": row.payout_address,
                "publisher_id": row.payout_address or cap.get("publisher_id") or "hestia",
            }
            owned = proven.get(holder_of(row.owner_pubkey))
            if owned:
                # A domain its owner proved to this hearth (hestia/domains.py): a buyer can
                # check who answers for the agent, which a free-text publisher_id cannot say.
                tool["publisher_domain"] = owned[0]
            tools.append(tool)
        return tools

    def _proven_domains() -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for name in store.names(DOMAIN):
            out.setdefault(name["holder"], []).append(name["name"])
        return {holder: sorted(names) for holder, names in out.items()}

    def _compute_caps() -> list[dict[str, Any]]:
        """The two compute SKUs, only on a hearth that serves them.

        `payout_address` is the dedicated compute wallet: a hub's seller_pay_to
        reads it, so the hub's own 402 names it and the hub stays the only till.
        Without it (hub-key-only hearths) a host cap has no payee on the market
        rail and the hub can only sell it on credits. The published limits are
        the price's terms: the hub bills per call, never per CPU-second, so a
        buyer needs to know what one call can use.
        """
        if not settings.compute_enabled:
            return []
        payee = settings.compute_payout_address
        input_schema = {
            "type": "object",
            "required": ["slug", "input"],
            "properties": {
                "slug": {"type": "string", "description": "a function listed by GET /v1/compute"},
                "function_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                    "description": "refuse the call unless the function is exactly these bytes",
                },
                "input": {"type": "object", "description": "what handle(payload) receives"},
            },
        }
        caps = [
            {
                "capability_id": COMPUTE_RUN,
                "name": "Metered compute (one replica)",
                "description": (
                    "Run an operator-published pure function (GET /v1/compute) once on your "
                    "input, under the published CPU, memory and wall-clock limits. Returns the "
                    "output and a signed receipt binding the function, input and output digests "
                    "to kernel-measured CPU time and peak memory. Your data, never your code."
                ),
                "price_per_call_usd": settings.compute_run_price_usd,
            },
            {
                "capability_id": COMPUTE_VERIFY,
                "name": "Replicated compute (two replicas)",
                "description": (
                    "Run the function twice in parallel with different hash seeds and sign "
                    "only if both produce identical output bytes; otherwise ok:false, "
                    "replicas_disagree, nothing signed. One operator runs both replicas: a "
                    "consistency check, not third-party verification (see docs/COMPUTE.md)."
                ),
                "price_per_call_usd": settings.compute_verify_price_usd,
            },
        ]
        for cap in caps:
            cap.update(
                {
                    "product_id": PRODUCT_ID,
                    "input_schema": input_schema,
                    "output_schema": {"type": "object"},
                    "limits": compute_limits.public(),
                    "replicas": REPLICAS[cap["capability_id"]],
                    "method": METHODS[cap["capability_id"]],
                    "payout_address": payee,
                    "publisher_id": payee or "hestia",
                }
            )
        return caps

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
        for cap in _compute_caps():
            tools.append(
                {
                    **cap,
                    "invoke_url": f"{settings.public_base}/ai-market/v2/invoke",
                    "provider_pubkey": signer.public_key_b64,
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
        owners = {o["pubkey"]: o for o in store.list_owners()}
        proven = _proven_domains()
        rows = []
        for row in store.list_running():
            owner_record = owners.get(holder_of(row.owner_pubkey))
            if owner_record is not None and owner_record["status"] != "active":
                continue  # a suspended owner's agents are not hosted here while suspended
            item = row.public_dict()
            # Whose agent this is: the hearth operator's own (no admitted owner — the reference
            # agents carry the placeholder key), or an admitted owner's, by the label the
            # operator gave it. A dashboard counts "ours" against "third-party" from this.
            item["owner"] = ({"kind": "owner", "label": str(owner_record.get("label") or "")[:80]}
                             if owner_record else {"kind": "operator"})
            if owner_record and proven.get(owner_record["pubkey"]):
                item["owner"]["domains"] = proven[owner_record["pubkey"]]
            rows.append(item)
        by_owner = sum(1 for r in rows if r["owner"]["kind"] == "owner")
        return {"ok": True, "hearth": settings.public_base, "tenants": rows,
                "running": len(rows), "operator_tenants": len(rows) - by_owner,
                "owner_tenants": by_owner}

    @app.get("/v1/compute")
    def compute_catalogue() -> dict[str, Any]:
        """What compute this hearth sells: functions, their digests, limits, prices.

        Free and signed. A buyer pins `function_sha256` from here, and a
        cross-hearth check compares it with another hearth's copy of the same
        function before paying either of them.
        """
        if not settings.compute_enabled:
            return {"ok": True, "enabled": False}
        document: dict[str, Any] = {
            "ok": True,
            "enabled": True,
            "hearth": settings.public_base,
            "provider_pubkey": signer.public_key_b64,
            "capabilities": [
                {
                    "capability_id": cap["capability_id"],
                    "price_per_call_usd": cap["price_per_call_usd"],
                    "replicas": cap["replicas"],
                    "method": cap["method"],
                }
                for cap in _compute_caps()
            ],
            "limits": compute_limits.public(),
            "payout_address": settings.compute_payout_address,
            "same_operator": True,
            **runtime_facts(),
            "functions": [fn.public() for fn in published_functions(settings, store, stub)],
        }
        document["signature"] = signer.sign_object(document)
        return document

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

    @app.post("/v1/admin/tenants/{slug}/listing")
    async def admin_listing(slug: str, request: Request) -> dict[str, Any]:
        """The operator's call on an agent the review held, or one it listed: in or out of the
        public manifest. Body: {"listed": true|false, "reason": "..."}."""
        _auth(request)
        try:
            body = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(body, dict) or not isinstance(body.get("listed"), bool):
            raise HTTPException(status_code=400, detail="send {listed: true|false, reason?}")
        listed = body["listed"]
        reasons = [] if listed else [("operator: " + str(body.get("reason") or "held"))[:300]]
        if not store.set_listing(slug, LISTED if listed else HELD, reasons, by="operator"):
            raise HTTPException(status_code=404, detail="unknown slug")
        return {"ok": True, "slug": slug, "listing": LISTED if listed else HELD}

    @app.get("/v1/admin/names")
    def admin_names(request: Request) -> dict[str, Any]:
        """Every claimed name (hestia/names.py): kind, folded key, holder, as first written."""
        _auth(request)
        return {"ok": True, "names": store.names()}

    async def _name_body(request: Request) -> tuple[str, str, str, dict[str, Any]]:
        try:
            body = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        kind = str((body or {}).get("kind") or "") if isinstance(body, dict) else ""
        name = str(body.get("name") or "").strip() if isinstance(body, dict) else ""
        if kind not in KINDS or not name or len(name) > 120:
            raise HTTPException(status_code=400,
                                detail=f"send {{kind: {'|'.join(KINDS)}, name}} (name up to 120)")
        key = key_for(kind, name)
        if not key:
            raise HTTPException(status_code=400, detail="the name folds to nothing")
        return kind, name, key, body

    @app.post("/v1/admin/names")
    async def admin_reserve_name(request: Request) -> dict[str, Any]:
        """Reserve a name for a holder, or hand a claimed one to another: {kind, name, holder}.
        holder is "operator" or an owner key. kind "prefix" covers every family, product and
        publisher whose folded name starts with it — e.g. a brand, for the owner it belongs to."""
        _auth(request)
        kind, name, key, body = await _name_body(request)
        if kind not in RESERVABLE:
            raise HTTPException(status_code=400, detail="a domain word comes only with its proof: "
                                "POST /v1/admin/owners/domain or /v1/owners/me/domain")
        raw_holder = str(body.get("holder") or OPERATOR).strip()
        try:
            holder = OPERATOR if raw_holder == OPERATOR else canonical_owner_key(raw_holder)
        except OwnerProofError as exc:
            raise HTTPException(status_code=400, detail=f"holder: {exc}") from exc
        if kind == FAMILY and key.startswith(CLOSED):
            raise HTTPException(status_code=400, detail="hestia.* stays this hearth's own")

        def reserve() -> dict[str, Any] | None:
            with lifecycle():
                store.set_name(kind, key, holder, name)
                return store.name_holder(kind, key)

        return {"ok": True, "name": await run_in_threadpool(reserve)}

    @app.post("/v1/admin/names/release")
    async def admin_release_name(request: Request) -> dict[str, Any]:
        """Free a claimed name: {kind, name}. Refused while the holder's live agents still use
        it — stop them first, so a release never leaves two holders serving one name. A domain
        word goes with the domain's publisher name, claimed with it."""
        _auth(request)
        kind, name, key, _ = await _name_body(request)

        def release() -> dict[str, Any]:
            with lifecycle():
                held = store.name_holder(kind, key)
                if held is None:
                    raise HTTPException(status_code=404, detail="no such name is claimed")
                freed = [(kind, key)]
                if kind == DOMAIN:
                    freed.append((PUBLISHER, key_for(PUBLISHER, held["name"])))
                rows = store.list_all()
                for freed_kind, freed_key in freed:
                    if freed_kind in (PREFIX, DOMAIN):
                        continue
                    users = users_of(rows, freed_kind, freed_key, held["holder"])
                    if users:
                        raise HTTPException(
                            status_code=409,
                            detail=f"still served by {', '.join(users)}: stop those first")
                for freed_kind, freed_key in freed:
                    store.delete_name(freed_kind, freed_key, holder=held["holder"])
                return held

        return {"ok": True, "released": await run_in_threadpool(release)}

    @app.post("/v1/admin/reconcile")
    def admin_reconcile(request: Request) -> dict[str, Any]:
        """Retry quarantined image tenants without restarting the hearth."""
        _auth(request)
        if docker is None:
            raise HTTPException(status_code=400, detail="reconcile needs HESTIA_RUNTIME=docker")
        with lifecycle():
            return {"ok": True, **reconcile_docker_tenants(store, docker, profile)}

    @app.post("/v1/owners")
    async def admit_owner(request: Request) -> dict[str, Any]:
        """Admit, update or suspend an owner — the operator's call only."""
        _auth(request)
        try:
            payload = json.loads(await request.body() or b"{}")
            pubkey = canonical_owner_key(str(payload.get("pubkey") or ""))
        except OwnerProofError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="body is not JSON") from exc
        status = str(payload.get("status") or "active")
        if status not in {"active", "suspended"}:
            raise HTTPException(status_code=400, detail="status must be active or suspended")
        try:
            max_tenants = int(payload.get("max_tenants") or settings.owner_max_tenants)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="max_tenants must be an integer") from exc
        if not 1 <= max_tenants <= settings.max_tenants:
            raise HTTPException(
                status_code=400, detail=f"max_tenants must be 1..{settings.max_tenants}"
            )
        # "operator": this key's agents share the runner with the operator's. "open": they
        # share the open lane with every self-admitted key. A key the operator admits is
        # "operator"; updating one keeps what it was unless the operator says otherwise.
        admitted_by = payload.get("admitted_by")
        if admitted_by is not None and admitted_by not in ("operator", "open"):
            raise HTTPException(status_code=400, detail="admitted_by must be operator or open")

        def write() -> dict[str, Any]:
            lane = admitted_by or (None if store.get_owner(pubkey) else "operator")
            return store.admit_owner(
                pubkey=pubkey, label=str(payload.get("label") or "")[:120], status=status,
                max_tenants=max_tenants, may_run_code=bool(payload.get("may_run_code", True)),
                admitted_by=lane,
            )

        owner = await run_in_threadpool(write)
        return {"ok": True, "owner": owner}

    @app.get("/v1/owners")
    def list_owners(request: Request) -> dict[str, Any]:
        _auth(request)
        return {"ok": True, "owners": store.list_owners(),
                "open_owners": settings.open_owners}

    @app.get("/v1/owners/me")
    async def owner_me(request: Request) -> dict[str, Any]:
        """An owner reads its own standing: admitted, quota, which agents are its."""
        if not has_owner_headers(request.headers):
            raise HTTPException(status_code=401, detail="owner signature required")
        principal = await run_in_threadpool(_principal, request, await request.body())
        owner = store.get_owner(principal.owner)
        mine = [row.slug for row in store.list_all() if _owns(principal, row)]
        return {
            "ok": True,
            "owner": principal.owner,
            "admitted": owner is not None,
            "status": (owner or {}).get("status", "not admitted"),
            "max_tenants": int((owner or {}).get("max_tenants") or settings.owner_max_tenants),
            "may_run_code": bool((owner or {}).get("may_run_code", 0)),
            # "open": this owner's agents share the open sandbox lane (a 503 means it was full).
            "admitted_by": (owner or {}).get("admitted_by", ""),
            "tenants": mine,
            "billing": store.owner_billing(principal.owner) if owner else {},
            "hubs_this_hearth_can_bill_for": [url for url, _ in settings.tenant_hub_keys],
        }

    @app.post("/v1/owners/me/billing")
    async def owner_billing(request: Request) -> dict[str, Any]:
        """An owner chooses which hubs may sell its agents, and the account each one credits."""
        if not has_owner_headers(request.headers):
            raise HTTPException(status_code=401, detail="owner signature required")
        raw = await request.body()
        principal = await run_in_threadpool(_principal, request, raw)
        if not principal.admitted:
            raise HTTPException(status_code=403, detail="owner is not admitted on this hearth")
        try:
            hubs = json.loads(raw or b"{}").get("hubs")
        except (ValueError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail="body must be {\"hubs\": {url: account}}") from exc
        if not isinstance(hubs, dict):
            raise HTTPException(status_code=400, detail="body must be {\"hubs\": {url: account}}")
        known = {url for url, _ in settings.tenant_hub_keys}
        billing = store.owner_billing(principal.owner)
        for url, account in hubs.items():
            url = str(url).rstrip("/")
            if url not in known:
                raise HTTPException(status_code=400,
                                    detail=f"this hearth holds no key for {url}; it cannot sell here")
            account = str(account or "").strip()
            if account and not ACCOUNT_RE.match(account):
                raise HTTPException(status_code=400,
                                    detail="account must be a hub credit account id (acct_ + 16 hex)")
            if account:
                billing[url] = account
            else:
                billing.pop(url, None)
        await run_in_threadpool(store.set_owner_billing, principal.owner, billing)
        return {"ok": True, "billing": billing}

    @app.get("/v1/owners/me/statement")
    async def owner_statement(request: Request) -> dict[str, Any]:
        """Every call a hub was served on this owner's behalf — to reconcile with what it paid."""
        if not has_owner_headers(request.headers):
            raise HTTPException(status_code=401, detail="owner signature required")
        principal = await run_in_threadpool(_principal, request, await request.body())
        return {"ok": True, "owner": principal.owner,
                **await run_in_threadpool(store.hub_statement, principal.owner)}

    @app.get("/v1/admin/hub-billing")
    def admin_hub_billing(request: Request) -> dict[str, Any]:
        _auth(request)
        return {"ok": True, "hubs": [url for url, _ in settings.tenant_hub_keys],
                **store.hub_statement(None, limit=100)}

    def _bind_domain(owner: str, raw_domain: str) -> dict[str, Any]:
        """Prove `owner` controls the domain (hestia/domains.py), then claim its word."""
        try:
            domain = domains.registrable(raw_domain)
        except domains.DomainError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            method = domains.prove(domain, owner, doh_url=settings.doh_url)
        except domains.DomainError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        with lifecycle():
            try:
                namespace = claim_domain(store, owner, domain)
            except NameTaken as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "domain": domain, "proved_by": method,
                "namespaces": [namespace, reverse_domain(namespace)],
                "domains": domains_of(store, owner),
                "note": f"families, products and publishers beginning with {namespace} or "
                        f"{reverse_domain(namespace)} are yours on this hearth; the proof is "
                        "checked once, the names stay yours"}

    @app.post("/v1/owners/me/domain")
    async def owner_domain(request: Request) -> dict[str, Any]:
        """An owner proves a domain and receives every name that begins with it (zone included).
        Body {"domain": "example.com"}; the proof is a TXT record or a /.well-known file."""
        if not has_owner_headers(request.headers):
            raise HTTPException(status_code=401, detail="owner signature required")
        raw = await request.body()
        principal = await run_in_threadpool(_principal, request, raw)
        if not principal.admitted:
            raise HTTPException(status_code=403, detail="owner is not admitted on this hearth")
        try:
            raw_domain = str(json.loads(raw or b"{}").get("domain") or "")
        except (ValueError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail='body must be {"domain": "..."}') from exc
        return await run_in_threadpool(_bind_domain, principal.owner, raw_domain)

    @app.post("/v1/admin/owners/domain")
    async def admin_owner_domain(request: Request) -> dict[str, Any]:
        """The operator runs the same proof for an admitted owner: {"pubkey", "domain"}. The
        domain must still name that owner's key; the operator cannot hand a domain out."""
        _auth(request)
        try:
            payload = json.loads(await request.body() or b"{}")
            pubkey = canonical_owner_key(str(payload.get("pubkey") or ""))
        except OwnerProofError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (ValueError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail='body must be {"pubkey", "domain"}') from exc
        if store.get_owner(pubkey) is None:
            raise HTTPException(status_code=404, detail="no such owner on this hearth")
        return await run_in_threadpool(_bind_domain, pubkey, str(payload.get("domain") or ""))

    @app.post("/v1/tenants")
    async def deploy(request: Request) -> dict[str, Any]:
        raw = await request.body()
        principal = await run_in_threadpool(_principal, request, raw)
        try:
            body = DeployRequest.model_validate(json.loads(raw or b"{}"))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Off the event loop: a deploy waits on docker/stub start for seconds, and
        # the whole hearth (edge included) used to freeze for that long.
        return await run_in_threadpool(_deploy, body, principal)

    def _runtime_for(row: TenantRow) -> StubRuntime | WasmRuntime | DockerRuntime | None:
        """The runtime that manages this row, or None when this hearth cannot."""
        if row.source_kind == "image":
            return docker
        return stub

    def _deploy(body: DeployRequest, principal: Principal) -> dict[str, Any]:
        with lifecycle():
            result = _deploy_locked(body, principal)
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

    def _admit_owner_deploy(body: DeployRequest, principal: Principal) -> DeployRequest:
        """An owner deploys as itself, onto its own or a free slug, within its quota."""
        try:
            declared = canonical_owner_key(body.owner_pubkey)
        except OwnerProofError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if declared != principal.owner:
            raise HTTPException(
                status_code=403, detail="owner_pubkey must be the key that signed the request"
            )
        existing = store.get(body.slug)
        if existing is not None and not _owns(principal, existing):
            raise HTTPException(status_code=403, detail="this slug belongs to another owner")
        if body.slug in settings.compute_functions:
            # Compute sells these slugs' handlers as the hearth's own function. An owner who
            # took one the operator named but had not deployed yet would be selling as it.
            raise HTTPException(status_code=403,
                                detail="this slug is reserved for the operator's compute functions")
        has_code = isinstance(body.source, TemplateSource) and bool(body.source.handler.strip())
        # HESTIA_OPEN_OWNER_CODE (wasm only): every owner's code runs in the WebAssembly
        # sandbox, so the right to run it no longer has to be granted key by key.
        if has_code and not (principal.may_run_code or settings.open_owner_code):
            raise HTTPException(
                status_code=403,
                detail="this owner may not run code on this hearth: deploy a pinned image or a "
                "sealed stub, or ask the operator to admit the key with code rights",
            )
        if not principal.admitted:
            store.admit_owner(pubkey=principal.owner, label="open signup",
                              max_tenants=settings.owner_max_tenants,
                              may_run_code=settings.open_owner_code, admitted_by="open")
        owner = store.get_owner(principal.owner) or {}
        quota = int(owner.get("max_tenants") or settings.owner_max_tenants)
        if store.owner_tenant_count(principal.owner, excluding=body.slug) >= quota:
            raise HTTPException(status_code=429, detail=f"owner quota reached ({quota} agents)")
        return body.model_copy(update={"owner_pubkey": principal.owner})

    def _listing_for(
        body: DeployRequest, principal: Principal, holder: str, existing: TenantRow | None
    ) -> tuple[str, list[str], str]:
        """(listing, reasons, decided by) for the row this deploy writes."""
        held_before = existing is not None and existing.listing == HELD
        if principal.kind == "operator":
            # The operator's deploy is its own listing decision, except that it does not lift
            # a hold silently ("redeploy everything"): POST /v1/admin/tenants/{slug}/listing does.
            if held_before:
                return HELD, existing.listing_reasons, existing.listing_by
            return LISTED, [], "operator"
        if held_before and existing.listing_by == "operator":
            return HELD, existing.listing_reasons, "operator"   # only the operator lifts its hold
        others = [dict(c) for c in CAPS] + [dict(c) for c in _compute_caps()]
        for row in store.list_all():
            if holder_of(row.owner_pubkey) != holder:
                others.append(row.capability)       # running or not: buyers still know it
        for name in store.names(FAMILY):
            if name["holder"] != holder:            # a family outlives its agents (renames)
                others.append({"capability_id": f"{name['name']}@v1"})
        for name in store.names(DOMAIN):
            if name["holder"] != holder:            # attestedmemorynetx.* beside attestedmemory.net
                for written in (name["name"], reverse_domain(name["name"])):
                    others.append({"capability_id": f"{written}@v1"})
        status, reasons = review_listing(body.capability.model_dump(), others=others)
        return status, reasons, "review"

    def _deploy_locked(body: DeployRequest, principal: Principal) -> dict[str, Any]:
        if principal.kind == "owner":
            body = _admit_owner_deploy(body, principal)
        elif holder_of(body.owner_pubkey) != OPERATOR:
            # The operator deploying for an owner: stored in the owner's canonical spelling, or
            # the owner's own requests, its quota and its names would not see it as theirs.
            body = body.model_copy(update={"owner_pubkey": holder_of(body.owner_pubkey)})
        existing = store.get(body.slug)
        occupies = existing is not None and existing.status in LIVE
        if store.count_live() >= settings.max_tenants and not occupies:
            raise HTTPException(status_code=429, detail="hearth is full")
        handler = ""
        digest = ""
        if isinstance(body.source, TemplateSource):
            if settings.require_sandbox and settings.runtime != "wasm":
                raise HTTPException(
                    status_code=400,
                    detail="HESTIA_REQUIRE_SANDBOX refuses template-handler stub tenants",
                )
            handler = body.source.handler
            if handler.strip():
                try:
                    if settings.runtime == "wasm":
                        lint_wasm_handler(handler)
                    else:
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

        # One capability id, one agent; one name, one holder. The routed invoke dispatches by
        # capability id, and hubs show the family, product and publisher a manifest names — what
        # a stranger would borrow to shadow an agent buyers already trust (hestia/names.py):
        #   - within one holder, an exact id (any case) runs as one agent at a time;
        #   - a family (`merkle-proof@v2` is `merkle.proof`'s), a product and a publisher (the
        #     payout address included) belong to their first holder for good; `hestia*` is the
        #     hearth's own, and reserved prefixes their holder's.
        # The names are claimed by the database's primary key, so a second process on the same
        # ledger cannot win one either; the lifecycle lock orders this process's deploys.
        holder = holder_of(body.owner_pubkey)
        cap_id = body.capability.capability_id
        for other in store.list_all():
            if (other.slug != body.slug and other.status in LIVE
                    and holder_of(other.owner_pubkey) == holder
                    and str(other.capability.get("capability_id") or "").lower() == cap_id.lower()):
                raise HTTPException(
                    status_code=409, detail=f"{cap_id} is already served here by another agent")
        try:
            claim_names(store, holder,
                        claims_for(body.capability.model_dump(), body.payout_address),
                        slug=body.slug)
        except NameTaken as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # An owner's agent runs at once but enters the public manifest (what hubs index) only
        # after the automatic review; the operator's own agents are not reviewed.
        listing, listing_reasons, listing_by = _listing_for(body, principal, holder, existing)

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
            listing=listing,
            listing_reasons=listing_reasons,
            listing_by=listing_by,
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
            "listed": listing == LISTED,
            "listing_reasons": listing_reasons,
            "note": (
                "This is a hearth URL, not a Hub listing. "
                "The Hub catalogue only changes after a successful announce."
            ),
        }

    @app.get("/v1/tenants")
    async def list_tenants(request: Request) -> dict[str, Any]:
        principal = await run_in_threadpool(_principal, request, await request.body())
        rows = []
        for tenant in store.list_all():
            if not _owns(principal, tenant):
                continue
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
    async def stop_tenant(slug: str, request: Request) -> dict[str, Any]:
        principal = await run_in_threadpool(_principal, request, await request.body())
        return await run_in_threadpool(_stop, slug, principal)

    def _stop(slug: str, principal: Principal) -> dict[str, Any]:
        with lifecycle():
            row = store.get(slug)
            if row is None:
                raise HTTPException(status_code=404, detail="unknown slug")
            _require_owner_of(principal, row)
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
    async def announce_tenant(slug: str, request: Request) -> dict[str, Any]:
        principal = await run_in_threadpool(_principal, request, await request.body())
        return await run_in_threadpool(_announce, slug, principal)

    def _announce(slug: str, principal: Principal) -> dict[str, Any]:
        row = store.get(slug)
        if row is None or row.status != "running":
            raise HTTPException(status_code=404, detail="running tenant required")
        _require_owner_of(principal, row)
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

    @app.api_route("/t/{slug}/{path:path}", methods=["GET", "POST"])
    async def edge(slug: str, path: str, request: Request) -> Response:
        row = store.get(slug)
        if row is None or row.status != "running":
            raise HTTPException(status_code=404, detail="tenant is not running")
        if holder_of(row.owner_pubkey) in _suspended():
            raise HTTPException(status_code=403, detail="this agent's owner is suspended here")
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
                    request.headers.get("x-payment-secret", "").strip(),
                )
                if refusal is not None:
                    return refusal
        if is_wasm(row.listen_url):
            return await _wasm_edge(row, path, request, claim)
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
