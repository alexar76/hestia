from unittest.mock import MagicMock

from tests.conftest import auth, client, deploy_payload


def test_extra_fields_rejected(tmp_path) -> None:
    api = client(tmp_path)
    payload = deploy_payload()
    payload["surprise"] = True
    res = api.post("/v1/tenants", json=payload, headers=auth(api))
    assert res.status_code == 400


def test_console_is_served(tmp_path) -> None:
    api = client(tmp_path)
    page = api.get("/ui/")
    assert page.status_code == 200
    assert b"HESTIA" in page.content
    assert api.get("/ui").status_code == 200


def test_hearth_capacity(tmp_path) -> None:
    api = client(tmp_path, max_tenants=1)
    first = api.post("/v1/tenants", json=deploy_payload(), headers=auth(api))
    assert first.status_code == 200, first.text
    second = api.post("/v1/tenants", json=deploy_payload(slug="other-bot"), headers=auth(api))
    assert second.status_code == 429
    api.post("/v1/tenants/demo-echo/stop", headers=auth(api))


def test_redeploy_running_tenant(tmp_path) -> None:
    api = client(tmp_path)
    first = api.post("/v1/tenants", json=deploy_payload(), headers=auth(api))
    assert first.status_code == 200
    again = api.post("/v1/tenants", json=deploy_payload(), headers=auth(api))
    assert again.status_code == 200
    api.post("/v1/tenants/demo-echo/stop", headers=auth(api))


def test_announce_with_hub(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "hestia.app.hub_announce",
        lambda **_kwargs: {"ok": True, "note": "observed"},
    )
    api = client(tmp_path, hub_url="http://127.0.0.1:1")
    payload = deploy_payload()
    payload["announce"] = True
    res = api.post("/v1/tenants", json=payload, headers=auth(api))
    assert res.status_code == 200
    assert res.json()["announced"] is True
    knock = api.post("/v1/tenants/demo-echo/announce", headers=auth(api))
    assert knock.status_code == 200
    api.post("/v1/tenants/demo-echo/stop", headers=auth(api))


def test_announce_http_failure_keeps_tenant(tmp_path, monkeypatch) -> None:
    from hestia.hub import AnnounceError

    def boom(**_kwargs):
        raise AnnounceError("Hub announce failed")

    monkeypatch.setattr("hestia.app.hub_announce", boom)
    api = client(tmp_path, hub_url="http://127.0.0.1:1")
    payload = deploy_payload()
    payload["announce"] = True
    res = api.post("/v1/tenants", json=payload, headers=auth(api))
    assert res.status_code == 200
    assert res.json()["announced"] is False
    listed = api.get("/v1/tenants", headers=auth(api)).json()
    assert listed["tenants"][0]["status"] == "running"
    api.post("/v1/tenants/demo-echo/stop", headers=auth(api))


def test_edge_unhealthy_tenant(tmp_path) -> None:
    api = client(tmp_path)
    created = api.post("/v1/tenants", json=deploy_payload(), headers=auth(api))
    assert created.status_code == 200
    api.app.state.stub.stop("demo-echo")
    res = api.get("/t/demo-echo/health")
    assert res.status_code == 502
    api.post("/v1/tenants/demo-echo/stop", headers=auth(api))


def test_invoke_deploy_rejects_bad_body(tmp_path) -> None:
    api = client(tmp_path)
    res = api.post(
        "/ai-market/v2/invoke",
        headers=auth(api),
        json={"capability_id": "hestia.host.deploy@v1", "input": {"slug": "x"}},
    )
    assert res.status_code == 400


def test_body_too_large(tmp_path) -> None:
    api = client(tmp_path)
    res = api.post(
        "/v1/tenants",
        headers={**auth(api), "content-length": str(300 * 1024)},
        content=b"{}",
    )
    assert res.status_code == 413


def test_list_tenants_requires_token(tmp_path) -> None:
    api = client(tmp_path)
    assert api.get("/v1/tenants").status_code == 401


def test_unknown_tenant_and_capability(tmp_path) -> None:
    api = client(tmp_path)
    assert api.get("/v1/tenants/missing").status_code == 404
    assert api.post("/v1/tenants/missing/stop", headers=auth(api)).status_code == 404
    unknown = api.post(
        "/ai-market/v2/invoke",
        json={"capability_id": "hestia.host.nope@v1", "input": {}},
    )
    assert unknown.status_code == 404


