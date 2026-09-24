"""What the ledger may claim after the control plane restarts, and what the
public edge is allowed to reach.

A stub tenant is a child of the control-plane process on an ephemeral loopback
port. If a row survives the process that owned it, the recorded port is free for
any other local service to bind — and /t/{slug} is unauthenticated.
"""

from __future__ import annotations

import http.server
import threading

from fastapi.testclient import TestClient

from hestia.app import build_app, tenant_handle
from hestia.config import Settings
from tests.conftest import auth, client, deploy_payload

HANDLER = "def handle(payload):\n    return {'seen': sorted(payload.keys())}\n"


def _restart(tmp_path) -> TestClient:
    """A second control plane over the same data dir — i.e. a restart."""
    return TestClient(build_app(Settings.for_test(tmp_path)))


def test_a_restart_brings_the_agent_back(tmp_path) -> None:
    """A restart used to empty the host and need a manual redeploy.

    Everything needed is in the data volume — capability.json, handler.py and
    tenant.key — so the hearth starts them again itself.
    """
    api = client(tmp_path)
    assert api.post(
        "/v1/tenants", json=deploy_payload("ghost", HANDLER), headers=auth(api)
    ).status_code == 200
    before = api.app.state.store.get("ghost")
    api.app.state.stub._stop_all()

    after = _restart(tmp_path)
    assert [t["slug"] for t in after.get("/v1/hearth").json()["tenants"]] == ["ghost"]
    row = after.app.state.store.get("ghost")
    assert row.status == "running"
    assert row.last_error == ""
    # The port is new on every start, so the row must have been rewritten — a
    # surviving row that kept its old port is the hazard this replaced.
    assert row.listen_url != before.listen_url
    assert after.post("/t/ghost/invoke", json={"x": 1}).status_code == 200


def test_a_restored_agent_keeps_its_signing_identity(tmp_path) -> None:
    """tenant.key lives in the data dir, so a buyer's pinned key stays valid."""
    api = client(tmp_path)
    api.post("/v1/tenants", json=deploy_payload("keeper", HANDLER), headers=auth(api))
    before = api.post("/t/keeper/invoke", json={}).json()["provider_pubkey"]
    api.app.state.stub._stop_all()

    after = _restart(tmp_path)
    assert after.post("/t/keeper/invoke", json={}).json()["provider_pubkey"] == before


def test_an_agent_that_cannot_come_back_is_marked_stopped(tmp_path, monkeypatch) -> None:
    """The one thing that must never happen: `running` pointing at a port we
    do not own. A failed restore stops the row instead."""
    api = client(tmp_path)
    api.post("/v1/tenants", json=deploy_payload("broken", HANDLER), headers=auth(api))
    api.app.state.stub._stop_all()

    monkeypatch.setattr(
        "hestia.runtime.stub.StubRuntime.start",
        lambda self, **kwargs: (_ for _ in ()).throw(RuntimeError("port exhausted")),
    )
    after = _restart(tmp_path)
    row = after.app.state.store.get("broken")
    assert row.status == "stopped"
    assert "restore after restart failed" in row.last_error
    assert after.get("/v1/hearth").json()["tenants"] == []
    assert after.post("/t/broken/invoke", json={}).status_code == 404


def test_a_restored_template_agent_keeps_its_handler(tmp_path) -> None:
    """start() deletes handler.py when handed an empty handler, so a restore that
    forgot to read it back would quietly demote the agent to a sealed stub."""
    api = client(tmp_path)
    api.post("/v1/tenants", json=deploy_payload("keeps", HANDLER), headers=auth(api))
    before = api.post("/t/keeps/invoke", json={"hello": "x"}).json()["result"]
    api.app.state.stub._stop_all()

    after = _restart(tmp_path)
    assert after.post("/t/keeps/invoke", json={"hello": "x"}).json()["result"] == before


