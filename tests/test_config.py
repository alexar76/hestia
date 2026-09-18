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
