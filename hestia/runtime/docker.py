"""Docker runtime. Host-process CLI only. Never mounts docker.sock into a tenant.

Digest-only: the image is the provider. Capability JSON / handler / operator env
are not mounted from the control-plane data dir.

The compose satellite image has no docker CLI and no host socket. This runtime
is for `python -m hestia` on a machine that already has Docker. The CLI env
(`DOCKER_HOST` / TLS) belongs to that host process. It is never copied into
tenant argv. A tenant must not be able to `docker run -v /:/host`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import replace

from hestia.models import CapabilitySpec
from hestia.policy import IsolationProfile, assert_safe_docker_argv, ensure_internal_tenant_network
from hestia.runtime import RunningTenant
from hestia.runtime.stub import docker_spec_for_tests


class DockerRuntime:
    def __init__(
        self,
        docker_bin: str,
        allow_digests: frozenset[str],
        client_env: Mapping[str, str] | None = None,
    ) -> None:
        self.docker_bin = docker_bin
        self.allow_digests = allow_digests
        self.client_env = dict(client_env or {})

    def _cli_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.client_env)
        return env

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
        network = ensure_internal_tenant_network(
            self.docker_bin, profile.network, env=self.client_env
        )
        locked = replace(profile, network=network)
        argv = docker_spec_for_tests(
            docker_bin=self.docker_bin, slug=slug, digest=image_digest, profile=locked
        )
        assert_safe_docker_argv(argv)
        # Sibling tenant on the same engine the host process already uses.
        # `none` if hestia-tenants is missing or not Internal.
        subprocess.run(
            argv, check=True, capture_output=True, text=True, timeout=60, env=self._cli_env()
        )
        handle = f"hestia-{slug}"
        return RunningTenant(
            slug=slug,
            listen_url=self._listen_url(handle),
            handle=handle,
        )

    def _listen_url(self, handle: str) -> str:
        fallback = f"http://{handle}:8080"
        try:
            result = subprocess.run(  # noqa: S603 — argv is docker_bin + fixed inspect
                [
                    self.docker_bin,
                    "inspect",
                    "-f",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                    handle,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=self._cli_env(),
            )
        except OSError:
            return fallback
        raw = result.stdout if isinstance(getattr(result, "stdout", None), str) else ""
        ip = raw.strip().split()[0] if raw.strip() else ""
        if ip and all(ch.isdigit() or ch == "." for ch in ip):
            return f"http://{ip}:8080"
        return fallback

    def stop(self, handle: str) -> None:
        subprocess.run(
            [self.docker_bin, "rm", "-f", handle],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=self._cli_env(),
        )
