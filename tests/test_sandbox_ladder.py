from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hestia.config import Settings
from hestia.docker_host import GVISOR_MISSING, resolve_oci_runtime
from hestia.policy import IsolationProfile, assert_safe_docker_argv, profile_from_settings
from hestia.runtime.stub import docker_spec_for_tests
from tests.conftest import auth, client, deploy_payload


def test_require_sandbox_refuses_stub_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_REQUIRE_SANDBOX", "1")
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="HESTIA_REQUIRE_SANDBOX"):
        Settings.from_env()


def test_require_sandbox_refuses_template_deploy(tmp_path) -> None:
    api = client(tmp_path, require_sandbox=True)
    res = api.post("/v1/tenants", json=deploy_payload(), headers=auth(api))
    assert res.status_code == 400
    assert "REQUIRE_SANDBOX" in res.json()["detail"]


def test_prod_profile_requires_postgres(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_PROFILE", "prod")
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HESTIA_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="HESTIA_DATABASE_URL=postgresql"):
        Settings.from_env()


def test_replicas_refused_with_postgres_and_docker(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_RUNTIME", "docker")
    monkeypatch.setenv("HESTIA_REPLICAS", "2")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_DATABASE_URL", "postgresql://hestia:x@127.0.0.1:5432/hestia")
    monkeypatch.setenv("HESTIA_ALLOW_HOST_DOCKER", "1")
    monkeypatch.setenv("HESTIA_DOCKER_RUNTIME", "runc")
    with pytest.raises(RuntimeError, match="not a farm"):
        Settings.from_env()


def test_replicas_refused_even_with_postgres(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.setenv("HESTIA_REPLICAS", "2")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_DATABASE_URL", "postgresql://hestia:x@127.0.0.1:5432/hestia")
    with pytest.raises(RuntimeError, match="not a farm"):
        Settings.from_env()


def test_bad_database_url_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_DATABASE_URL", "sqlite:///tmp/x.db")
    with pytest.raises(RuntimeError, match="postgresql"):
        Settings.from_env()


def test_gvisor_required_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_RUNTIME", "docker")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_ALLOW_HOST_DOCKER", "1")
    monkeypatch.setenv("HESTIA_DOCKER_RUNTIME", "runsc")
    monkeypatch.setattr("hestia.config.runsc_available", lambda *_a, **_k: False)
    with pytest.raises(RuntimeError, match="runsc"):
        Settings.from_env()
    assert "Silent fallback" in GVISOR_MISSING


def test_resolve_oci_runtime_autodetect_and_runc(monkeypatch) -> None:
    monkeypatch.setattr("hestia.docker_host.runsc_available", lambda *_a, **_k: True)
    assert resolve_oci_runtime("", require_gvisor=False) == "runsc"
    assert resolve_oci_runtime("runc", require_gvisor=False) == ""
    assert resolve_oci_runtime("runsc", require_gvisor=False) == "runsc"
    monkeypatch.setattr("hestia.docker_host.runsc_available", lambda *_a, **_k: False)
    assert resolve_oci_runtime("", require_gvisor=False) == ""
    with pytest.raises(RuntimeError, match="runsc"):
        resolve_oci_runtime("runsc", require_gvisor=True)


def test_docker_argv_omits_seccomp_for_the_default_profile(tmp_path) -> None:
    # `--security-opt seccomp=default` is a docker error: there is no `default`
    # keyword, so the value is read as a file path and every start failed. The
    # daemon applies its built-in profile when the option is absent.
    settings = Settings.for_test(tmp_path)
    argv = docker_spec_for_tests(
        docker_bin="docker",
        slug="demo",
        digest="sha256:" + ("ab" * 32),
        profile=profile_from_settings(settings),
    )
    assert_safe_docker_argv(argv)
    assert not any("seccomp=" in tok for tok in argv)
    assert "no-new-privileges:true" in argv
    assert "--runtime" not in argv


def test_docker_argv_passes_an_explicit_seccomp_path(tmp_path) -> None:
    from dataclasses import replace

    settings = Settings.for_test(tmp_path)
    profile = replace(profile_from_settings(settings), seccomp="/etc/hestia/tenant.json")
    argv = profile.docker_argv(
        docker_bin="docker", name="t", image="img@sha256:" + "a" * 64, env={}
    )
    assert "seccomp=/etc/hestia/tenant.json" in argv


def test_docker_argv_refuses_unconfined_seccomp(tmp_path) -> None:
    from dataclasses import replace

    settings = Settings.for_test(tmp_path)
    for value in ("unconfined", "seccomp=unconfined"):
        profile = replace(profile_from_settings(settings), seccomp=value)
        with pytest.raises(RuntimeError, match="unconfined"):
            profile.docker_argv(
                docker_bin="docker", name="t", image="img@sha256:" + "a" * 64, env={}
            )


def test_docker_argv_runsc_and_refuses_other_runtimes() -> None:
    profile = IsolationProfile(
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
        oci_runtime="runsc",
    )
    argv = profile.docker_argv(docker_bin="docker", name="hestia-x", image="img", env={})
    assert argv[argv.index("--runtime") + 1] == "runsc"
    with pytest.raises(RuntimeError, match="not supported"):
        IsolationProfile(
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
            oci_runtime="kata",
        ).docker_argv(docker_bin="docker", name="x", image="img", env={})
    with pytest.raises(RuntimeError, match="unconfined"):
        IsolationProfile(
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
            seccomp="unconfined",
        ).docker_argv(docker_bin="docker", name="x", image="img", env={})


def test_oci_runtime_info_false_on_oserror(monkeypatch) -> None:
    from hestia.docker_host import oci_runtime_registered

    monkeypatch.setattr(
        "hestia.docker_host.subprocess.run",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("no docker")),
    )
    assert oci_runtime_registered("docker", "runsc") is False
    from hestia.docker_host import runsc_available

    assert runsc_available("docker") is False


