"""Operator configuration. Secrets stay in env; nothing is baked into the image."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from hestia.docker_host import (
    assert_operator_docker_host,
    client_env,
    resolve_allow_host_docker,
    resolve_docker_cert_path,
    resolve_docker_host,
    resolve_docker_tls_verify,
)


def _truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    public_base: str
    data_dir: Path
    runtime: str
    deploy_token: str
    max_tenants: int
    max_body_bytes: int
    tenant_cpu: str
    tenant_memory_mb: int
    tenant_pids: int
    hub_url: str
    auto_announce: bool
    themis_url: str
    admit_review: bool
    docker_bin: str
    docker_host: str
    docker_tls_verify: bool
    docker_cert_path: str
    allow_host_docker: bool
    allow_image_digests: frozenset[str]
    egress_allowlist: tuple[str, ...]
    tenant_network: str
    replicas: int

    @classmethod
    def from_env(cls) -> Settings:
        data = Path(os.environ.get("HESTIA_DATA_DIR", "./data")).resolve()
        public = os.environ.get("HESTIA_PUBLIC_BASE", "http://127.0.0.1:9480").rstrip("/")
        raw_digests = os.environ.get("HESTIA_ALLOW_IMAGE_DIGESTS", "")
        digests = frozenset(
            d.strip().lower() for d in raw_digests.split(",") if d.strip().startswith("sha256:")
        )
        egress = tuple(
            h.strip().lower()
            for h in os.environ.get(
                "HESTIA_EGRESS_ALLOWLIST",
                "modelmarket.dev,metis.modelmarket.dev,uni.modelmarket.dev",
            ).split(",")
            if h.strip()
        )
        runtime = os.environ.get("HESTIA_RUNTIME", "stub").strip().lower()
        if runtime not in {"stub", "docker"}:
            raise RuntimeError("HESTIA_RUNTIME must be stub or docker")
        replicas = int(os.environ.get("HESTIA_REPLICAS", "1"))
        if replicas > 1:
            raise RuntimeError(
                "HESTIA_REPLICAS>1 is refused: the tenant table is process-local. "
                "Run one replica or wait for a shared store."
            )
        tenant_network = os.environ.get("HESTIA_TENANT_NETWORK", "hestia-tenants").strip() or "hestia-tenants"
        lowered_net = tenant_network.lower()
        if lowered_net in {"host", "bridge", "default"} or lowered_net.startswith("container:"):
            raise RuntimeError("HESTIA_TENANT_NETWORK host/bridge/default is refused")
        allow_host_docker = resolve_allow_host_docker()
        docker_host = assert_operator_docker_host(
            resolve_docker_host(),
            allow_host_docker=allow_host_docker,
            runtime=runtime,
        )
        return cls(
            host=os.environ.get("HESTIA_HOST", "127.0.0.1"),
            port=int(os.environ.get("HESTIA_PORT", "9480")),
            public_base=public,
            data_dir=data,
            runtime=runtime,
            deploy_token=os.environ.get("HESTIA_DEPLOY_TOKEN", "").strip(),
            max_tenants=int(os.environ.get("HESTIA_MAX_TENANTS", "32")),
            max_body_bytes=int(os.environ.get("HESTIA_MAX_BODY_BYTES", str(256 * 1024))),
            tenant_cpu=os.environ.get("HESTIA_TENANT_CPUS", "0.5"),
            tenant_memory_mb=int(os.environ.get("HESTIA_TENANT_MEMORY_MB", "256")),
            tenant_pids=int(os.environ.get("HESTIA_TENANT_PIDS", "64")),
            hub_url=os.environ.get("HESTIA_HUB_URL", "").rstrip("/"),
            auto_announce=_truthy("HESTIA_AUTO_ANNOUNCE"),
            themis_url=os.environ.get("HESTIA_THEMIS_URL", "").rstrip("/"),
            admit_review=_truthy("HESTIA_ADMIT_REVIEW"),
            docker_bin=os.environ.get("HESTIA_DOCKER_BIN", "docker"),
            docker_host=docker_host,
            docker_tls_verify=resolve_docker_tls_verify(),
            docker_cert_path=resolve_docker_cert_path(),
            allow_host_docker=allow_host_docker,
            allow_image_digests=digests,
            egress_allowlist=egress,
            tenant_network=tenant_network,
            replicas=replicas,
        )

    @classmethod
    def for_test(cls, data_dir: Path, *, token: str = "test-token") -> Settings:
        return cls(
            host="127.0.0.1",
            port=9480,
            public_base="http://127.0.0.1:9480",
            data_dir=data_dir,
            runtime="stub",
            deploy_token=token,
            max_tenants=8,
            max_body_bytes=256 * 1024,
            tenant_cpu="0.5",
            tenant_memory_mb=256,
            tenant_pids=64,
            hub_url="",
            auto_announce=False,
            themis_url="",
            admit_review=False,
            docker_bin="docker",
            docker_host="tcp://dind:2376",
            docker_tls_verify=False,
            docker_cert_path="",
            allow_host_docker=False,
            allow_image_digests=frozenset(),
            egress_allowlist=("modelmarket.dev",),
            tenant_network="hestia-tenants",
            replicas=1,
        )

    def docker_client_env(self) -> dict[str, str]:
        """Operator CLI env for a host-process docker runtime. Not tenant env."""
        return client_env(
            host=self.docker_host,
            tls_verify=self.docker_tls_verify,
            cert_path=self.docker_cert_path,
        )
