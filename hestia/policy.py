"""Isolation profile. The same numbers feed the stub docs and docker argv.

`egress_allowlist` is recorded for a future sidecar. It is NOT applied to docker
argv: tenants stay on `none` or the internal `hestia-tenants` bridge. Punching
holes from this list would let tenants hit arbitrary URLs.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from hestia.config import Settings

INTERNAL_TENANT_NETWORK = "hestia-tenants"
_REFUSED_NETWORKS = frozenset({"host", "bridge", "default"})
_REFUSED_FLAGS = frozenset(
    {
        "--privileged",
        "--publish-all",
        "-P",
        "--pid=host",
        "--ipc=host",
        "--uts=host",
        "--userns=host",
        "--cgroupns=host",
        "--add-host",
        "--cap-add",
        "--device",
        "--mount",
    }
)
_HOST_NS_FLAGS = ("--pid", "--ipc", "--uts", "--userns", "--cgroupns")


@dataclass(frozen=True)
class IsolationProfile:
    cpus: str
    memory_mb: int
    pids: int
    user: str
    read_only: bool
    no_new_privileges: bool
    cap_drop: tuple[str, ...]
    tmpfs_mb: int
    network: str
    egress_allowlist: tuple[str, ...]

    def docker_argv(self, *, docker_bin: str, name: str, image: str, env: dict[str, str]) -> list[str]:
        if refused_network(self.network):
            raise RuntimeError(f"tenant network '{self.network}' is refused")
        argv = [
            docker_bin,
            "run",
            "--detach",
            "--name",
            name,
            "--user",
            self.user,
            "--cpus",
            self.cpus,
            "--memory",
            f"{self.memory_mb}m",
            "--memory-swap",
            f"{self.memory_mb}m",
            "--pids-limit",
            str(self.pids),
            "--read-only",
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "--network",
            self.network,
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={self.tmpfs_mb}m",
            "--tmpfs",
            f"/data:rw,noexec,nosuid,size={self.tmpfs_mb}m",
        ]
        for key, value in env.items():
            if "docker.sock" in f"{key}={value}":
                raise RuntimeError("docker.sock must not appear in tenant env")
            argv.extend(["--env", f"{key}={value}"])
        argv.append(image)
        assert_safe_docker_argv(argv)
        return argv


def refused_network(name: str) -> bool:
    lowered = (name or "").strip().lower()
    return (
        lowered in _REFUSED_NETWORKS
        or lowered.startswith("container:")
        or lowered.startswith("service:")
    )


def _cli_env(env: Mapping[str, str] | None) -> dict[str, str] | None:
    if env is None:
        return None
    merged = os.environ.copy()
    merged.update(env)
    return merged


def network_is_internal(docker_bin: str, name: str, env: Mapping[str, str] | None = None) -> bool:
    """True only when `docker network inspect` reports Internal (no default route)."""
    try:
        result = subprocess.run(  # noqa: S603 — argv is docker_bin + fixed inspect
            [docker_bin, "network", "inspect", "-f", "{{.Internal}}", name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_cli_env(env),
        )
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip().lower() in {"true", "1"}


def resolve_tenant_network(
    docker_bin: str, requested: str, env: Mapping[str, str] | None = None
) -> str:
    """Use a named network only when it exists and is Internal. Otherwise `none`.

    Never falls through to host/bridge. Operators create the intended net with:
    `docker network create --internal hestia-tenants`
    """
    name = (requested or "none").strip() or "none"
    if refused_network(name):
        raise RuntimeError(f"tenant network '{name}' is refused")
    if name == "none":
        return "none"
    if network_is_internal(docker_bin, name, env):
        return name
    return "none"


def ensure_internal_tenant_network(
    docker_bin: str, requested: str, env: Mapping[str, str] | None = None
) -> str:
    """Create `--internal hestia-tenants` on the engine the host-process CLI talks to.

    Still refuses host/bridge. Falls back to `none` if create/inspect cannot
    prove Internal. Never used from the compose image (no docker CLI there).
    """
    name = (requested or "none").strip() or "none"
    if refused_network(name):
        raise RuntimeError(f"tenant network '{name}' is refused")
    if name == "none":
        return "none"
    if network_is_internal(docker_bin, name, env):
        return name
    try:
        subprocess.run(  # noqa: S603 — argv is docker_bin + fixed create
            [docker_bin, "network", "create", "--internal", name],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=_cli_env(env),
        )
    except OSError:
        return "none"
    if network_is_internal(docker_bin, name, env):
        return name
    return "none"


def assert_safe_docker_argv(argv: list[str]) -> None:
    """Lock the isolation properties tests and DockerRuntime both rely on."""
    if not argv:
        raise RuntimeError("empty docker argv")
    joined = " ".join(argv)
    if "docker.sock" in joined:
        raise RuntimeError("docker.sock must not appear in tenant argv")
    for flag in _REFUSED_FLAGS:
        if flag in argv:
            raise RuntimeError(f"{flag} is refused for tenants")
    if "-p" in argv or "--publish" in argv:
        raise RuntimeError("publishing tenant ports is refused")
    if "-v" in argv or "--volume" in argv:
        raise RuntimeError("host volume mounts are refused for tenants")
    if "--network" in argv:
        idx = argv.index("--network")
        net = argv[idx + 1] if idx + 1 < len(argv) else ""
        if refused_network(net):
            raise RuntimeError(f"tenant network '{net}' is refused")
        if net == "host" or "network=host" in joined or "network host" in joined:
            raise RuntimeError("host networking is refused")
    for ns_flag in _HOST_NS_FLAGS:
        if ns_flag in argv:
            idx = argv.index(ns_flag)
            val = argv[idx + 1] if idx + 1 < len(argv) else ""
            if val == "host":
                raise RuntimeError(f"{ns_flag} host is refused")
    if "seccomp=unconfined" in joined or "apparmor=unconfined" in joined:
        raise RuntimeError("unconfined security profiles are refused")


def profile_from_settings(settings: Settings) -> IsolationProfile:
    return IsolationProfile(
        cpus=settings.tenant_cpu,
        memory_mb=settings.tenant_memory_mb,
        pids=settings.tenant_pids,
        user="65532:65532",
        read_only=True,
        no_new_privileges=True,
        cap_drop=("ALL",),
        tmpfs_mb=32,
        network=settings.tenant_network,
        egress_allowlist=settings.egress_allowlist,
    )
