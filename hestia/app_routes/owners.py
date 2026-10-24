"""Owners: admission, the owner's own record, billing, statements, the hub billing view and
custom domains.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from hestia import domains
from hestia.config import Settings
from hestia.hub_billing import ACCOUNT_RE
from hestia.names import NameTaken, claim_domain, domains_of
from hestia.names import reverse as reverse_domain
from hestia.owners import OwnerProofError, canonical_owner_key, code_right, has_owner_headers
from hestia.store import TenantStore


def register(
    *,
    _auth: Callable[..., Any],
    _owns: Callable[..., Any],
    _principal: Callable[..., Any],
    app: FastAPI,
    lifecycle: Callable[..., Any],
    settings: Settings,
    store: TenantStore,
    stub: Any = None,
) -> None:
    """Attach the owner routes to ``app``."""
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
        # An update changes only what it names. Defaults here used to rewrite the rest: raising
        # a self-admitted key's quota stored a code right (may_run_code defaulted to True) and
        # blanked its label, which a later switch to the stub runtime honoured (independent
        # review of GCVE-110-PYPI-2026-020712); it also reinstated a suspended key.
        known = await run_in_threadpool(store.get_owner, pubkey) or {}
        status = str(payload.get("status") or known.get("status") or "active")
        if status not in {"active", "suspended"}:
            raise HTTPException(status_code=400, detail="status must be active or suspended")
        try:
            max_tenants = int(payload.get("max_tenants") or known.get("max_tenants")
                              or settings.owner_max_tenants)
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
            label = payload["label"] if "label" in payload else known.get("label", "")
            may_run_code = (payload["may_run_code"] if "may_run_code" in payload
                             else known.get("may_run_code", True))
            return store.admit_owner(
                pubkey=pubkey, label=str(label or "")[:120], status=status,
                max_tenants=max_tenants, may_run_code=bool(may_run_code), admitted_by=lane,
            )

        owner = await run_in_threadpool(write)
        stopped = await run_in_threadpool(_stop_code_it_may_not_run, owner)
        body: dict[str, Any] = {"ok": True, "owner": owner}
        if stopped:
            body["stopped"] = stopped
        return body

    def _stop_code_it_may_not_run(owner: dict[str, Any]) -> list[str]:
        """Under the stub a handler is a plain child process; when an update takes the owner's
        code right away, its running handlers stop now rather than at the next restart."""
        if stub is None or settings.runtime == "wasm" or code_right(
                owner, runtime=settings.runtime, open_owner_code=settings.open_owner_code):
            return []
        from hestia.app import tenant_handle  # app.py imports this module

        stopped = []
        with lifecycle():
            for row in store.list_running():
                if (row.owner_pubkey != owner["pubkey"] or row.source_kind == "image"
                        or not stub.stored_handler(row.slug).strip()):
                    continue
                try:
                    stub.stop(tenant_handle(row.source_kind, row.slug))
                except (RuntimeError, OSError) as exc:
                    store.set_status(row.slug, row.status, error=f"stop failed: {exc}"[:500])
                    continue
                store.set_status(row.slug, "stopped",
                                 error="stopped: its owner may no longer run code on this hearth")
                stopped.append(row.slug)
        return stopped

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
            # What the deploy gate applies: the operator's grant, or the live open-code lane.
            "may_run_code": code_right(
                owner or {"status": "active", "admitted_by": "open", "may_run_code": 0},
                runtime=settings.runtime, open_owner_code=settings.open_owner_code),
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
