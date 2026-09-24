"""Isolation profile and per-tenant Docker network policy.

`egress_allowlist` is recorded for a future sidecar. It is NOT applied to Docker
argv. Each image tenant gets its own Internal bridge, with IPv6 off, on a /29
out of HESTIA_TENANT_SUBNET_POOL. It has no public egress and no route to
another tenant.

What an Internal bridge does NOT stop: the bridge's gateway address belongs to
the engine host, and Docker filters FORWARD, not INPUT. A tenant can therefore
open connections to any host process listening on a wildcard address. Every
tenant bridge is named `hst…` so one host rule closes that
(`scripts/tenant-host-firewall.sh`). Hestia cannot install the rule itself: it
may not be root, and the engine may be another machine.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from hestia.config import Settings
from hestia.slugs import validate_slug
from hestia.subnets import (
    DEFAULT_TENANT_SUBNET_POOL,
    NETWORK_PREFIX_RE,
    parse_subnet_pool,
    pick_tenant_subnet,
    subnet_in_pool,
    tenant_bridge_name,
)

logger = logging.getLogger(__name__)

DEFAULT_TENANT_NETWORK_PREFIX = "hestia-tenants"
TENANT_NETWORK_LABEL = "dev.aicom.hestia.tenant"
# One value per `docker run`, so a failed start removes exactly what it created
# and never a same-named container someone else left behind.
TENANT_RUN_LABEL = "dev.aicom.hestia.run"
_BRIDGE_NAME_OPTION = "com.docker.network.bridge.name"
_GATEWAY_MODE_OPTIONS = (
    "com.docker.network.bridge.gateway_mode_ipv4",
    "com.docker.network.bridge.gateway_mode_ipv6",
)
_CREATE_ATTEMPTS = 8
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
_LABEL_RE = re.compile(r"dev\.aicom\.hestia\.[a-z]+=[a-z0-9-]{1,64}")


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
    seccomp: str = "default"
    oci_runtime: str = ""

    def docker_argv(
        self,
        *,
        docker_bin: str,
        name: str,
        image: str,
        env: dict[str, str],
        labels: Mapping[str, str] | None = None,
    ) -> list[str]:
        if refused_network(self.network):
            raise RuntimeError(f"tenant network '{self.network}' is refused")
        argv = [docker_bin, "run"]
        if self.oci_runtime == "runsc":
            argv.extend(["--runtime", "runsc"])
        elif self.oci_runtime and self.oci_runtime != "runc":
            raise RuntimeError(f"oci runtime '{self.oci_runtime}' is not supported")
        argv.extend(
            [
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
                *self._seccomp_args(),
                "--cap-drop",
                "ALL",
                "--network",
                self.network,
                "--tmpfs",
                f"/tmp:rw,noexec,nosuid,size={self.tmpfs_mb}m",
                "--tmpfs",
                f"/data:rw,noexec,nosuid,size={self.tmpfs_mb}m",
            ]
        )
        for key, value in (labels or {}).items():
            if not _LABEL_RE.fullmatch(f"{key}={value}"):
                raise RuntimeError(f"tenant label {key!r} is refused")
            argv.extend(["--label", f"{key}={value}"])
        for key, value in env.items():
            if "docker.sock" in f"{key}={value}":
                raise RuntimeError("docker.sock must not appear in tenant env")
            argv.extend(["--env", f"{key}={value}"])
        argv.append(image)
        assert_safe_docker_argv(argv)
        return argv

    def _seccomp_args(self) -> list[str]:
        """The seccomp portion of the argv.

        `--security-opt seccomp=default` is WRONG: docker has no `default`
        keyword and reads the value as a file path, so every tenant start failed
        — and if a file named `default` sat in the cwd, its contents became the
        profile silently. The daemon applies its built-in default profile
        whenever no seccomp option is given, so "default" must mean OMIT the
        flag. Only an explicit profile PATH is passed through, and unconfined is
        refused outright.
        """
        raw = (self.seccomp or "default").strip() or "default"
        lowered = raw.lower()
        if lowered in {"unconfined", "seccomp=unconfined"}:
            raise RuntimeError("unconfined seccomp is refused")
        if lowered == "default":
            return []  # daemon default profile; naming it as a value breaks docker
        value = raw[len("seccomp="):] if lowered.startswith("seccomp=") else raw
        if value.lower() == "unconfined":
            raise RuntimeError("unconfined seccomp is refused")
        return ["--security-opt", f"seccomp={value}"]



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


def tenant_network_name(slug: str, prefix: str = DEFAULT_TENANT_NETWORK_PREFIX) -> str:
    """The private network of one tenant: `<prefix>-<slug>`."""
    if not NETWORK_PREFIX_RE.fullmatch(prefix or ""):
        raise RuntimeError(
            "HESTIA_TENANT_NETWORK must be a network name prefix: 1–32 of [A-Za-z0-9_.-], "
            "starting with a letter or digit"
        )
    if refused_network(prefix) or prefix.lower() == "none":
        raise RuntimeError(f"tenant network prefix {prefix!r} is refused")
    try:
        if validate_slug(slug) != slug:
            raise ValueError("slug is not in canonical form")
    except ValueError as exc:
        raise RuntimeError(f"invalid tenant slug for a Docker network: {exc}") from exc
    return f"{prefix}-{slug}"


def _docker(
    docker_bin: str,
    args: list[str],
    env: Mapping[str, str] | None,
    *,
    timeout: float,
) -> subprocess.CompletedProcess:
    """Run the operator CLI. An engine that hangs or is missing is an error, never a pass."""
    try:
        return subprocess.run(  # noqa: S603 — argv is docker_bin + fixed verbs
            [docker_bin, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_cli_env(env),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"docker {args[0]} {args[1] if len(args) > 1 else ''} timed out") from exc
    except OSError as exc:
        raise RuntimeError(f"could not run docker: {exc}") from exc


def _tail(text: str | None) -> str:
    return (text or "").strip()[-300:]


def network_missing(stderr: str) -> bool:
    lowered = (stderr or "").lower()
    return bool(re.search(r"network \S+ not found|no such network", lowered))


def container_missing(stderr: str) -> bool:
    # Exactly "no such container". "no such host" (an engine DNS failure) must
    # never read as "the container is gone".
    return "no such container" in (stderr or "").lower()


def inspect_tenant_network(
    docker_bin: str, name: str, env: Mapping[str, str] | None = None
) -> dict | None:
    """The network's inspect document, or None when the engine says it does not exist."""
    result = _docker(docker_bin, ["network", "inspect", name], env, timeout=10)
    if result.returncode != 0:
        if network_missing(result.stderr):
            return None
        raise RuntimeError(f"could not inspect tenant network {name!r}: {_tail(result.stderr)}")
    try:
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError("unexpected docker network inspect response")
        return rows[0]
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"invalid docker network inspect response for {name!r}") from exc


