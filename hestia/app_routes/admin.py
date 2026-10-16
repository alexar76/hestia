"""The console and the operator's admin routes: overview, listings, reserved names, reconcile."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from hestia.config import Settings
from hestia.listing import HELD, LISTED
from hestia.names import (
    CLOSED,
    DOMAIN,
    FAMILY,
    key_for,
    KINDS,
    OPERATOR,
    PREFIX,
    PUBLISHER,
    RESERVABLE,
    users_of,
)
from hestia.owners import canonical_owner_key, OwnerProofError
from hestia.runtime.docker import DockerRuntime
from hestia.store import QUARANTINED, TenantStore


def register(
    *,
    _auth: Callable[..., Any],
    app: FastAPI,
    console_dir: Any,
    docker: DockerRuntime | None,
    lifecycle: Callable[..., Any],
    profile: Any,
    settings: Settings,
    store: TenantStore,
) -> None:
    """Attach the console and admin routes to ``app``."""
    # app.py's module-level helpers; imported here because app.py imports this module.
    from hestia.app import (
        reconcile_docker_tenants,
    )

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
