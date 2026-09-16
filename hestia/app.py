"""HESTIA control plane and invoke edge."""

from __future__ import annotations

import hmac
import time
from dataclasses import asdict
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
from hestia.models import DeployRequest, TemplateSource
from hestia.policy import profile_from_settings
from hestia.runtime.docker import DockerRuntime
from hestia.runtime.stub import StubRuntime
from hestia.scan import HandlerDenied, admit_handler
from hestia.signing import ProviderSigner
from hestia.store import TenantRow, TenantStore
from hestia.themis import AdmissionDenied
from hestia.themis import admit as themis_admit

PRODUCT_ID = "hestia"
_EDGE_FORWARD_HEADERS = frozenset({"content-type", "accept"})


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


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = TenantStore(settings.data_dir / "hestia.db")
    signer = ProviderSigner(settings.data_dir / "provider.key")
    profile = profile_from_settings(settings)
    stub = StubRuntime(settings.data_dir)
    docker = DockerRuntime(
        settings.docker_bin,
        settings.allow_image_digests,
        client_env=settings.docker_client_env(),
    )
    handles: dict[str, str] = {}

    app = FastAPI(title="HESTIA", version=__version__, docs_url=None, redoc_url=None)
    console_dir = Path(__file__).resolve().parent.parent / "console"
    fonts_dir = Path(__file__).resolve().parent.parent / "docs" / "landing" / "fonts"
    if console_dir.is_dir():
        app.mount("/ui/assets", StaticFiles(directory=console_dir), name="ui-assets")
    if fonts_dir.is_dir():
        app.mount("/ui/fonts", StaticFiles(directory=fonts_dir), name="ui-fonts")

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

    @app.middleware("http")
    async def _limit_body(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and int(length) > settings.max_body_bytes:
            return JSONResponse({"ok": False, "error": "body too large"}, status_code=413)
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "hestia",
            "version": __version__,
            "runtime": settings.runtime,
            "tenants": store.count(),
        }

    @app.get("/.well-known/ai-market.json")
    def well_known() -> dict[str, Any]:
        return {
            "protocol": "aimarket/2",
            "name": "HESTIA",
            "hub_url": settings.public_base,
            "provider_pubkey": signer.public_key_b64,
            "capabilities": [c["capability_id"] for c in CAPS],
        }

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
        return {
            "protocol": "aimarket/2",
            "name": "HESTIA",
            "total_capabilities": len(tools),
            "tools": tools,
        }

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
            runtime = docker if existing.source_kind == "image" else stub
            runtime.stop(handles.get(body.slug, body.slug))

        runtime = docker if digest else stub
        running = runtime.start(
            slug=body.slug,
            capability=body.capability,
            handler=handler,
            image_digest=digest,
            profile=profile,
            env={},
        )
        handles[body.slug] = running.handle
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
        runtime.stop(handles.get(slug, slug))
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
        raise HTTPException(status_code=404, detail="unknown capability")

    @app.api_route("/t/{slug}/{path:path}", methods=["GET", "POST"])
    async def edge(slug: str, path: str, request: Request) -> Response:
        row = store.get(slug)
        if row is None or row.status != "running":
            raise HTTPException(status_code=404, detail="tenant is not running")
        target = f"{row.listen_url.rstrip('/')}/{path}"
        raw = await request.body()
        headers = edge_forward_headers(request.headers)
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                forwarded = await client.request(
                    request.method, target, content=raw, headers=headers
                )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="tenant unreachable") from exc
        return Response(
            content=forwarded.content,
            status_code=forwarded.status_code,
            media_type=forwarded.headers.get("content-type", "application/json"),
        )

    app.state.settings = settings
    app.state.store = store
    app.state.stub = stub
    return app
