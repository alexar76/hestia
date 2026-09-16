"""Locks isolation properties: env, cwd, docker argv, AST, edge headers."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hestia.app import edge_forward_headers
from hestia.config import Settings
from hestia.models import CapabilitySpec
from hestia.policy import (
    IsolationProfile,
    assert_safe_docker_argv,
    ensure_internal_tenant_network,
    profile_from_settings,
    refused_network,
    resolve_tenant_network,
)
from hestia.runtime.stub import StubRuntime, docker_spec_for_tests, minimal_tenant_env
from hestia.scan import HandlerDenied, admit_handler
from hestia.tenant_stub import _load_handler, scrub_operator_env
from tests.conftest import auth, client, deploy_payload


def _cap() -> CapabilitySpec:
    return CapabilitySpec(
        product_id="x",
        capability_id="x.y@v1",
        name="x",
        description="enough",
        price_per_call_usd=0,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        publisher_id="t",
    )


def _profile(*, network: str = "none") -> IsolationProfile:
    return IsolationProfile(
        cpus="0.5",
        memory_mb=256,
        pids=64,
        user="65532:65532",
        read_only=True,
        no_new_privileges=True,
        cap_drop=("ALL",),
        tmpfs_mb=32,
        network=network,
        egress_allowlist=("modelmarket.dev",),
    )


def test_minimal_tenant_env_drops_operator_secrets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HESTIA_DEPLOY_TOKEN", "super-secret")
    monkeypatch.setenv("AIMARKET_PROVIDER_IDENTITY_FILE", str(tmp_path / "provider.key"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-xxx")
    home = tmp_path / "tenants" / "demo"
    home.mkdir(parents=True)
    env = minimal_tenant_env(
        slug="demo",
        cap_file=home / "capability.json",
        key_file=home / "tenant.key",
        handler_file=home / "handler.py",
        bind="127.0.0.1",
        port=9,
        home=home,
        extra={
            "HESTIA_DEPLOY_TOKEN": "also-secret",
            "AIMARKET_FOO": "nope",
            "HESTIA_TENANT_NOTE": "ok",
        },
    )
    assert "HESTIA_DEPLOY_TOKEN" not in env
    assert "AIMARKET_PROVIDER_IDENTITY_FILE" not in env
    assert "AIMARKET_FOO" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["HESTIA_TENANT_SLUG"] == "demo"
    assert env["HESTIA_TENANT_NOTE"] == "ok"
    assert env["HOME"] == str(home)
    assert env["PATH"]
    assert "PYTHONPATH" not in env


def test_stub_start_uses_tenant_cwd_and_filtered_env(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    class Proc:
        def __init__(self) -> None:
            self.stderr = io.StringIO("")

        def poll(self):
            return None

        def terminate(self) -> None:
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self) -> None:
            return None

    def fake_popen(argv, cwd=None, env=None, **_kwargs):
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["env"] = env
        return Proc()

    monkeypatch.setenv("HESTIA_DEPLOY_TOKEN", "leak-me")
    monkeypatch.setenv("AIMARKET_PROVIDER_IDENTITY_FILE", str(tmp_path / "provider.key"))
    monkeypatch.setattr("hestia.runtime.stub.subprocess.Popen", fake_popen)
    monkeypatch.setattr("hestia.runtime.stub._port_open", lambda _port: True)

    runtime = StubRuntime(tmp_path)
    running = runtime.start(
        slug="demo",
        capability=_cap(),
        handler="def handle(payload):\n    return payload\n",
        image_digest="",
        profile=_profile(),
        env={"HESTIA_DEPLOY_TOKEN": "injected"},
    )
    tenant_dir = tmp_path / "tenants" / "demo"
    assert running.handle == "demo"
    assert captured["cwd"] == str(tenant_dir)
    assert not (tmp_path / "provider.key").exists()
    assert not (tenant_dir / "provider.key").exists()
    assert "HESTIA_DEPLOY_TOKEN" not in captured["env"]
    assert "AIMARKET_PROVIDER_IDENTITY_FILE" not in captured["env"]
    assert captured["env"]["HOME"] == str(tenant_dir)
    assert captured["env"]["HESTIA_TENANT_KEY"] == str(tenant_dir / "tenant.key")
    assert not str(captured["env"]["HESTIA_TENANT_KEY"]).endswith("provider.key")
    runtime.stop("demo")


def test_docker_argv_never_host_never_sock_never_publish(tmp_path: Path) -> None:
    settings = Settings.for_test(tmp_path)
    profile = profile_from_settings(settings)
    argv = docker_spec_for_tests(
        docker_bin="docker",
        slug="demo",
        digest="sha256:" + ("ab" * 32),
        profile=profile,
    )
    assert_safe_docker_argv(argv)
    joined = " ".join(argv)
    assert "docker.sock" not in joined
    assert "--privileged" not in argv
    assert "-p" not in argv
    assert "--publish" not in argv
    assert "-P" not in argv
    assert "--publish-all" not in argv
    assert "0.0.0.0:" not in joined
    net = argv[argv.index("--network") + 1]
    assert net in {"none", "hestia-tenants"}
    assert net != "host"
    assert refused_network("host")
    assert not any(h in joined for h in profile.egress_allowlist)


def test_assert_safe_docker_argv_rejects_holes() -> None:
    with pytest.raises(RuntimeError, match="empty"):
        assert_safe_docker_argv([])
    with pytest.raises(RuntimeError, match="docker.sock"):
        assert_safe_docker_argv(["docker", "run", "-v", "/var/run/docker.sock:/var/run/docker.sock"])
    with pytest.raises(RuntimeError, match="volume"):
        assert_safe_docker_argv(["docker", "run", "-v", "/tmp/data:/data", "img"])
    with pytest.raises(RuntimeError, match="publish"):
        assert_safe_docker_argv(["docker", "run", "-p", "0.0.0.0:8080:8080", "img"])
    with pytest.raises(RuntimeError, match="privileged"):
        assert_safe_docker_argv(["docker", "run", "--privileged", "img"])
    with pytest.raises(RuntimeError, match="refused"):
        assert_safe_docker_argv(["docker", "run", "--network", "host", "img"])
    with pytest.raises(RuntimeError, match="refused"):
        assert_safe_docker_argv(["docker", "run", "--network", "container:other", "img"])
    with pytest.raises(RuntimeError, match="--pid host"):
        assert_safe_docker_argv(["docker", "run", "--pid", "host", "img"])
    with pytest.raises(RuntimeError, match="cap-add"):
        assert_safe_docker_argv(["docker", "run", "--cap-add", "SYS_ADMIN", "img"])
    with pytest.raises(RuntimeError, match="device"):
        assert_safe_docker_argv(["docker", "run", "--device", "/dev/sda", "img"])
    with pytest.raises(RuntimeError, match="mount"):
        assert_safe_docker_argv(["docker", "run", "--mount", "type=bind,src=/,dst=/host", "img"])
    with pytest.raises(RuntimeError, match="unconfined"):
        assert_safe_docker_argv(["docker", "run", "--security-opt", "seccomp=unconfined", "img"])
    with pytest.raises(RuntimeError, match="docker.sock"):
        _profile().docker_argv(
            docker_bin="docker",
            name="hestia-x",
            image="img",
            env={"BIND": "unix:///var/run/docker.sock"},
        )


def test_docker_argv_refuses_host_network() -> None:
    with pytest.raises(RuntimeError, match="refused"):
        docker_spec_for_tests(
            docker_bin="docker",
            slug="demo",
            digest="sha256:" + ("ab" * 32),
            profile=_profile(network="host"),
        )


def test_resolve_tenant_network_internal_only(monkeypatch) -> None:
    assert resolve_tenant_network("docker", "none") == "none"
    with pytest.raises(RuntimeError, match="refused"):
        resolve_tenant_network("docker", "host")
    monkeypatch.setattr("hestia.policy.network_is_internal", lambda _bin, name, env=None: name == "hestia-tenants")
    assert resolve_tenant_network("docker", "hestia-tenants") == "hestia-tenants"
    monkeypatch.setattr("hestia.policy.network_is_internal", lambda *_args, **_kw: False)
    assert resolve_tenant_network("docker", "hestia-tenants") == "none"


def test_ensure_internal_tenant_network_creates(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if "inspect" in argv:
            stdout = "true\n" if any("create" in a for a in calls) else "false\n"
            return MagicMock(returncode=0, stdout=stdout)
        return MagicMock(returncode=0, stdout="")

    monkeypatch.setattr("hestia.policy.subprocess.run", fake_run)
    assert ensure_internal_tenant_network("docker", "hestia-tenants") == "hestia-tenants"
    assert any("create" in a and "--internal" in a for a in calls)
    assert ensure_internal_tenant_network("docker", "none") == "none"
    with pytest.raises(RuntimeError, match="refused"):
        ensure_internal_tenant_network("docker", "bridge")
    monkeypatch.setattr("hestia.policy.network_is_internal", lambda *_a, **_k: True)
    assert ensure_internal_tenant_network("docker", "hestia-tenants") == "hestia-tenants"


def test_ensure_internal_tenant_network_stays_none(monkeypatch) -> None:
    monkeypatch.setattr("hestia.policy.network_is_internal", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "hestia.policy.subprocess.run",
        lambda *_a, **_k: MagicMock(returncode=1, stdout=""),
    )
    assert ensure_internal_tenant_network("docker", "hestia-tenants") == "none"


def test_network_is_internal_merges_cli_env(monkeypatch) -> None:
    from hestia.policy import network_is_internal

    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return MagicMock(returncode=0, stdout="true\n")

    monkeypatch.setattr("hestia.policy.subprocess.run", fake_run)
    assert network_is_internal("docker", "hestia-tenants", {"DOCKER_HOST": "tcp://dind:2376"}) is True
    assert seen["env"]["DOCKER_HOST"] == "tcp://dind:2376"
    assert "PATH" in seen["env"]


def test_ensure_internal_tenant_network_oserror(monkeypatch) -> None:
    monkeypatch.setattr("hestia.policy.network_is_internal", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "hestia.policy.subprocess.run",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("no docker")),
    )
    assert ensure_internal_tenant_network("docker", "hestia-tenants") == "none"


def test_network_is_internal_reads_inspect(monkeypatch) -> None:
    from hestia.policy import network_is_internal

    monkeypatch.setattr(
        "hestia.policy.subprocess.run",
        lambda *_a, **_k: MagicMock(returncode=0, stdout="true\n"),
    )
    assert network_is_internal("docker", "hestia-tenants") is True
    monkeypatch.setattr(
        "hestia.policy.subprocess.run",
        lambda *_a, **_k: MagicMock(returncode=0, stdout="false\n"),
    )
    assert network_is_internal("docker", "hestia-tenants") is False
    monkeypatch.setattr(
        "hestia.policy.subprocess.run",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("no docker")),
    )
    assert network_is_internal("docker", "hestia-tenants") is False


def test_docker_start_resolves_internal_network(monkeypatch) -> None:
    from hestia.runtime.docker import DockerRuntime

    runs: list[list[str]] = []

    def fake_run(argv, **kwargs):
        runs.append(list(argv))
        return MagicMock(returncode=0, stdout="")

    monkeypatch.setattr("hestia.runtime.docker.subprocess.run", fake_run)
    monkeypatch.setattr("hestia.runtime.docker.shutil.which", lambda _bin: "/usr/bin/docker")
    monkeypatch.setattr("hestia.policy.network_is_internal", lambda *_a, **_k: True)
    digest = "sha256:" + ("ab" * 32)
    runtime = DockerRuntime(
        "docker",
        frozenset({digest}),
        client_env={"DOCKER_HOST": "tcp://dind:2376", "DOCKER_TLS_VERIFY": "1"},
    )
    running = runtime.start(
        slug="safe",
        capability=_cap(),
        handler="",
        image_digest=digest,
        profile=_profile(network="hestia-tenants"),
        env={"HESTIA_DEPLOY_TOKEN": "must-not-appear"},
    )
    run_argv = next(a for a in runs if len(a) > 1 and a[1] == "run")
    assert running.listen_url == "http://hestia-safe:8080"
    joined = " ".join(run_argv)
    assert "--network hestia-tenants" in joined
    assert "host" != run_argv[run_argv.index("--network") + 1]
    assert "docker.sock" not in joined
    assert "HESTIA_DEPLOY_TOKEN" not in joined
    assert "-p" not in run_argv
    assert "build" not in run_argv
    inspect_env = None

    def capture(argv, **kwargs):
        nonlocal inspect_env
        inspect_env = kwargs.get("env")
        if "inspect" in argv:
            return MagicMock(returncode=0, stdout="10.89.0.4\n")
        return MagicMock(returncode=0, stdout="")

    monkeypatch.setattr("hestia.runtime.docker.subprocess.run", capture)
    running_ip = runtime.start(
        slug="safe",
        capability=_cap(),
        handler="",
        image_digest=digest,
        profile=_profile(network="hestia-tenants"),
        env={},
    )
    assert running_ip.listen_url == "http://10.89.0.4:8080"
    assert inspect_env["DOCKER_HOST"] == "tcp://dind:2376"
    assert "/var/run/docker.sock" not in inspect_env["DOCKER_HOST"]


def test_edge_forward_headers_drop_authorization() -> None:
    forwarded = edge_forward_headers(
        {
            "Authorization": "Bearer test-token",
            "authorization": "Bearer also",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Cookie": "session=nope",
            "X-Forwarded-For": "1.2.3.4",
        }
    )
    assert forwarded == {"Content-Type": "application/json", "Accept": "application/json"}
    assert "Authorization" not in forwarded
    assert "authorization" not in forwarded
    assert "Cookie" not in forwarded


def test_edge_does_not_forward_authorization(tmp_path: Path, monkeypatch) -> None:
    seen: dict = {}

    class FakeResp:
        content = b'{"ok":true}'
        status_code = 200
        headers = {"content-type": "application/json"}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def request(self, method, url, content=None, headers=None):
            seen["headers"] = headers
            seen["url"] = url
            return FakeResp()

    monkeypatch.setattr("hestia.app.httpx.AsyncClient", FakeClient)
    api = client(tmp_path)
    created = api.post("/v1/tenants", json=deploy_payload(), headers=auth(api))
    assert created.status_code == 200, created.text
    res = api.post(
        "/t/demo-echo/invoke",
        json={"hello": "hearth"},
        headers={"Authorization": "Bearer test-token", "X-Secret": "nope"},
    )
    assert res.status_code == 200
    assert "Authorization" not in seen["headers"]
    assert "authorization" not in {k.lower() for k in seen["headers"]}
    assert "X-Secret" not in seen["headers"]
    api.post("/v1/tenants/demo-echo/stop", headers=auth(api))


def test_load_handler_readmits_source(tmp_path: Path) -> None:
    path = tmp_path / "handler.py"
    path.write_text("import os\ndef handle(payload):\n    return payload\n", encoding="utf-8")
    with pytest.raises(HandlerDenied, match="import"):
        _load_handler(path)


def test_scrub_operator_env_removes_tokens() -> None:
    env = {
        "HESTIA_TENANT_SLUG": "demo",
        "HESTIA_CAPABILITY_FILE": "/tmp/cap.json",
        "HESTIA_DEPLOY_TOKEN": "secret",
        "AIMARKET_PROVIDER_IDENTITY_FILE": "/data/provider.key",
        "OPENAI_API_KEY": "sk",
        "PATH": "/bin",
    }
    scrub_operator_env(env)
    assert env["HESTIA_TENANT_SLUG"] == "demo"
    assert env["HESTIA_CAPABILITY_FILE"].endswith("cap.json")
    assert "HESTIA_DEPLOY_TOKEN" not in env
    assert "AIMARKET_PROVIDER_IDENTITY_FILE" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["PATH"] == "/bin"


@pytest.mark.parametrize(
    "src",
    [
        "import operator\n",
        "setattr(x, 'y', 1)\n",
        "type(x)\n",
        "x.__getattribute__('__class__')\n",
        "def handle(__builtins__):\n    return {}\n",
        "dir(x)\n",
        "super()\n",
    ],
)
def test_scanner_blocks_escape_gadgets(src: str) -> None:
    with pytest.raises(HandlerDenied):
        admit_handler(src)
