from pathlib import Path

import pytest

from hestia.config import Settings


def test_bad_runtime_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_RUNTIME", "firecracker")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="stub or docker"):
        Settings.from_env()


def test_from_env_defaults(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.delenv("HESTIA_REPLICAS", raising=False)
    monkeypatch.delenv("HESTIA_DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("HESTIA_ALLOW_HOST_DOCKER", raising=False)
    monkeypatch.delenv("HESTIA_DEPLOY_TOKEN", raising=False)
    monkeypatch.delenv("HESTIA_DATABASE_URL", raising=False)
    monkeypatch.delenv("HESTIA_DOCKER_RUNTIME", raising=False)
    monkeypatch.delenv("HESTIA_REQUIRE_SANDBOX", raising=False)
    monkeypatch.delenv("HESTIA_REQUIRE_GVISOR", raising=False)
    monkeypatch.delenv("HESTIA_PROFILE", raising=False)
    monkeypatch.delenv("HESTIA_MAX_BODY_BYTES", raising=False)
    settings = Settings.from_env()
    assert settings.port == 9480
    assert settings.max_body_bytes == 256 * 1024
    assert settings.runtime == "stub"
    assert settings.deploy_token == ""
    assert settings.replicas == 1
    assert settings.profile == "dev"
    assert settings.database_url == ""
    assert settings.docker_runtime == ""
    assert settings.require_sandbox is False
    assert settings.require_gvisor is False
    assert settings.docker_host == ""
    assert settings.allow_host_docker is False
    assert settings.docker_client_env() == {}


def test_max_body_bytes_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.setenv("HESTIA_MAX_BODY_BYTES", "4096")
    assert Settings.from_env().max_body_bytes == 4096


def test_max_body_bytes_must_be_positive(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    for raw in ("0", "-1"):
        monkeypatch.setenv("HESTIA_MAX_BODY_BYTES", raw)
        with pytest.raises(RuntimeError, match="HESTIA_MAX_BODY_BYTES"):
            Settings.from_env()


def test_from_env_strips_deploy_token(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.setenv("HESTIA_DEPLOY_TOKEN", "  padded-token  ")
    settings = Settings.from_env()
    assert settings.deploy_token == "padded-token"


def test_host_tenant_network_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_TENANT_NETWORK", "host")
    with pytest.raises(RuntimeError, match="HESTIA_TENANT_NETWORK"):
        Settings.from_env()


def test_tenant_network_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.delenv("HESTIA_TENANT_NETWORK", raising=False)
    settings = Settings.from_env()
    assert settings.tenant_network == "hestia-tenants"


def test_multi_replica_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.setenv("HESTIA_REPLICAS", "2")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="HESTIA_REPLICAS"):
        Settings.from_env()


def test_bad_docker_runtime_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_DOCKER_RUNTIME", "firecracker")
    with pytest.raises(RuntimeError, match="HESTIA_DOCKER_RUNTIME"):
        Settings.from_env()


def _docker_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_RUNTIME", "docker")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_DOCKER_HOST", "unix:///run/user/1000/docker.sock")
    monkeypatch.setenv("HESTIA_DOCKER_RUNTIME", "runc")


@pytest.mark.parametrize("value", ["none", "p" * 40, "_under", "net/1"])
def test_a_stub_hearth_ignores_a_tenant_network_it_never_uses(monkeypatch, tmp_path, value) -> None:
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_TENANT_NETWORK", value)
    monkeypatch.setenv("HESTIA_TENANT_SUBNET_POOL", "garbage")
    assert Settings.from_env().tenant_network == value


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("none", False),
        ("-leading", False),
        ("p" * 33, False),
        ("p" * 32, True),
        ("hestia-tenants-net", True),
        ("my.private_net", True),
    ],
)
def test_docker_tenant_network_prefix(monkeypatch, tmp_path, value, ok) -> None:
    _docker_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HESTIA_TENANT_NETWORK", value)
    if ok:
        assert Settings.from_env().tenant_network == value
    else:
        with pytest.raises(RuntimeError, match="HESTIA_TENANT_NETWORK"):
            Settings.from_env()


def test_docker_subnet_pool_is_validated(monkeypatch, tmp_path) -> None:
    _docker_env(monkeypatch, tmp_path)
    assert Settings.from_env().tenant_subnet_pool == "10.231.0.0/16"
    for bad in ("8.8.0.0/16", "fd00::/48", "nonsense"):
        monkeypatch.setenv("HESTIA_TENANT_SUBNET_POOL", bad)
        with pytest.raises(RuntimeError, match="HESTIA_TENANT_SUBNET_POOL"):
            Settings.from_env()
    # 4 /29s cannot hold the default 32 tenants.
    monkeypatch.setenv("HESTIA_TENANT_SUBNET_POOL", "10.9.0.0/27")
    with pytest.raises(RuntimeError, match="fewer than HESTIA_MAX_TENANTS"):
        Settings.from_env()
    monkeypatch.setenv("HESTIA_MAX_TENANTS", "4")
    assert Settings.from_env().tenant_subnet_pool == "10.9.0.0/27"


@pytest.mark.parametrize(
    ("host", "tls", "escape", "ok"),
    [
        ("tcp://dind:2375", "0", "0", False),
        ("tcp://10.0.0.5:2375", "0", "0", False),
        # Dialling loopback says nothing about where the daemon listens.
        ("tcp://127.0.0.1:2375", "0", "0", False),
        ("tcp://[::1]:2375", "0", "0", False),
        # The docker CLI reads a scheme-less host as tcp://.
        ("dind:2375", "0", "0", False),
        ("http://dind:2375", "0", "0", False),
        ("tcp://dind:2376", "1", "0", True),
        ("tcp://dind:2375", "0", "1", True),
        ("ssh://ops@engine", "0", "0", True),
    ],
)
def test_plaintext_tcp_engine_is_refused(monkeypatch, tmp_path, host, tls, escape, ok) -> None:
    _docker_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HESTIA_DOCKER_HOST", host)
    monkeypatch.setenv("HESTIA_DOCKER_TLS_VERIFY", tls)
    monkeypatch.setenv("HESTIA_ALLOW_PLAINTEXT_DOCKER_TCP", escape)
    if ok:
        assert Settings.from_env().docker_host == host
    else:
        with pytest.raises(RuntimeError, match="plaintext TCP"):
            Settings.from_env()


def test_env_example_values_carry_no_inline_comments(monkeypatch, tmp_path) -> None:
    """`docker --env-file` and systemd keep everything after `=`, comment included."""
    example = Path(__file__).resolve().parent.parent / ".env.example"
    for line in example.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        assert " #" not in value and "\t#" not in value, f"{key} has an inline comment"
        if key == "HESTIA_TENANT_NETWORK":
            _docker_env(monkeypatch, tmp_path)
            monkeypatch.setenv(key, value)
            assert Settings.from_env().tenant_network == value


def test_an_empty_docker_host_pins_the_default_context(monkeypatch, tmp_path) -> None:
    """Otherwise the CLI follows `docker context use`, which can be a tcp:// endpoint."""
    _docker_env(monkeypatch, tmp_path)
    monkeypatch.delenv("HESTIA_DOCKER_HOST")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("HESTIA_ALLOW_HOST_DOCKER", "1")
    assert Settings.from_env().docker_client_env() == {"DOCKER_CONTEXT": "default"}
