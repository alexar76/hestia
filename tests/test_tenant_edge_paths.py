"""Payment admission and tenant dispatch must interpret the same route.

These tests use an in-process ASGI app and a mock HTTP upstream only.
"""
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from hestia.app_routes import edge


@pytest.fixture
def tenant_edge(monkeypatch):
    app = FastAPI()
    calls = {"settle": 0, "upstream": []}
    row = SimpleNamespace(status="running", owner_pubkey="", listen_url="http://127.0.0.1:32123")

    def settle(*args):
        calls["settle"] += 1
        return JSONResponse({"error": "payment_required"}, status_code=402), None

    async def read_capped(response):
        return await response.aread()

    async def wasm_edge(*args):
        raise AssertionError("HTTP tenant fixture must not use the WASM route")

    def upstream(request):
        calls["upstream"].append(str(request.url))
        return httpx.Response(200, json={"path": request.url.path})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(edge.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(upstream), **kwargs
    ))
    edge.register(
        _read_capped=read_capped,
        _release_claim=lambda *args: None,
        _release_on_platform_failure=lambda *args: None,
        _settle=settle,
        _still_serving=lambda row: True,
        _suspended=lambda: set(),
        _terms_for=lambda row: object(),
        _wasm_edge=wasm_edge,
        app=app,
        store=SimpleNamespace(get=lambda slug: row),
    )
    return TestClient(app), calls


@pytest.mark.parametrize("path", ["invoke", "%69nvoke", "invoke/"])
def test_paid_path_is_checked_after_asgi_decoding(tenant_edge, path):
    api, calls = tenant_edge
    response = api.post(f"/t/priced/{path}", json={})
    assert response.status_code == 402
    assert calls == {"settle": 1, "upstream": []}


@pytest.mark.parametrize("path", [
    "health/%2e%2e/invoke", "%2e/invoke", "i%2fnvoke", "i%5cnvoke",
    "%252e%252e/invoke", "invoke%3fignored", "invoke%23ignored",
    "invoke;ignored", "health//invoke", "%00invoke",
])
def test_ambiguous_path_never_reaches_payment_or_upstream(tenant_edge, path):
    api, calls = tenant_edge
    response = api.post(f"/t/priced/{path}", json={})
    assert response.status_code == 400
    assert calls == {"settle": 0, "upstream": []}


def test_health_query_is_forwarded_without_entering_the_path(tenant_edge):
    api, calls = tenant_edge
    response = api.get("/t/priced/health?detail=yes%2Fno")
    assert response.status_code == 200
    assert calls == {"settle": 0, "upstream": ["http://127.0.0.1:32123/health?detail=yes%2Fno"]}


@pytest.mark.parametrize("path", ["", "docs/"])
def test_root_and_ordinary_directory_hosting_still_work(tenant_edge, path):
    api, calls = tenant_edge
    assert api.get(f"/t/priced/{path}").status_code == 200
    assert calls == {"settle": 0, "upstream": [f"http://127.0.0.1:32123/{path}"]}


@pytest.mark.parametrize("path", ["/invoke", "../invoke", "invoke\\ignored"])
def test_canonical_path_rejects_ambiguous_segments(path):
    with pytest.raises(HTTPException) as error:
        edge.canonical_edge_path(path, b"/t/priced/placeholder")
    assert error.value.status_code == 400