def test_edge_refuses_a_port_another_service_now_owns(tmp_path) -> None:
    api = client(tmp_path)
    api.post("/v1/tenants", json=deploy_payload("ghost", HANDLER), headers=auth(api))
    port = int(api.app.state.store.get("ghost").listen_url.rsplit(":", 1)[1])
    api.app.state.stub._stop_all()

    class Imposter(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b'{"who":"an unrelated loopback service"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:  # noqa: A003
            return

    squatter = http.server.HTTPServer(("127.0.0.1", port), Imposter)
    threading.Thread(target=squatter.serve_forever, daemon=True).start()
    try:
        after = _restart(tmp_path)
        res = after.get("/t/ghost/anything")
        assert res.status_code == 404
        assert "an unrelated loopback service" not in res.text
    finally:
        squatter.shutdown()
        squatter.server_close()


def test_image_rows_survive_a_restart(tmp_path) -> None:
    # A container outlives the control plane, so its row must not be reconciled.
    api = client(tmp_path)
    store = api.app.state.store
    api.post("/v1/tenants", json=deploy_payload("keeper", HANDLER), headers=auth(api))
    row = store.get("keeper")
    row.source_kind = "image"
    row.image_digest = "sha256:" + "a" * 64
    store.upsert(row)
    assert store.reconcile_stale_stubs("restarted") == 0
    assert store.get("keeper").status == "running"


def test_tenant_handle_is_derived_not_remembered() -> None:
    # The in-memory map went empty on restart and docker was handed the bare slug.
    assert tenant_handle("image", "alpha") == "hestia-alpha"
    assert tenant_handle("template", "alpha") == "alpha"


def test_stop_after_a_restart_removes_the_right_container(tmp_path, monkeypatch) -> None:
    from hestia.runtime.docker import DockerRuntime
    from tests.fake_engine import FakeEngine

    digest = "sha256:" + ("ab" * 32)
    engine = FakeEngine().install(monkeypatch)
    settings = Settings(
        **{
            **Settings.for_test(tmp_path).__dict__,
            "runtime": "docker",
            "allow_image_digests": frozenset({digest}),
        }
    )
    api = TestClient(build_app(settings))
    payload = deploy_payload("boxed")
    payload["source"] = {"kind": "image", "image_digest": digest}
    assert api.post("/v1/tenants", json=payload, headers=auth(api)).status_code == 200

    after = TestClient(build_app(settings))
    removed: list[str] = []
    original = DockerRuntime.stop

    def spy(self, handle):
        removed.append(handle)
        return original(self, handle)

    monkeypatch.setattr("hestia.runtime.docker.DockerRuntime.stop", spy)
    assert after.post("/v1/tenants/boxed/stop", headers=auth(after)).status_code == 200
    assert removed == ["hestia-boxed"]
    assert "hestia-boxed" not in engine.containers
    assert "hestia-tenants-boxed" not in engine.networks


def test_a_stub_hearth_stops_an_image_row_without_calling_docker(tmp_path, monkeypatch) -> None:
    """A stub hearth's docker CLI is ungated (the host-socket opt-in is checked
    for the docker runtime only), so it must never call docker at all."""
    from tests.fake_engine import FakeEngine

    api = client(tmp_path)
    api.post("/v1/tenants", json=deploy_payload("boxed", HANDLER), headers=auth(api))
    store = api.app.state.store
    row = store.get("boxed")
    row.source_kind = "image"
    store.upsert(row)

    engine = FakeEngine().install(monkeypatch)
    after = _restart(tmp_path)
    assert after.app.state.store.get("boxed").status == "quarantined"
    res = after.post("/v1/tenants/boxed/stop", headers=auth(after))
    assert res.status_code == 200
    assert "did not touch" in res.json()["note"]
    assert engine.calls == []


def test_edge_forwards_the_query_string(tmp_path) -> None:
    api = client(tmp_path)
    api.post("/v1/tenants", json=deploy_payload("q-echo", HANDLER), headers=auth(api))
    seen: dict[str, str] = {}
    import httpx

    original = httpx.AsyncClient.stream

    def spy(self, method, url, **kwargs):
        seen["url"] = str(url)
        return original(self, method, url, **kwargs)

    httpx.AsyncClient.stream = spy
    try:
        api.get("/t/q-echo/health", params={"trace": "1", "x": "2"})
    finally:
        httpx.AsyncClient.stream = original
    assert seen["url"].endswith("/health?trace=1&x=2")


def test_a_tenant_that_will_not_start_does_not_stay_on_the_roster(tmp_path, monkeypatch) -> None:
    api = client(tmp_path)
    api.post("/v1/tenants", json=deploy_payload("flaky", HANDLER), headers=auth(api))
    monkeypatch.setattr(
        "hestia.runtime.stub.StubRuntime.start",
        lambda self, **kwargs: (_ for _ in ()).throw(RuntimeError("tenant did not become healthy")),
    )
    res = api.post("/v1/tenants", json=deploy_payload("flaky", HANDLER), headers=auth(api))
    assert res.status_code == 502
    assert api.app.state.store.get("flaky").status == "stopped"
    assert api.get("/v1/hearth").json()["tenants"] == []