def test_announce_without_hub_does_not_list(tmp_path) -> None:
    api = client(tmp_path)
    payload = deploy_payload()
    payload["announce"] = True
    res = api.post("/v1/tenants", json=payload, headers=auth(api))
    assert res.status_code == 200
    body = res.json()
    assert body["announced"] is False
    assert "hearth URL" in body["note"]
    listed = api.get("/v1/tenants", headers=auth(api)).json()
    assert listed["tenants"][0]["last_error"]
    api.post("/v1/tenants/demo-echo/stop", headers=auth(api))


def test_announce_endpoint_requires_hub(tmp_path) -> None:
    api = client(tmp_path)
    api.post("/v1/tenants", json=deploy_payload(), headers=auth(api))
    res = api.post("/v1/tenants/demo-echo/announce", headers=auth(api))
    assert res.status_code == 400
    api.post("/v1/tenants/demo-echo/stop", headers=auth(api))


def test_themis_unreachable_blocks_deploy(tmp_path) -> None:
    api = client(tmp_path, themis_url="http://127.0.0.1:9")
    res = api.post("/v1/tenants", json=deploy_payload(), headers=auth(api))
    assert res.status_code == 403
    assert api.get("/v1/hearth").json()["tenants"] == []


def test_invoke_deploy_status_stop(tmp_path) -> None:
    api = client(tmp_path)
    created = api.post(
        "/ai-market/v2/invoke",
        headers=auth(api),
        json={"capability_id": "hestia.host.deploy@v1", "input": deploy_payload()},
    )
    assert created.status_code == 200, created.text
    assert created.json()["result"]["slug"] == "demo-echo"
    status = api.post(
        "/ai-market/v2/invoke",
        json={"capability_id": "hestia.host.status@v1", "input": {"slug": "demo-echo"}},
    )
    assert status.json()["result"]["slug"] == "demo-echo"
    stopped = api.post(
        "/ai-market/v2/invoke",
        headers=auth(api),
        json={"capability_id": "hestia.host.stop@v1", "input": {"slug": "demo-echo"}},
    )
    assert stopped.json()["result"]["status"] == "stopped"


def test_docker_start_uses_locked_argv(monkeypatch) -> None:
    from hestia.models import CapabilitySpec
    from hestia.policy import IsolationProfile
    from hestia.runtime.docker import DockerRuntime

    runs: list[list[str]] = []

    def fake_run(argv, **kwargs):
        runs.append(list(argv))
        return MagicMock(returncode=0, stdout="")

    monkeypatch.setattr("hestia.runtime.docker.subprocess.run", fake_run)
    monkeypatch.setattr("hestia.runtime.docker.shutil.which", lambda _bin: "/usr/bin/docker")
    digest = "sha256:" + ("ab" * 32)
    runtime = DockerRuntime(
        "docker",
        frozenset({digest}),
        client_env={"DOCKER_HOST": "tcp://dind:2376"},
    )
    running = runtime.start(
        slug="safe",
        capability=CapabilitySpec(
            product_id="x",
            capability_id="x.y@v1",
            name="x",
            description="enough",
            price_per_call_usd=0,
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            publisher_id="t",
        ),
        handler="",
        image_digest=digest,
        profile=IsolationProfile(
            cpus="0.5",
            memory_mb=256,
            pids=64,
            user="65532:65532",
            read_only=True,
            no_new_privileges=True,
            cap_drop=("ALL",),
            tmpfs_mb=32,
            network="none",
            egress_allowlist=(),
        ),
        env={},
    )
    assert running.handle == "hestia-safe"
    run_argv = next(a for a in runs if len(a) > 1 and a[1] == "run")
    joined = " ".join(run_argv)
    assert "--network none" in joined
    assert "--cap-drop ALL" in joined
    assert "/var/run/docker.sock" not in joined
    assert "--privileged" not in run_argv
    assert "-p" not in run_argv
    assert "host" != run_argv[run_argv.index("--network") + 1]
    runtime.stop(running.handle)
    assert any(len(a) > 1 and a[1] == "rm" for a in runs)