def test_oci_runtime_info_parses_runtimes(monkeypatch) -> None:
    from hestia.docker_host import oci_runtime_registered

    monkeypatch.setattr(
        "hestia.docker_host.subprocess.run",
        lambda *_a, **_k: MagicMock(returncode=0, stdout="runc\nrunsc\n"),
    )
    assert oci_runtime_registered("docker", "runsc") is True
    assert oci_runtime_registered("docker", "") is True


class _Info:
    """Stand-in for `docker info --format {{range .Runtimes}}`."""

    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def test_runsc_needs_engine_registration_not_a_binary_on_path(monkeypatch) -> None:
    """A runsc binary on PATH is not the question; the engine's runtime list is.

    What starts a tenant is `docker run --runtime runsc` against DOCKER_HOST,
    which may be another machine and refuses an unregistered runtime regardless
    of what is installed locally.
    """
    import shutil

    from hestia.docker_host import runsc_available

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/runsc")
    monkeypatch.setattr(
        "hestia.docker_host.subprocess.run", lambda *_a, **_k: _Info("runc\n")
    )
    assert runsc_available("docker") is False

    monkeypatch.setattr(
        "hestia.docker_host.subprocess.run", lambda *_a, **_k: _Info("runc\nrunsc\n")
    )
    assert runsc_available("docker") is True


def test_gvisor_gate_probes_the_configured_engine(monkeypatch, tmp_path) -> None:
    """The gate must query DOCKER_HOST, not whatever the default context is."""
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["env"] = kwargs.get("env") or {}
        return _Info("runc\nrunsc\n")

    monkeypatch.setenv("HESTIA_RUNTIME", "docker")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_DOCKER_RUNTIME", "runsc")
    monkeypatch.setenv("HESTIA_DOCKER_HOST", "tcp://dind:2376")
    monkeypatch.setenv("HESTIA_DOCKER_TLS_VERIFY", "1")  # plaintext tcp is refused
    monkeypatch.setattr("hestia.docker_host.subprocess.run", fake_run)
    settings = Settings.from_env()

    assert settings.docker_host == "tcp://dind:2376"
    assert seen["env"].get("DOCKER_HOST") == "tcp://dind:2376"


def test_gvisor_gate_names_the_engine_when_it_refuses(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_RUNTIME", "docker")
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_DOCKER_RUNTIME", "runsc")
    monkeypatch.setenv("HESTIA_DOCKER_HOST", "tcp://dind:2376")
    monkeypatch.setenv("HESTIA_DOCKER_TLS_VERIFY", "1")
    monkeypatch.setattr(
        "hestia.docker_host.subprocess.run", lambda *_a, **_k: _Info("runc\n")
    )
    with pytest.raises(RuntimeError, match="tcp://dind:2376"):
        Settings.from_env()
