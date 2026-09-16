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
    settings = Settings.from_env()
    assert settings.port == 9480
    assert settings.runtime == "stub"
    assert settings.deploy_token == ""
    assert settings.replicas == 1
    assert settings.docker_host == ""
    assert settings.allow_host_docker is False
    assert settings.docker_client_env() == {}


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
