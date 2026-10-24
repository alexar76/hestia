"""HESTIA control plane and invoke edge."""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx  # noqa: F401 - tests patch hestia.app.httpx.AsyncClient (the edge proxy client)
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from hestia import __version__
from hestia.app_routes import admin as _admin_routes
from hestia.app_routes import discovery as _discovery_routes
from hestia.app_routes import edge as _edge_routes
from hestia.app_routes import invoke as _invoke_routes
from hestia.app_routes import owners as _owners_routes
from hestia.app_routes import tenants as _tenants_routes
from hestia.app_routes.edge import edge_forward_headers  # noqa: F401 - re-exported
from hestia.compute import (
    COMPUTE_CAPS,
    Limits,
    ReplicaSlots,
    settings_problem,
)
from hestia.compute import price_of as compute_price_of
from hestia.config import Settings
from hestia.limits import BodyLimitMiddleware
from hestia.listing import LISTED
from hestia.models import CapabilitySpec
from hestia.names import backfill as backfill_names
from hestia.names import (
    holder_of,
    seed_prefixes,
)
from hestia.owners import (
    OwnerProofError,
    canonical_owner_key,
    code_right,
    has_owner_headers,
    verify_owner_request,
)
from hestia.policy import profile_from_settings
from hestia.runtime.docker import DockerRuntime
from hestia.runtime.stub import StubRuntime, tenant_image_ref
from hestia.runtime.wasm import WasmRuntime
from hestia.signing import ProviderSigner
from hestia.store import LIVE, QUARANTINED, TenantRow, TenantStore
from hestia.tenant_stub import call_timeout_s

logger = logging.getLogger(__name__)

PRODUCT_ID = "hestia"
# Longest a deploy/stop/reconcile waits for another one to finish before 409.
LIFECYCLE_WAIT_S = 15.0
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


def restore_stub_tenants(
    store: TenantStore, stub: StubRuntime, profile,
    code_allowed: Callable[[TenantRow], bool] | None = None,
) -> dict[str, int]:
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

    A handler is started again only if its owner may run code on THIS hearth now
    (`code_allowed`). Code an open-signup key deployed into the WebAssembly sandbox
    would otherwise come back as a plain child process after a switch to the stub
    runtime (Vulnetix GCVE-110-PYPI-2026-020712); such a row is stopped with the reason.
    """
    restored = 0
    failed = 0
    for row in store.list_running():
        if row.source_kind == "image":
            continue  # a container outlives the control plane; leave it alone
        handler = stub.stored_handler(row.slug)
        if handler.strip() and code_allowed is not None and not code_allowed(row):
            store.set_status(
                row.slug, "stopped",
                error="not restored: its owner may not run code on this hearth now "
                "(suspended, an open-signup key outside the wasm sandbox, or no code right)",
            )
            failed += 1
            logger.warning("tenant %s not restored: owner may not run code here", row.slug)
            continue
        try:
            running = stub.start(
                slug=row.slug,
                capability=CapabilitySpec.model_validate(row.capability),
                handler=handler,
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
    def code_allowed(row: TenantRow) -> bool:
        # Under wasm every handler runs in the sandbox runner, so a restart brings them all back
        # as before. Under the stub a handler is a plain child process: only an owner who may run
        # code here now (hestia.owners.code_right) gets one.
        if settings.runtime == "wasm":
            return True
        return code_right(store.get_owner(row.owner_pubkey), runtime=settings.runtime,
                          open_owner_code=settings.open_owner_code)

    restore_stub_tenants(store, stub, profile, code_allowed)
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
            # Not admitted yet: it would be admitted as an open-signup key on its first deploy.
            unadmitted = {"status": "active", "admitted_by": "open", "may_run_code": 0}
            return Principal("owner", proof.pubkey, admitted=False,
                             may_run_code=code_right(unadmitted, runtime=settings.runtime,
                                                     open_owner_code=settings.open_owner_code))
        return Principal("owner", proof.pubkey,
                         may_run_code=code_right(owner, runtime=settings.runtime,
                                                 open_owner_code=settings.open_owner_code))

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

    # Landing, health, discovery, manifest, search, compute catalogue.
    discovery_scope = _discovery_routes.register(
        _offered=_offered,
        _suspended=_suspended,
        app=app,
        compute_limits=compute_limits,
        landing_dir=landing_dir,
        settings=settings,
        signer=signer,
        store=store,
        stub=stub,
    )
    _compute_caps = discovery_scope._compute_caps
    hearth = discovery_scope.hearth

    # Console and admin.
    _admin_routes.register(
        _auth=_auth,
        app=app,
        console_dir=console_dir,
        docker=docker,
        lifecycle=lifecycle,
        profile=profile,
        settings=settings,
        store=store,
    )

    # Owners, billing, domains.
    _owners_routes.register(
        _auth=_auth,
        _owns=_owns,
        _principal=_principal,
        app=app,
        lifecycle=lifecycle,
        settings=settings,
        store=store,
        stub=stub,
    )

    # Tenants: deploy, list, stop, announce.
    tenants_scope = _tenants_routes.register(
        Principal=Principal,
        _compute_caps=_compute_caps,
        _owns=_owns,
        _principal=_principal,
        _require_owner_of=_require_owner_of,
        app=app,
        docker=docker,
        lifecycle=lifecycle,
        profile=profile,
        settings=settings,
        store=store,
        stub=stub,
    )
    _deploy = tenants_scope._deploy
    _stop = tenants_scope._stop

    # POST /ai-market/v2/invoke.
    invoke_scope = _invoke_routes.register(
        _deploy=_deploy,
        _offered=_offered,
        _principal=_principal,
        _receipt=_receipt,
        _stop=_stop,
        _suspended=_suspended,
        app=app,
        compute_limits=compute_limits,
        compute_slots=compute_slots,
        hearth=hearth,
        settings=settings,
        signer=signer,
        store=store,
        stub=stub,
    )
    _read_capped = invoke_scope._read_capped
    _release_claim = invoke_scope._release_claim
    _release_on_platform_failure = invoke_scope._release_on_platform_failure
    _settle = invoke_scope._settle
    _still_serving = invoke_scope._still_serving
    _terms_for = invoke_scope._terms_for
    _wasm_edge = invoke_scope._wasm_edge

    # Tenant edge.
    _edge_routes.register(
        _read_capped=_read_capped,
        _release_claim=_release_claim,
        _release_on_platform_failure=_release_on_platform_failure,
        _settle=_settle,
        _still_serving=_still_serving,
        _suspended=_suspended,
        _terms_for=_terms_for,
        _wasm_edge=_wasm_edge,
        app=app,
        store=store,
    )



    app.state.settings = settings
    app.state.store = store
    app.state.stub = stub
    return app
