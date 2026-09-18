"""Where the docker CLI talks — host-process path only.

The compose image has no docker CLI and no socket. A live
`unix:///var/run/docker.sock` inside the Hestia *container* is a shell onto
the host (`docker run -v /:/host --privileged`). That path is refused unless
the operator sets `HESTIA_ALLOW_HOST_DOCKER=1` while running Hestia as a
host process (`python -m hestia`), not as the compose service.

Tenants never receive that socket; this module only gates the operator CLI.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping

_HOST_SOCK_MARKERS = (
    "/var/run/docker.sock",
    "/run/docker.sock",
)

HOST_SOCKET_REFUSAL = (
    "host docker.sock is refused (a live sock is a shell onto the host: "
    "docker run -v /:/host --privileged). "
    "The compose image has no docker CLI and no sock. "
    "Docker runtime is a host-process path: set HESTIA_ALLOW_HOST_DOCKER=1 "
    "when you run python -m hestia on a machine that already has Docker."
)


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def is_host_docker_socket(host: str) -> bool:
    """True when the CLI would use the host engine socket (including the empty default)."""
    lowered = (host or "").strip().lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in _HOST_SOCK_MARKERS)


def resolve_docker_host() -> str:
    return (
        os.environ.get("HESTIA_DOCKER_HOST", "").strip()
        or os.environ.get("DOCKER_HOST", "").strip()
    )


def resolve_docker_tls_verify() -> bool:
    if "HESTIA_DOCKER_TLS_VERIFY" in os.environ:
        return _flag("HESTIA_DOCKER_TLS_VERIFY")
    return _flag("DOCKER_TLS_VERIFY")


def resolve_docker_cert_path() -> str:
    return (
        os.environ.get("HESTIA_DOCKER_CERT_PATH", "").strip()
        or os.environ.get("DOCKER_CERT_PATH", "").strip()
    )


def resolve_allow_host_docker() -> bool:
    return _flag("HESTIA_ALLOW_HOST_DOCKER")


def assert_operator_docker_host(host: str, *, allow_host_docker: bool, runtime: str) -> str:
    """Refuse the host socket unless the operator opted into a host-process CLI."""
    value = (host or "").strip()
    if runtime != "docker":
        return value
    if is_host_docker_socket(value) and not allow_host_docker:
        raise RuntimeError(HOST_SOCKET_REFUSAL)
    return value


# Older name used in early tests/docs. Same gate.
assert_nested_docker_host = assert_operator_docker_host


def client_env(*, host: str, tls_verify: bool, cert_path: str) -> dict[str, str]:
    """Env for the operator `docker` CLI. Never copied into tenant argv."""
    env: dict[str, str] = {}
    if host:
        env["DOCKER_HOST"] = host
    if tls_verify:
        env["DOCKER_TLS_VERIFY"] = "1"
    if cert_path:
        env["DOCKER_CERT_PATH"] = cert_path
    return env


GVISOR_MISSING = (
    "gVisor runsc is required but missing. "
    "Install gVisor and register the runtime, or unset HESTIA_REQUIRE_GVISOR / "
    "HESTIA_DOCKER_RUNTIME=runsc. Silent fallback to runc is refused."
)


def oci_runtime_registered(
    docker_bin: str,
    name: str,
    env: Mapping[str, str] | None = None,
) -> bool:
    """True when the engine lists ``name`` under ``.Runtimes`` (or the name is runc)."""
    if name in {"", "runc"}:
        return True
    merged = os.environ.copy()
    if env:
        merged.update(env)
    try:
        result = subprocess.run(  # noqa: S603 — argv is docker_bin + fixed info
            [docker_bin, "info", "--format", "{{range $k, $v := .Runtimes}}{{$k}}\n{{end}}"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            env=merged,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    names = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return name in names


def runsc_available(docker_bin: str = "docker", env: Mapping[str, str] | None = None) -> bool:
    """True only when the ENGINE lists runsc as a registered runtime.

    A ``runsc`` binary on this machine's PATH used to satisfy this, which answers
    the wrong question twice. What runs a tenant is ``docker run --runtime runsc``
    against the engine named by ``DOCKER_HOST`` — which may be a different machine
    entirely (``tcp://dind:2376``), and which refuses an unregistered runtime no
    matter what is installed locally. Asking the engine is the only check that
    matches what is about to be executed.
    """
    return oci_runtime_registered(docker_bin, "runsc", env)


def resolve_oci_runtime(
    requested: str,
    *,
    require_gvisor: bool,
    docker_bin: str = "docker",
    env: Mapping[str, str] | None = None,
) -> str:
    """Pick runc (empty) or runsc. Auto-detects runsc when present unless runc was forced.

    ``requested`` is already normalized by Settings (empty or ``runsc``).
    Fail closed when gVisor is required and runsc is missing.
    """
    wanted = (requested or "").strip().lower()
    available = runsc_available(docker_bin, env)
    if require_gvisor or wanted == "runsc":
        if not available:
            raise RuntimeError(GVISOR_MISSING)
        return "runsc"
    if wanted == "runc":
        return ""
    if wanted == "" and available:
        return "runsc"
    return ""
