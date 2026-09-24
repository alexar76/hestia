"""Boot-time reconcile of image tenants, and the lifecycle around it.

The per-tenant network fix first shipped with a boot step that crashed the whole
hearth — stub tenants included — whenever the engine could not answer, and ran
docker even under HESTIA_RUNTIME=stub. These tests pin the replacement: a stub
hearth never calls docker, a docker hearth always boots, and a row it cannot
verify is quarantined and retried rather than served or silently dropped.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from hestia.app import build_app
from hestia.config import Settings
from hestia.store import QUARANTINED, TenantRow, TenantStore
from tests.conftest import auth, capability, client, deploy_payload
from tests.fake_engine import FakeEngine

DIGEST = "sha256:" + ("ab" * 32)
UNREACHABLE = "failed to connect to the docker API at tcp://dind:2376: lookup dind: no such host"


def _docker_settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings.for_test(tmp_path).__dict__
    return Settings(
        **{**base, "runtime": "docker", "allow_image_digests": frozenset({DIGEST}), **overrides}
    )


def _image_row(store: TenantStore, slug: str, *, status: str = "running", url: str = "") -> None:
    now = time.time()
    cap = capability(capability_id=f"{slug}.cap@v1")
    store.upsert(
        TenantRow(
            slug=slug,
            status=status,
            capability=cap,
            source_kind="image",
            owner_pubkey="",
            listen_url=url or "http://172.18.0.2:8080",
            public_url=f"http://127.0.0.1:9480/t/{slug}",
            announced=0,
            note="",
            created_at=now,
            updated_at=now,
            last_error="",
            image_digest=DIGEST,
        )
    )


def _ledger(tmp_path: Path) -> TenantStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return TenantStore.open(data_dir=tmp_path)


def _image_payload(slug: str) -> dict:
    payload = deploy_payload(slug)
    payload["capability"] = capability(capability_id=f"{slug}.cap@v1")
    payload["source"] = {"kind": "image", "image_digest": DIGEST}
    return payload


# ------------------------------------------------------------------ stub path


def test_a_stub_hearth_boots_without_docker_and_never_calls_it(tmp_path, monkeypatch) -> None:
    store = _ledger(tmp_path)
    _image_row(store, "legacy")
    store.close()

    # No docker binary at all, like the compose box: HEAD booted here, the
    # first version of the fix crashed.
    missing = Settings(**{**Settings.for_test(tmp_path).__dict__, "docker_bin": "/nonexistent/docker"})
    engine = FakeEngine().install(monkeypatch)
    api = TestClient(build_app(missing))

    row = api.app.state.store.get("legacy")
    assert row.status == QUARANTINED
    assert "HESTIA_RUNTIME=docker" in row.last_error
    assert engine.calls == []  # not one docker call, host socket or otherwise
    assert api.get("/v1/hearth").json()["tenants"] == []
    assert api.get("/t/legacy/health").status_code == 404


# ------------------------------------------------------------- docker upgrade


def test_upgrade_moves_every_tracked_tenant_off_the_shared_bridge(tmp_path, monkeypatch) -> None:
    engine = FakeEngine().install(monkeypatch)
    engine.add_network("hestia-tenants", internal=True)
    for name in ("alpha", "beta", "stale", "orphan"):
        engine.add_container(f"hestia-{name}", "hestia-tenants")
    store = _ledger(tmp_path)
    _image_row(store, "alpha")
    _image_row(store, "beta")
    _image_row(store, "stale", status="stopped")  # its `docker rm` once failed
    store.close()

    api = TestClient(build_app(_docker_settings(tmp_path)))

    ledger = api.app.state.store
    for slug in ("alpha", "beta"):
        row = ledger.get(slug)
        assert row.status == "running"
        nets = engine.containers[f"hestia-{slug}"]["NetworkSettings"]["Networks"]
        assert set(nets) == {f"hestia-tenants-{slug}"}
        assert row.listen_url == f"http://{nets[f'hestia-tenants-{slug}']['IPAddress']}:8080"
    # All legacy containers were removed before any tenant started again.
    verbs = [" ".join(c[1:3]) for c in engine.calls if c[1:2] in (["rm"], ["run"])]
    assert verbs.index("run --detach") > max(i for i, v in enumerate(verbs) if v.startswith("rm -f"))
    # The shared bridge is gone; what the ledger does not track is cut off, not deleted.
    assert "hestia-tenants" not in engine.networks
    for leftover in ("hestia-stale", "hestia-orphan"):
        assert engine.containers[leftover]["NetworkSettings"]["Networks"] == {}
    assert {t["slug"] for t in api.get("/v1/hearth").json()["tenants"]} == {"alpha", "beta"}


def test_an_unreachable_engine_quarantines_image_rows_and_the_hearth_still_serves(
    tmp_path, monkeypatch
) -> None:
    stub_api = client(tmp_path)
    assert stub_api.post("/v1/tenants", json=deploy_payload("echo"), headers=auth(stub_api)).status_code == 200
    _image_row(stub_api.app.state.store, "boxed")

    engine = FakeEngine().install(monkeypatch)
    engine.on(lambda a: True, stderr=UNREACHABLE)
    api = TestClient(build_app(_docker_settings(tmp_path)))

    assert api.get("/health").status_code == 200
    assert api.get("/t/echo/health").status_code == 200  # the stub tenant came back
    row = api.app.state.store.get("boxed")
    assert row.status == QUARANTINED and "cannot verify isolation" in row.last_error
    assert api.get("/t/boxed/health").status_code == 404
    overview = api.get("/v1/admin/overview", headers=auth(api)).json()
    assert overview["quarantined_slugs"] == ["boxed"]

    # Engine back: the operator retries without a restart.
    engine.hooks.clear()
    report = api.post("/v1/admin/reconcile", headers=auth(api)).json()
    assert report["restored"] == ["boxed"]
    assert api.app.state.store.get("boxed").status == "running"


def test_one_container_that_will_not_go_does_not_hold_back_the_rest(tmp_path, monkeypatch) -> None:
    engine = FakeEngine().install(monkeypatch)
    engine.add_network("hestia-tenants", internal=True)
    engine.add_container("hestia-alpha", "hestia-tenants")
    engine.add_container("hestia-beta", "hestia-tenants")
    store = _ledger(tmp_path)
    _image_row(store, "alpha")
    _image_row(store, "beta")
    store.close()
    engine.on(
        lambda a: a == ["rm", "-f", "hestia-alpha"],
        stderr='Error response from daemon: cannot remove container "/hestia-alpha": could not kill',
    )

    api = TestClient(build_app(_docker_settings(tmp_path)))

    ledger = api.app.state.store
    assert ledger.get("alpha").status == QUARANTINED
    assert "cannot remove" in ledger.get("alpha").last_error
    assert ledger.get("beta").status == "running"
    # Even the stuck one is cut off from the shared bridge.
    assert engine.containers["hestia-alpha"]["NetworkSettings"]["Networks"] == {}


def test_a_verified_tenant_is_left_running_and_its_address_re_read(tmp_path, monkeypatch) -> None:
    engine = FakeEngine().install(monkeypatch)
    settings = _docker_settings(tmp_path)
    api = TestClient(build_app(settings))
    assert api.post("/v1/tenants", json=_image_payload("alpha"), headers=auth(api)).status_code == 200
    container_id = engine.containers["hestia-alpha"]["Id"]
    api.app.state.store.set_status("alpha", "running")
    row = api.app.state.store.get("alpha")
    row.listen_url = "http://10.231.99.2:8080"  # recorded long ago
    api.app.state.store.upsert(row)

    engine.calls.clear()
    after = TestClient(build_app(settings))

    assert engine.containers["hestia-alpha"]["Id"] == container_id
    assert not [c for c in engine.calls if c[1:2] in (["rm"], ["run"])]
    address = engine.containers["hestia-alpha"]["NetworkSettings"]["Networks"]["hestia-tenants-alpha"]["IPAddress"]
    assert after.app.state.store.get("alpha").listen_url == f"http://{address}:8080"


def test_pool_exhaustion_quarantines_and_a_later_reconcile_restores(tmp_path, monkeypatch) -> None:
    engine = FakeEngine().install(monkeypatch)
    engine.add_network("hestia-tenants", internal=True)
    engine.add_container("hestia-alpha", "hestia-tenants")
    engine.add_container("hestia-beta", "hestia-tenants")
    store = _ledger(tmp_path)
    _image_row(store, "alpha")
    _image_row(store, "beta")
    store.close()

    api = TestClient(build_app(_docker_settings(tmp_path, tenant_subnet_pool="10.231.0.0/29")))
    ledger = api.app.state.store
    assert ledger.get("alpha").status == "running"
    assert ledger.get("beta").status == QUARANTINED
    assert "exhausted" in ledger.get("beta").last_error

    assert api.post("/v1/tenants/alpha/stop", headers=auth(api)).status_code == 200
    # alpha's address stays reserved for a grace period (see the next test).
    assert api.post("/v1/admin/reconcile", headers=auth(api)).json()["restored"] == []
    monkeypatch.setattr("hestia.app.RELEASED_ADDRESS_GRACE_S", 0)
    assert api.post("/v1/admin/reconcile", headers=auth(api)).json()["restored"] == ["beta"]
    assert ledger.get("beta").status == "running"


def test_a_just_stopped_tenants_address_is_not_handed_to_the_next(tmp_path, monkeypatch) -> None:
    """A request routed to alpha just before its stop must not reach beta."""
    engine = FakeEngine().install(monkeypatch)
    api = TestClient(build_app(_docker_settings(tmp_path)))
    assert api.post("/v1/tenants", json=_image_payload("alpha"), headers=auth(api)).status_code == 200
    alpha_url = api.app.state.store.get("alpha").listen_url
    assert api.post("/v1/tenants/alpha/stop", headers=auth(api)).status_code == 200
    assert api.post("/v1/tenants", json=_image_payload("beta"), headers=auth(api)).status_code == 200
    assert api.app.state.store.get("beta").listen_url != alpha_url
    assert engine.networks["hestia-tenants-beta"]["IPAM"]["Config"][0]["Subnet"] == "10.231.0.8/29"


def test_reconcile_is_refused_on_a_stub_hearth(tmp_path) -> None:
    api = client(tmp_path)
    assert api.post("/v1/admin/reconcile", headers=auth(api)).status_code == 400


# ------------------------------------------------------------------ stop/deploy


def test_a_stop_the_engine_refuses_is_reported_and_the_row_kept(tmp_path, monkeypatch) -> None:
    engine = FakeEngine().install(monkeypatch)
    api = TestClient(build_app(_docker_settings(tmp_path)))
    assert api.post("/v1/tenants", json=_image_payload("alpha"), headers=auth(api)).status_code == 200
    engine.on(
        lambda a: a[:2] == ["rm", "-f"],
        stderr='Error response from daemon: cannot remove container "/hestia-alpha": could not kill',
        times=1,
    )

    res = api.post("/v1/tenants/alpha/stop", headers=auth(api))
    assert res.status_code == 502 and "could not kill" in res.json()["detail"]
    row = api.app.state.store.get("alpha")
    assert row.status == "running" and "stop failed" in row.last_error

    assert api.post("/v1/tenants/alpha/stop", headers=auth(api)).status_code == 200
    assert api.app.state.store.get("alpha").status == "stopped"
    assert "hestia-tenants-alpha" not in engine.networks


def test_a_stuck_network_rm_does_not_leave_a_stopped_tenant_running(tmp_path, monkeypatch) -> None:
    engine = FakeEngine().install(monkeypatch)
    api = TestClient(build_app(_docker_settings(tmp_path)))
    api.post("/v1/tenants", json=_image_payload("alpha"), headers=auth(api))
    engine.on(lambda a: a[:2] == ["network", "rm"], stderr="Error response from daemon: timeout")
    assert api.post("/v1/tenants/alpha/stop", headers=auth(api)).status_code == 200
    assert api.app.state.store.get("alpha").status == "stopped"


def test_a_failed_stop_before_redeploy_is_reported_not_500(tmp_path, monkeypatch) -> None:
    engine = FakeEngine().install(monkeypatch)
    api = TestClient(build_app(_docker_settings(tmp_path)))
    api.post("/v1/tenants", json=_image_payload("alpha"), headers=auth(api))
    engine.on(lambda a: a[:2] == ["rm", "-f"], stderr="Error response from daemon: could not kill")
    res = api.post("/v1/tenants", json=_image_payload("alpha"), headers=auth(api))
    assert res.status_code == 502
    assert "stop before redeploy failed" in api.app.state.store.get("alpha").last_error


def test_a_stop_during_announce_is_not_undone(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, hub_url="http://hub.invalid")
    api.post("/v1/tenants", json=deploy_payload("echo"), headers=auth(api))
    store = api.app.state.store

    def slow_announce(**_kwargs):
        store.set_status("echo", "stopped")  # a stop lands while the hub is slow
        return {"ok": True}

    monkeypatch.setattr("hestia.app.hub_announce", slow_announce)
    res = api.post("/v1/tenants/echo/announce", headers=auth(api))
    assert res.status_code == 409
    assert store.get("echo").status == "stopped"


def _blocking_start(monkeypatch):
    from hestia.runtime.stub import StubRuntime

    entered, release = threading.Event(), threading.Event()
    original = StubRuntime.start

    def start(self, **kwargs):
        running = original(self, **kwargs)
        entered.set()
        assert release.wait(10)
        return running

    monkeypatch.setattr("hestia.runtime.stub.StubRuntime.start", start)
    return entered, release


def test_a_stop_cannot_land_between_a_start_and_its_ledger_row(tmp_path, monkeypatch) -> None:
    api = client(tmp_path)
    api.post("/v1/tenants", json=deploy_payload("echo"), headers=auth(api))
    entered, release = _blocking_start(monkeypatch)
    results: dict[str, int] = {}

    deploy = threading.Thread(
        target=lambda: results.__setitem__(
            "deploy", api.post("/v1/tenants", json=deploy_payload("echo"), headers=auth(api)).status_code
        )
    )
    deploy.start()
    assert entered.wait(10)
    stop = threading.Thread(
        target=lambda: results.__setitem__(
            "stop", api.post("/v1/tenants/echo/stop", headers=auth(api)).status_code
        )
    )
    stop.start()
    stop.join(0.5)
    assert stop.is_alive()  # waits for the deploy instead of racing it
    release.set()
    deploy.join(10)
    stop.join(10)
    assert results == {"deploy": 200, "stop": 200}
    assert api.app.state.store.get("echo").status == "stopped"


def test_a_deploy_does_not_freeze_the_hearth(tmp_path, monkeypatch) -> None:
    entered, release = _blocking_start(monkeypatch)
    # As a context manager the client serves every request on ONE event loop,
    # like uvicorn; without it each request gets a loop of its own and a
    # blocked loop would go unnoticed.
    with TestClient(build_app(Settings.for_test(tmp_path))) as api:
        deploy = threading.Thread(
            target=lambda: api.post("/v1/tenants", json=deploy_payload("echo"), headers=auth(api))
        )
        deploy.start()
        try:
            assert entered.wait(10)
            answered: list[int] = []
            probe = threading.Thread(target=lambda: answered.append(api.get("/health").status_code))
            probe.start()
            probe.join(3)
            assert answered == [200]  # served while the deploy is still in progress
        finally:
            release.set()
            deploy.join(10)
            probe.join(10)


def test_the_sqlite_ledger_is_safe_across_threads(tmp_path) -> None:
    store = _ledger(tmp_path)
    for i in range(8):
        _image_row(store, f"tenant-{i}")
    errors: list[BaseException] = []

    def hammer(i: int) -> None:
        try:
            for _ in range(150):
                row = store.get(f"tenant-{i}")
                assert row is not None and row.slug == f"tenant-{i}"
                store.set_status(row.slug, "running")
                assert len(store.list_all()) == 8
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


# --------------------------------------------------------- second review round


def test_a_redeploy_takes_the_old_address_off_the_edge_first(tmp_path, monkeypatch) -> None:
    """While the new tenant starts, the old one's freed port could be bound by
    anyone; the edge must not proxy there."""
    api = client(tmp_path)
    api.post("/v1/tenants", json=deploy_payload("echo"), headers=auth(api))
    entered, release = _blocking_start(monkeypatch)
    deploy = threading.Thread(
        target=lambda: api.post("/v1/tenants", json=deploy_payload("echo"), headers=auth(api))
    )
    deploy.start()
    try:
        assert entered.wait(10)
        assert api.get("/t/echo/health").status_code == 404
        assert api.get("/v1/hearth").json()["tenants"] == []
    finally:
        release.set()
        deploy.join(10)
    assert api.get("/t/echo/health").status_code == 200


def test_announce_runs_after_the_row_is_written_and_outside_the_lock(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, hub_url="http://hub.invalid")
    store = api.app.state.store
    in_hub, finish = threading.Event(), threading.Event()
    seen: dict[str, str] = {}

    def slow_hub(**_kwargs):
        seen["status"] = store.get("echo").status
        in_hub.set()
        assert finish.wait(10)
        return {"ok": True}

    monkeypatch.setattr("hestia.app.hub_announce", slow_hub)
    payload = deploy_payload("echo")
    payload["announce"] = True
    out: dict = {}
    deploy = threading.Thread(
        target=lambda: out.update(api.post("/v1/tenants", json=payload, headers=auth(api)).json())
    )
    deploy.start()
    assert in_hub.wait(10)
    assert seen["status"] == "running"
    # The hub is slow, yet a stop is not held up by it.
    assert api.post("/v1/tenants/echo/stop", headers=auth(api)).status_code == 200
    finish.set()
    deploy.join(10)
    assert out["announced"] is False  # the tenant was stopped before the hub answered
    assert store.get("echo").status == "stopped"


def test_a_busy_lifecycle_answers_409_instead_of_queueing_forever(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hestia.app.LIFECYCLE_WAIT_S", 0.2)
    api = client(tmp_path)
    api.post("/v1/tenants", json=deploy_payload("echo"), headers=auth(api))
    entered, release = _blocking_start(monkeypatch)
    deploy = threading.Thread(
        target=lambda: api.post("/v1/tenants", json=deploy_payload("echo"), headers=auth(api))
    )
    deploy.start()
    try:
        assert entered.wait(10)
        assert api.post("/v1/tenants/echo/stop", headers=auth(api)).status_code == 409
    finally:
        release.set()
        deploy.join(10)


def test_admin_reconcile_needs_the_token_and_holds_the_lifecycle(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hestia.app.LIFECYCLE_WAIT_S", 0.2)
    engine = FakeEngine().install(monkeypatch)
    api = TestClient(build_app(_docker_settings(tmp_path)))
    assert api.post("/v1/admin/reconcile").status_code == 401
    api.post("/v1/tenants", json=_image_payload("alpha"), headers=auth(api))
    inside, leave = threading.Event(), threading.Event()

    def slow(args):
        inside.set()
        assert leave.wait(10)

    engine.on(lambda a: a[:1] == ["version"], action=slow, times=1)
    worker = threading.Thread(target=lambda: api.post("/v1/admin/reconcile", headers=auth(api)))
    worker.start()
    try:
        assert inside.wait(10)
        assert api.post("/v1/tenants/alpha/stop", headers=auth(api)).status_code == 409
    finally:
        leave.set()
        worker.join(10)


def test_the_invoke_door_does_not_freeze_the_hearth_either(tmp_path, monkeypatch) -> None:
    entered, release = _blocking_start(monkeypatch)
    with TestClient(build_app(Settings.for_test(tmp_path))) as api:
        body = {"capability_id": "hestia.host.deploy@v1", "input": deploy_payload("echo")}
        deploy = threading.Thread(target=lambda: api.post("/ai-market/v2/invoke", json=body, headers=auth(api)))
        deploy.start()
        try:
            assert entered.wait(10)
            answered: list[int] = []
            probe = threading.Thread(target=lambda: answered.append(api.get("/health").status_code))
            probe.start()
            probe.join(3)
            assert answered == [200]
        finally:
            release.set()
            deploy.join(10)
            probe.join(10)


def test_a_redeploy_over_a_quarantined_tenant_replaces_its_container(tmp_path, monkeypatch) -> None:
    engine = FakeEngine().install(monkeypatch)
    api = TestClient(build_app(_docker_settings(tmp_path)))
    api.post("/v1/tenants", json=_image_payload("alpha"), headers=auth(api))
    old_id = engine.containers["hestia-alpha"]["Id"]
    api.app.state.store.set_status("alpha", QUARANTINED, error="test")
    assert api.post("/v1/tenants", json=_image_payload("alpha"), headers=auth(api)).status_code == 200
    assert engine.containers["hestia-alpha"]["Id"] != old_id


def test_a_redeploy_over_a_stopped_row_whose_container_survived(tmp_path, monkeypatch) -> None:
    engine = FakeEngine().install(monkeypatch)
    store = _ledger(tmp_path)
    _image_row(store, "alpha", status="stopped")
    store.close()
    engine.add_container("hestia-alpha", "bridge")  # a stub hearth left it running
    api = TestClient(build_app(_docker_settings(tmp_path)))
    assert api.post("/v1/tenants", json=_image_payload("alpha"), headers=auth(api)).status_code == 200
    assert set(engine.containers["hestia-alpha"]["NetworkSettings"]["Networks"]) == {"hestia-tenants-alpha"}


def _deployed(tmp_path, monkeypatch, *slugs):
    engine = FakeEngine().install(monkeypatch)
    settings = _docker_settings(tmp_path)
    api = TestClient(build_app(settings))
    for slug in slugs:
        assert api.post("/v1/tenants", json=_image_payload(slug), headers=auth(api)).status_code == 200
    return engine, settings


def test_reconcile_rebuilds_a_tenant_whose_own_network_was_tampered_with(tmp_path, monkeypatch) -> None:
    engine, settings = _deployed(tmp_path, monkeypatch, "alpha")
    engine.networks["hestia-tenants-alpha"]["Internal"] = False
    api = TestClient(build_app(settings))
    assert api.app.state.store.get("alpha").status == "running"
    assert engine.networks["hestia-tenants-alpha"]["Internal"] is True


def test_reconcile_restarts_a_tenant_whose_container_stopped(tmp_path, monkeypatch) -> None:
    engine, settings = _deployed(tmp_path, monkeypatch, "alpha")
    engine.stop_container("hestia-alpha")
    api = TestClient(build_app(settings))
    assert api.app.state.store.get("alpha").status == "running"
    assert engine.containers["hestia-alpha"]["State"]["Running"] is True


def test_reconcile_removes_a_tenant_whose_image_was_withdrawn(tmp_path, monkeypatch) -> None:
    engine, settings = _deployed(tmp_path, monkeypatch, "alpha")
    api = TestClient(build_app(Settings(**{**settings.__dict__, "allow_image_digests": frozenset()})))
    row = api.app.state.store.get("alpha")
    assert row.status == QUARANTINED and "no longer" in row.last_error
    assert "hestia-alpha" not in engine.containers


def test_reconcile_replaces_a_container_running_another_image(tmp_path, monkeypatch) -> None:
    engine, settings = _deployed(tmp_path, monkeypatch, "alpha")
    engine.containers["hestia-alpha"]["Config"]["Image"] = "evil:latest"
    old_id = engine.containers["hestia-alpha"]["Id"]
    TestClient(build_app(settings))
    assert engine.containers["hestia-alpha"]["Id"] != old_id
    assert engine.containers["hestia-alpha"]["Config"]["Image"] == f"hestia-tenant@{DIGEST}"


def test_a_hung_engine_costs_one_probe_not_one_timeout_per_tenant(tmp_path, monkeypatch) -> None:
    engine, settings = _deployed(tmp_path, monkeypatch, "alpha", "beta")
    engine.calls.clear()
    engine.on(lambda a: a[:1] == ["version"], stderr="Cannot connect to the Docker daemon")
    api = TestClient(build_app(settings))
    assert {api.app.state.store.get(s).status for s in ("alpha", "beta")} == {QUARANTINED}
    assert [c[1] for c in engine.calls] == ["version"]


def test_a_quarantined_rows_address_is_never_handed_out(tmp_path, monkeypatch) -> None:
    engine = FakeEngine().install(monkeypatch)
    store = _ledger(tmp_path)
    _image_row(store, "ghost", status=QUARANTINED, url="http://10.231.0.2:8080")
    store.close()
    api = TestClient(build_app(_docker_settings(tmp_path)))
    assert api.post("/v1/tenants", json=_image_payload("alpha"), headers=auth(api)).status_code == 200
    assert engine.networks["hestia-tenants-alpha"]["IPAM"]["Config"][0]["Subnet"] != "10.231.0.0/29"


def test_the_edge_rechecks_the_row_before_it_connects(tmp_path, monkeypatch) -> None:
    api = client(tmp_path)
    api.post("/v1/tenants", json=deploy_payload("echo"), headers=auth(api))
    store = api.app.state.store
    import hestia.app as app_module

    original = app_module.edge_forward_headers

    def flip(headers):
        store.set_status("echo", "stopped")  # a stop lands while the body is read
        return original(headers)

    monkeypatch.setattr("hestia.app.edge_forward_headers", flip)
    assert api.get("/t/echo/health").status_code == 503


def test_a_manual_announce_clears_an_old_announce_error(tmp_path, monkeypatch) -> None:
    api = client(tmp_path, hub_url="http://hub.invalid")
    api.post("/v1/tenants", json=deploy_payload("echo"), headers=auth(api))
    api.app.state.store.record_error("echo", "hub said no")
    monkeypatch.setattr("hestia.app.hub_announce", lambda **_k: {"ok": True})
    assert api.post("/v1/tenants/echo/announce", headers=auth(api)).status_code == 200
    assert api.app.state.store.get("echo").last_error == ""


def test_one_tenant_the_engine_cannot_inspect_does_not_stop_the_rest(tmp_path, monkeypatch) -> None:
    engine, settings = _deployed(tmp_path, monkeypatch, "alpha", "beta")
    engine.on(
        lambda a: a == ["container", "inspect", "hestia-alpha"],
        stderr="Error response from daemon: context deadline exceeded",
    )
    api = TestClient(build_app(settings))
    ledger = api.app.state.store
    assert ledger.get("alpha").status == QUARANTINED
    assert "cannot verify isolation" in ledger.get("alpha").last_error
    assert ledger.get("beta").status == "running"
