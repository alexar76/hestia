from __future__ import annotations

from pathlib import Path

import pytest

from hestia.config import Settings
from hestia.docker_host import (
    HOST_SOCKET_REFUSAL,
    assert_nested_docker_host,
    assert_operator_docker_host,
    client_env,
    is_host_docker_socket,
    resolve_allow_host_docker,
    resolve_docker_cert_path,
    resolve_docker_host,
    resolve_docker_tls_verify,
)


@pytest.mark.parametrize(
    "host,expected",
    [
        ("", True),
        ("   ", True),
        ("unix:///var/run/docker.sock", True),
        ("unix:///run/docker.sock", True),
        ("UNIX:///var/run/docker.sock", True),
        ("tcp://dind:2376", False),
        ("tcp://127.0.0.1:2376", False),
        ("ssh://operator@hearth", False),
    ],
)
def test_is_host_docker_socket(host: str, expected: bool) -> None:
    assert is_host_docker_socket(host) is expected


def test_assert_operator_docker_host_skips_stub() -> None:
    assert assert_operator_docker_host("", allow_host_docker=False, runtime="stub") == ""
    assert (
        assert_operator_docker_host(
            "unix:///var/run/docker.sock", allow_host_docker=False, runtime="stub"
        )
        == "unix:///var/run/docker.sock"
    )
    assert assert_nested_docker_host is assert_operator_docker_host


def test_assert_operator_docker_host_refuses_default_and_sock() -> None:
    with pytest.raises(RuntimeError, match="shell onto the host"):
        assert_operator_docker_host("", allow_host_docker=False, runtime="docker")
    with pytest.raises(RuntimeError, match="HESTIA_ALLOW_HOST_DOCKER"):
        assert_operator_docker_host(
            "unix:///var/run/docker.sock", allow_host_docker=False, runtime="docker"
        )
    assert HOST_SOCKET_REFUSAL.startswith("host docker.sock")


def test_assert_operator_docker_host_host_process_opt_in() -> None:
    assert (
        assert_operator_docker_host(
            "unix:///var/run/docker.sock", allow_host_docker=True, runtime="docker"
        )
        == "unix:///var/run/docker.sock"
    )
    assert (
        assert_operator_docker_host("tcp://127.0.0.1:2376", allow_host_docker=False, runtime="docker")
        == "tcp://127.0.0.1:2376"
    )


def test_resolve_docker_host_prefers_hestia(monkeypatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://other:2376")
    monkeypatch.setenv("HESTIA_DOCKER_HOST", "tcp://dind:2376")
    assert resolve_docker_host() == "tcp://dind:2376"
    monkeypatch.delenv("HESTIA_DOCKER_HOST")
    assert resolve_docker_host() == "tcp://other:2376"
    monkeypatch.delenv("DOCKER_HOST")
    assert resolve_docker_host() == ""


def test_resolve_tls_and_certs(monkeypatch) -> None:
    monkeypatch.delenv("HESTIA_DOCKER_TLS_VERIFY", raising=False)
    monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
    monkeypatch.delenv("HESTIA_DOCKER_CERT_PATH", raising=False)
    monkeypatch.delenv("DOCKER_CERT_PATH", raising=False)
    monkeypatch.delenv("HESTIA_ALLOW_HOST_DOCKER", raising=False)
    assert resolve_docker_tls_verify() is False
    assert resolve_docker_cert_path() == ""
    assert resolve_allow_host_docker() is False
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/certs/client")
    assert resolve_docker_tls_verify() is True
    assert resolve_docker_cert_path() == "/certs/client"
    monkeypatch.setenv("HESTIA_DOCKER_TLS_VERIFY", "0")
    monkeypatch.setenv("HESTIA_DOCKER_CERT_PATH", "/hestia/certs")
    monkeypatch.setenv("HESTIA_ALLOW_HOST_DOCKER", "yes")
    assert resolve_docker_tls_verify() is False
    assert resolve_docker_cert_path() == "/hestia/certs"
    assert resolve_allow_host_docker() is True


def test_client_env_omits_empty() -> None:
    assert client_env(host="", tls_verify=False, cert_path="") == {}
    assert client_env(host="tcp://dind:2376", tls_verify=True, cert_path="/certs/client") == {
        "DOCKER_HOST": "tcp://dind:2376",
        "DOCKER_TLS_VERIFY": "1",
        "DOCKER_CERT_PATH": "/certs/client",
    }


def test_from_env_refuses_host_sock_for_docker_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_RUNTIME", "docker")
    monkeypatch.delenv("HESTIA_DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("HESTIA_ALLOW_HOST_DOCKER", raising=False)
    with pytest.raises(RuntimeError, match="docker.sock"):
        Settings.from_env()
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    with pytest.raises(RuntimeError, match="shell onto the host"):
        Settings.from_env()


def test_from_env_allows_tcp_and_host_process_opt_in(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_RUNTIME", "docker")
    monkeypatch.setenv("HESTIA_DOCKER_HOST", "tcp://dind:2376")
    monkeypatch.setenv("HESTIA_DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("HESTIA_DOCKER_CERT_PATH", "/certs/client")
    monkeypatch.setenv("HESTIA_ALLOW_HOST_DOCKER", "0")
    settings = Settings.from_env()
    assert settings.docker_host == "tcp://dind:2376"
    assert settings.docker_tls_verify is True
    assert settings.docker_cert_path == "/certs/client"
    assert settings.allow_host_docker is False
    assert settings.docker_client_env()["DOCKER_HOST"] == "tcp://dind:2376"
    assert "docker.sock" not in settings.docker_client_env()["DOCKER_HOST"]

    monkeypatch.setenv("HESTIA_DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setenv("HESTIA_ALLOW_HOST_DOCKER", "1")
    escaped = Settings.from_env()
    assert escaped.allow_host_docker is True
    assert escaped.docker_host == "unix:///var/run/docker.sock"


def test_compose_never_mounts_host_sock() -> None:
    text = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "/var/run/docker.sock" not in text
    assert "/run/docker.sock" not in text
    assert "privileged: true" not in text
    assert "dind:" not in text
