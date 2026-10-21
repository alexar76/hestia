"""Tenants: deploy (with owner admission and the listing), list, read, stop and announce."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from hestia.config import Settings
from hestia.hub import AnnounceError
from hestia.hub import announce as hub_announce
from hestia.listing import HELD, LISTED
from hestia.listing import review as review_listing
from hestia.models import DeployRequest, TemplateSource
from hestia.names import (
    DOMAIN,
    FAMILY,
    OPERATOR,
    NameTaken,
    claims_for,
    holder_of,
)
from hestia.names import (
    claim as claim_names,
)
from hestia.names import (
    reverse as reverse_domain,
)
from hestia.owners import OwnerProofError, canonical_owner_key
from hestia.runtime.docker import DockerRuntime
from hestia.runtime.stub import StubRuntime
from hestia.runtime.wasm import WasmRuntime
from hestia.scan import HandlerDenied, admit_handler, lint_wasm_handler
from hestia.store import LIVE, QUARANTINED, TenantRow, TenantStore
from hestia.themis import AdmissionDenied
from hestia.themis import admit as themis_admit


def register(
    *,
    Principal: Any,
    _compute_caps: Callable[..., Any],
    _owns: Callable[..., Any],
    _principal: Callable[..., Any],
    _require_owner_of: Callable[..., Any],
    app: FastAPI,
    docker: DockerRuntime | None,
    lifecycle: Callable[..., Any],
    profile: Any,
    settings: Settings,
    store: TenantStore,
    stub: StubRuntime | WasmRuntime,
) -> SimpleNamespace:
    """Attach the tenant routes to ``app``."""
    # app.py's module-level helpers; imported here because app.py imports this module.
    from hestia.app import (
        CAPS,
        logger,
        tenant_handle,
    )

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

    return SimpleNamespace(
        _deploy=_deploy,
        _stop=_stop,
    )