def attached_containers(
    docker_bin: str, name: str, env: Mapping[str, str] | None = None
) -> list[str]:
    """Every container attached to a network, stopped or created ones included.

    `network inspect` lists running endpoints only. A stopped container still
    holds its attachment and rejoins when started, so the check has to ask
    `ps -a`.
    """
    result = _docker(
        docker_bin,
        ["ps", "-a", "--no-trunc", "--filter", f"network={name}", "--format", "{{.Names}}"],
        env,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"could not list containers on tenant network {name!r}: {_tail(result.stderr)}"
        )
    return sorted({line.strip().lstrip("/") for line in result.stdout.splitlines() if line.strip()})


def engine_subnets(
    docker_bin: str, env: Mapping[str, str] | None = None
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Every subnet any network on the engine already uses."""
    listed = _docker(docker_bin, ["network", "ls", "-q", "--no-trunc"], env, timeout=10)
    if listed.returncode != 0:
        raise RuntimeError(f"could not list Docker networks: {_tail(listed.stderr)}")
    ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not ids:
        return []
    inspected = _docker(docker_bin, ["network", "inspect", *ids], env, timeout=15)
    try:
        rows = json.loads(inspected.stdout or "[]")
    except ValueError as exc:
        raise RuntimeError("invalid docker network inspect response") from exc
    if inspected.returncode != 0 and not rows:
        # A network removed between ls and inspect makes the call fail for that
        # id only; an engine error returns nothing at all.
        raise RuntimeError(f"could not inspect Docker networks: {_tail(inspected.stderr)}")
    subnets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for row in rows if isinstance(rows, list) else []:
        for config in ((row or {}).get("IPAM") or {}).get("Config") or []:
            try:
                subnets.append(ipaddress.ip_network(str(config.get("Subnet") or ""), strict=False))
            except ValueError:
                continue
    return subnets


def tenant_network_problem(
    network: dict,
    *,
    slug: str,
    name: str,
    pool: ipaddress.IPv4Network,
    attached: Iterable[str],
) -> str:
    """Why this network is not a private tenant bridge Hestia would create, or ""."""
    labels = network.get("Labels") or {}
    options = network.get("Options") or {}
    if network.get("Name") != name:
        return f"its name is {network.get('Name')!r}"
    if labels.get(TENANT_NETWORK_LABEL) != slug:
        return f"it is not labelled {TENANT_NETWORK_LABEL}={slug}"
    if network.get("Driver") != "bridge":
        return f"driver is {network.get('Driver')!r}, not bridge"
    if network.get("Internal") is not True:
        return "it is not Internal"
    if (network.get("Scope") or "local") != "local":
        return f"scope is {network.get('Scope')!r}, not local"
    if network.get("EnableIPv6"):
        return "IPv6 is enabled"
    if options.get(_BRIDGE_NAME_OPTION) != tenant_bridge_name(name):
        return f"its bridge interface is not {tenant_bridge_name(name)}"
    modes = [key for key in _GATEWAY_MODE_OPTIONS if key in options]
    if modes:
        return f"it sets {', '.join(modes)}"
    configs = (network.get("IPAM") or {}).get("Config") or []
    subnets = [str((config or {}).get("Subnet") or "") for config in configs]
    if len(subnets) != 1 or not subnet_in_pool(subnets[0], pool):
        return f"its subnet {subnets} is not one /29 in {pool}"
    foreign = sorted(set(attached) - {f"hestia-{slug}"})
    if foreign:
        return "other container(s) are attached: " + ", ".join(foreign)
    return ""


def validate_tenant_network(
    network: dict,
    *,
    slug: str,
    name: str,
    pool: ipaddress.IPv4Network | str = DEFAULT_TENANT_SUBNET_POOL,
    attached: Iterable[str] = (),
) -> None:
    net_pool = parse_subnet_pool(pool) if isinstance(pool, str) else pool
    problem = tenant_network_problem(
        network, slug=slug, name=name, pool=net_pool, attached=attached
    )
    if problem:
        raise RuntimeError(f"tenant network {name!r} is not a private Hestia bridge: {problem}")


def _remove_network(docker_bin: str, name: str, env: Mapping[str, str] | None) -> None:
    result = _docker(docker_bin, ["network", "rm", name], env, timeout=15)
    if result.returncode != 0 and not network_missing(result.stderr):
        raise RuntimeError(f"could not remove tenant network {name!r}: {_tail(result.stderr)}")


def ensure_isolated_tenant_network(
    docker_bin: str,
    slug: str,
    prefix: str = DEFAULT_TENANT_NETWORK_PREFIX,
    env: Mapping[str, str] | None = None,
    *,
    pool: ipaddress.IPv4Network | str = DEFAULT_TENANT_SUBNET_POOL,
    reserved_hosts: Iterable[str] = (),
) -> tuple[str, bool]:
    """Create or verify the private Internal bridge of exactly one tenant.

    Returns (network name, created_here). Fails closed: there is no shared network
    and no `none` fallback. A network that already carries this tenant's label
    but is not what Hestia would create now (an older layout, a leftover from a
    crash) is replaced when nothing is attached to it; anything else is refused.
    """
    net_pool = parse_subnet_pool(pool) if isinstance(pool, str) else pool
    reserved = list(reserved_hosts)
    name = tenant_network_name(slug, prefix)
    existing = inspect_tenant_network(docker_bin, name, env)
    if existing is not None:
        attached = attached_containers(docker_bin, name, env)
        problem = tenant_network_problem(
            existing, slug=slug, name=name, pool=net_pool, attached=attached
        )
        if not problem:
            return name, False
        ours = (existing.get("Labels") or {}).get(TENANT_NETWORK_LABEL) == slug
        if not ours or attached:
            raise RuntimeError(f"tenant network {name!r} cannot be used: {problem}")
        logger.warning("replacing stale tenant network %s: %s", name, problem)
        _remove_network(docker_bin, name, env)

    created = False
    refused: set[ipaddress.IPv4Network] = set()
    detail = ""
    for _ in range(_CREATE_ATTEMPTS):
        subnet = pick_tenant_subnet(
            net_pool,
            taken=engine_subnets(docker_bin, env),
            reserved_hosts=reserved,
            skip=refused,
        )
        result = _docker(
            docker_bin,
            [
                "network",
                "create",
                "--driver",
                "bridge",
                "--internal",
                "--ipv6=false",
                "--subnet",
                str(subnet),
                "--opt",
                f"{_BRIDGE_NAME_OPTION}={tenant_bridge_name(name)}",
                "--label",
                f"{TENANT_NETWORK_LABEL}={slug}",
                name,
            ],
            env,
            timeout=15,
        )
        if result.returncode == 0:
            created = True
            break
        detail = _tail(result.stderr)
        if "overlap" in detail.lower():
            # Another network took this range between our listing and the create.
            refused.add(subnet)
            continue
        break
    try:
        network = inspect_tenant_network(docker_bin, name, env)
        if network is None:
            raise RuntimeError(
                f"could not establish isolated tenant network {name!r}"
                + (f": {detail}" if detail else "")
            )
        # A same-named network may have been created concurrently. It is accepted
        # only after the same ownership and isolation checks.
        validate_tenant_network(
            network,
            slug=slug,
            name=name,
            pool=net_pool,
            attached=attached_containers(docker_bin, name, env),
        )
    except Exception:
        if created:
            try:
                _remove_network(docker_bin, name, env)
            except RuntimeError as exc:
                logger.warning("could not remove tenant network %s after a failure: %s", name, exc)
        raise
    return name, created


def remove_isolated_tenant_network(
    docker_bin: str,
    slug: str,
    prefix: str = DEFAULT_TENANT_NETWORK_PREFIX,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Remove a tenant's network when it is ours and nothing, not even a stopped
    container, is attached. Returns whether it was removed."""
    name = tenant_network_name(slug, prefix)
    network = inspect_tenant_network(docker_bin, name, env)
    if network is None:
        return False
    if (network.get("Labels") or {}).get(TENANT_NETWORK_LABEL) != slug:
        return False
    if attached_containers(docker_bin, name, env):
        return False
    _remove_network(docker_bin, name, env)
    return True


def retire_legacy_tenant_network(
    docker_bin: str,
    prefix: str = DEFAULT_TENANT_NETWORK_PREFIX,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Take apart the shared bridge older Hestia versions put every tenant on.

    It was created as `docker network create --internal <prefix>`, unlabelled, and
    only tenants (`hestia-<slug>`) joined it. Only a network that still looks
    exactly like that is touched: named exactly the prefix (inspect also resolves
    ID prefixes), an Internal bridge, no labels, and nothing but tenant containers
    attached. Anything else is someone else's network and is left alone.

    Tracked tenants have been moved off it by the time this runs. What is left
    is containers the ledger does not reach: rows marked stopped whose `docker
    rm` once failed, or containers from a ledger that was replaced. They are
    disconnected rather than removed, so no container Hestia does not track gets
    deleted, and they can no longer reach each other.
    """
    report: dict = {"network": prefix, "disconnected": [], "removed": False, "errors": []}
    network = inspect_tenant_network(docker_bin, prefix, env)
    if network is None:
        return report
    if (
        network.get("Name") != prefix
        or network.get("Driver") != "bridge"
        or network.get("Internal") is not True
        or network.get("Labels")
    ):
        # Not the network older Hestia created. The prefix may simply collide
        # with an operator's own network, or with the start of a network ID.
        report["errors"].append(f"{prefix!r} is not the former Hestia shared bridge; left alone")
        return report
    attached = attached_containers(docker_bin, prefix, env)
    foreign = [name for name in attached if not _is_tenant_container(name)]
    if foreign:
        report["errors"].append(
            "non-tenant container(s) attached, network left alone: " + ", ".join(foreign)
        )
        return report
    for container in attached:
        result = _docker(
            docker_bin, ["network", "disconnect", "-f", prefix, container], env, timeout=15
        )
        if result.returncode == 0:
            report["disconnected"].append(container)
        else:
            report["errors"].append(f"disconnect {container}: {_tail(result.stderr)}")
    if attached_containers(docker_bin, prefix, env):
        report["errors"].append("containers are still attached; network kept")
        return report
    try:
        _remove_network(docker_bin, prefix, env)
        report["removed"] = True
    except RuntimeError as exc:
        report["errors"].append(str(exc))
    return report


def _is_tenant_container(name: str) -> bool:
    if not name.startswith("hestia-"):
        return False
    try:
        validate_slug(name[len("hestia-"):])
    except ValueError:
        return False
    return True


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
    # One spelling only, so the check below sees the value docker will use:
    # docker also accepts `--net`, `--network=x` and `--net=x`.
    for item in argv:
        if item == "--net" or item.startswith(("--net=", "--network=")):
            raise RuntimeError(f"{item.split('=', 1)[0]} is refused; pass --network <name>")
    if argv.count("--network") > 1:
        raise RuntimeError("a tenant joins exactly one network")
    if "--network" in argv:
        idx = argv.index("--network")
        net = argv[idx + 1] if idx + 1 < len(argv) else ""
        if refused_network(net):
            raise RuntimeError(f"tenant network '{net}' is refused")
        if net == "host" or "network=host" in joined or "network host" in joined:
            raise RuntimeError("host networking is refused")
        if "--name" in argv:
            name = argv[argv.index("--name") + 1] if argv.index("--name") + 1 < len(argv) else ""
            slug = name[len("hestia-"):] if name.startswith("hestia-") else ""
            # A named tenant runs on its own `<prefix>-<slug>` network (or none).
            # The bare prefix is the shared bridge older versions used.
            if slug and net != "none" and not net.endswith(f"-{slug}"):
                raise RuntimeError(f"tenant {name} must use its own network, not {net!r}")
    for ns_flag in _HOST_NS_FLAGS:
        if ns_flag in argv:
            idx = argv.index(ns_flag)
            val = argv[idx + 1] if idx + 1 < len(argv) else ""
            if val == "host":
                raise RuntimeError(f"{ns_flag} host is refused")
    if "seccomp=unconfined" in joined or "apparmor=unconfined" in joined:
        raise RuntimeError("unconfined security profiles are refused")
    if "--runtime" in argv:
        idx = argv.index("--runtime")
        runtime = argv[idx + 1] if idx + 1 < len(argv) else ""
        if runtime not in {"runsc"}:
            raise RuntimeError(f"oci runtime '{runtime}' is not supported")
    for item in argv:
        if item.startswith("--runtime="):
            val = item.split("=", 1)[1]
            if val not in {"runsc"}:
                raise RuntimeError(f"oci runtime '{val}' is not supported")


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
        seccomp="default",
        oci_runtime=settings.docker_runtime if settings.docker_runtime == "runsc" else "",
    )
