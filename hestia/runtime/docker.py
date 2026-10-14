"""Docker runtime. Host-process CLI only. Never mounts docker.sock into a tenant.

Digest-only: the image is the provider. Capability JSON / handler / operator env
are not mounted from the control-plane data dir.

The compose satellite image has no docker CLI and no host socket. This runtime
is for `python -m hestia` on a machine that already has Docker. The CLI env
(`DOCKER_HOST` / TLS) belongs to that host process. It is never copied into
tenant argv. A tenant must not be able to `docker run -v /:/host`.

The edge reaches a tenant at its address on the tenant's private bridge, so this
process has to share the engine host's network namespace. `start` proves that
the address answers before it reports success; a remote engine whose bridges
this host cannot route to fails there instead of leaving a dead tenant listed.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace

from hestia.docker_host import resolve_oci_runtime
from hestia.models import CapabilitySpec
from hestia.policy import (
    DEFAULT_TENANT_NETWORK_PREFIX,
    TENANT_RUN_LABEL,
    IsolationProfile,
    assert_safe_docker_argv,
    attached_containers,
    container_missing,
    ensure_isolated_tenant_network,
    inspect_tenant_network,
    remove_isolated_tenant_network,
    retire_legacy_tenant_network,
    tenant_network_name,
    tenant_network_problem,
)
from hestia.runtime import RunningTenant
from hestia.runtime.stub import docker_spec_for_tests
from hestia.subnets import DEFAULT_TENANT_SUBNET_POOL, parse_subnet_pool

logger = logging.getLogger(__name__)

TENANT_PORT = 8080


@dataclass(frozen=True)
class TenantState:
    """What the engine reports for `hestia-<slug>`."""

    running: bool
    networks: tuple[str, ...]
    address: str
    isolated: bool
    problem: str = ""
    image: str = ""

    @property
    def listen_url(self) -> str:
        return f"http://{self.address}:{TENANT_PORT}"


class DockerRuntime:
    def __init__(
        self,
        docker_bin: str,
        allow_digests: frozenset[str],
        client_env: Mapping[str, str] | None = None,
        *,
        oci_runtime: str = "",
        require_gvisor: bool = False,
        network_prefix: str = DEFAULT_TENANT_NETWORK_PREFIX,
        subnet_pool: str = DEFAULT_TENANT_SUBNET_POOL,
        reserved_hosts: Callable[[str], Iterable[str]] | None = None,
        ready_timeout_s: float = 20.0,
    ) -> None:
        self.docker_bin = docker_bin
        self.allow_digests = allow_digests
        self.client_env = dict(client_env or {})
        self.oci_runtime = oci_runtime
        self.require_gvisor = require_gvisor
        self.network_prefix = network_prefix
        self.subnet_pool = parse_subnet_pool(subnet_pool)
        # Addresses live ledger rows of OTHER tenants still point at. A subnet
        # holding one is never reused, or the edge would proxy that row to
        # whoever got the address next.
        self.reserved_hosts = reserved_hosts or (lambda _slug: ())
        self.ready_timeout_s = ready_timeout_s

    def _cli_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.client_env)
        return env

    def _run(self, args: list[str], *, timeout: float) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(  # noqa: S603 — argv is docker_bin + fixed verbs
                [self.docker_bin, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._cli_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"docker {args[0]} timed out after {timeout:g}s") from exc
        except OSError as exc:
            raise RuntimeError(f"could not run docker: {exc}") from exc

    def start(
        self,
        *,
        slug: str,
        capability: CapabilitySpec,
        handler: str,
        image_digest: str,
        profile: IsolationProfile,
        env: dict[str, str],
    ) -> RunningTenant:
        del capability, handler, env
        if image_digest not in self.allow_digests:
            raise RuntimeError("image digest is not on HESTIA_ALLOW_IMAGE_DIGESTS")
        if shutil.which(self.docker_bin) is None:
            raise RuntimeError("docker binary is not on PATH")
        network, created_network = ensure_isolated_tenant_network(
            self.docker_bin,
            slug,
            self.network_prefix,
            env=self.client_env,
            pool=self.subnet_pool,
            reserved_hosts=list(self.reserved_hosts(slug)),
        )
        run_id = secrets.token_hex(8)
        handle = f"hestia-{slug}"
        try:
            runtime = resolve_oci_runtime(
                self.oci_runtime,
                require_gvisor=self.require_gvisor,
                docker_bin=self.docker_bin,
                env=self.client_env,
            )
            locked = replace(profile, network=self.network_prefix, oci_runtime=runtime)
            argv = docker_spec_for_tests(
                docker_bin=self.docker_bin,
                slug=slug,
                digest=image_digest,
                profile=locked,
                run_id=run_id,
            )
            assert_safe_docker_argv(argv)
            if argv[argv.index("--network") + 1] != network:
                raise RuntimeError("tenant argv does not name the tenant's private network")
            result = self._run(argv[1:], timeout=60)
            if result.returncode != 0:
                raise RuntimeError(f"docker run failed: {(result.stderr or '').strip()[-300:]}")
            state = self.tenant_state(slug)
            if state is None or not state.running:
                raise RuntimeError("tenant container is not running after docker run")
            if not state.isolated:
                raise RuntimeError(f"tenant container is not isolated: {state.problem}")
            self._wait_reachable(state.address, TENANT_PORT)
        except Exception:
            self._remove_run(run_id)
            if created_network:
                self._remove_network_quietly(slug)
            raise
        return RunningTenant(slug=slug, listen_url=state.listen_url, handle=handle)

    def _wait_reachable(self, address: str, port: int) -> None:
        """The edge will connect to exactly this address, so prove it answers."""
        deadline = time.monotonic() + self.ready_timeout_s
        while True:
            try:
                with socket.create_connection((address, port), timeout=1.0):
                    return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"tenant at {address}:{port} is not reachable from this host ({exc}). "
                        "The docker runtime needs Hestia on the engine host itself: the edge "
                        "connects to the tenant's private bridge address."
                    ) from exc
            time.sleep(0.2)

    def _remove_run(self, run_id: str) -> None:
        """Remove what one `docker run` created, and nothing else."""
        try:
            listed = self._run(
                ["ps", "-a", "-q", "--no-trunc", "--filter", f"label={TENANT_RUN_LABEL}={run_id}"],
                timeout=15,
            )
            for container_id in listed.stdout.split():
                self._run(["rm", "-f", container_id], timeout=30)
        except RuntimeError as exc:
            logger.warning("could not clean up a failed tenant start: %s", exc)

    def _remove_network_quietly(self, slug: str) -> None:
        try:
            remove_isolated_tenant_network(
                self.docker_bin, slug, self.network_prefix, env=self.client_env
            )
        except RuntimeError as exc:
            # The row is already stopped or failed by then, and the allocator does
            # not reuse this subnet while anything points at it.
            logger.warning("could not remove the network of tenant %s: %s", slug, exc)

    def tenant_state(self, slug: str) -> TenantState | None:
        """The container's state, or None when the engine says it does not exist.

        Raises when the engine cannot answer: an unreachable engine must never
        read as "container gone".
        """
        handle = f"hestia-{slug}"
        result = self._run(["container", "inspect", handle], timeout=15)
        if result.returncode != 0:
            if container_missing(result.stderr):
                return None
            raise RuntimeError(
                f"could not inspect tenant container {handle!r}: {(result.stderr or '').strip()[-300:]}"
            )
        try:
            rows = json.loads(result.stdout)
            data = rows[0] if isinstance(rows, list) else rows
            running = bool(((data.get("State") or {}).get("Running")))
            image = str(((data.get("Config") or {}).get("Image")) or "")
            networks = ((data.get("NetworkSettings") or {}).get("Networks")) or {}
            if not isinstance(networks, dict):
                raise ValueError("networks is not a mapping")
        except (ValueError, TypeError, IndexError, AttributeError) as exc:
            raise RuntimeError(f"invalid inspect data for tenant container {handle!r}") from exc
        name = tenant_network_name(slug, self.network_prefix)
        address = str((networks.get(name) or {}).get("IPAddress") or "")
        problem = ""
        if set(networks) != {name}:
            problem = f"attached to {sorted(networks)}, expected only {name}"
        else:
            network = inspect_tenant_network(self.docker_bin, name, self.client_env)
            if network is None:
                problem = f"network {name} does not exist"
            else:
                problem = tenant_network_problem(
                    network,
                    slug=slug,
                    name=name,
                    pool=self.subnet_pool,
                    attached=attached_containers(self.docker_bin, name, self.client_env),
                )
        if not problem:
            try:
                ipaddress.IPv4Address(address)
            except ValueError:
                problem = f"no IPv4 address on {name}"
        return TenantState(
            running=running,
            networks=tuple(sorted(networks)),
            address=address,
            isolated=not problem,
            problem=problem,
            image=image,
        )

    def engine_problem(self) -> str:
        """"" when the engine answers within a few seconds, else why not.

        Asked once before a reconcile, so a hung engine costs one short timeout
        rather than one long timeout per tenant while nothing is being served.
        """
        try:
            result = self._run(["version", "--format", "{{.Server.Version}}"], timeout=5)
        except RuntimeError as exc:
            return str(exc)
        if result.returncode != 0 or not (result.stdout or "").strip():
            return (result.stderr or "no server version").strip()[-300:]
        return ""

    def is_isolated(self, slug: str) -> bool:
        """Whether `hestia-<slug>` runs attached only to its own private network."""
        state = self.tenant_state(slug)
        return state is not None and state.running and state.isolated

    def stop(self, handle: str) -> None:
        """Remove the container. Raises only when it may still exist.

        The network is cleaned up after that on a best-effort basis: a stuck
        `network rm` must not leave the ledger saying `running` for a container
        that is already gone.
        """
        result = self._run(["rm", "-f", handle], timeout=30)
        if result.returncode != 0:
            detail = (result.stderr or "").strip()
            if container_missing(detail):
                pass
            elif "already in progress" in detail.lower():
                self._wait_removed(handle)
            else:
                raise RuntimeError(f"could not remove tenant container {handle!r}: {detail[-300:]}")
        if handle.startswith("hestia-"):
            self._remove_network_quietly(handle[len("hestia-"):])

    def _wait_removed(self, handle: str, timeout_s: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            probe = self._run(["container", "inspect", handle], timeout=10)
            if probe.returncode != 0 and container_missing(probe.stderr):
                return
            time.sleep(0.2)
        raise RuntimeError(f"tenant container {handle!r} is still being removed")

    def retire_legacy_network(self) -> dict:
        """Disconnect everything from the old shared `<prefix>` bridge and remove it."""
        return retire_legacy_tenant_network(
            self.docker_bin, self.network_prefix, env=self.client_env
        )
