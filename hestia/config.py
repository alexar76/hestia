"""Operator configuration. Secrets stay in env; nothing is baked into the image."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from hestia.db import is_postgres_url
from hestia.docker_host import (
    assert_operator_docker_host,
    client_env,
    resolve_allow_host_docker,
    resolve_docker_cert_path,
    resolve_docker_host,
    resolve_docker_tls_verify,
    runsc_available,
)
from hestia.limits import DEFAULT_MAX_BODY_BYTES, resolve_max_body_bytes
from hestia.subnets import (
    DEFAULT_TENANT_SUBNET_POOL,
    NETWORK_PREFIX_RE,
    parse_subnet_pool,
    pool_capacity,
)

# An empty HESTIA_DOCKER_HOST (allowed only with HESTIA_ALLOW_HOST_DOCKER=1) means
# the local default engine. Without pinning the context, the CLI would follow
# whatever `docker context use` last selected, which can be a plaintext tcp://
# endpoint the plaintext-TCP check above never saw.
DEFAULT_ENGINE_ENV = {"DOCKER_CONTEXT": "default"}


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
    max_tenant_response_bytes: int
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
    payments_enabled: bool
    payment_chain: str
    payment_token: str
    payment_token_contract: str
    payment_decimals: int
    payment_rpc_url: str
    payment_min_confirmations: int
    payment_max_age_s: int
    payment_require_binding: bool
    payment_invoice_ttl_s: int
    payment_chain_id: int
    payment_token_eip712_name: str
    payment_token_eip712_version: str
    profile: str
    database_url: str
    require_sandbox: bool
    docker_runtime: str
    require_gvisor: bool
    # Private range tenant /29s are carved from (docker runtime only).
    tenant_subnet_pool: str = DEFAULT_TENANT_SUBNET_POOL

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
        profile = os.environ.get("HESTIA_PROFILE", "dev").strip().lower() or "dev"
        if profile not in {"dev", "prod"}:
            raise RuntimeError("HESTIA_PROFILE must be dev or prod")
        database_url = os.environ.get("HESTIA_DATABASE_URL", "").strip()
        if database_url and not is_postgres_url(database_url):
            raise RuntimeError(
                "HESTIA_DATABASE_URL must be postgresql://… or empty "
                "(SQLite under HESTIA_DATA_DIR)"
            )
        if profile == "prod" and not is_postgres_url(database_url):
            raise RuntimeError(
                "HESTIA_PROFILE=prod requires HESTIA_DATABASE_URL=postgresql://...; "
                "SQLite is tests/dev only"
            )
        require_sandbox = _truthy("HESTIA_REQUIRE_SANDBOX") or profile == "prod"
        if require_sandbox and runtime == "stub":
            raise RuntimeError(
                "HESTIA_REQUIRE_SANDBOX=1 refuses the stub runtime: "
                "template-handler stub is not a sandbox. Set HESTIA_RUNTIME=docker "
                "with an allowlisted digest, or unset HESTIA_REQUIRE_SANDBOX. "
                "HESTIA_PROFILE=prod also refuses stub."
            )
        replicas = int(os.environ.get("HESTIA_REPLICAS", "1"))
        if replicas > 1:
            raise RuntimeError(
                "HESTIA_REPLICAS>1 is refused: tenants still run on this host. "
                "Postgres is a shared ledger, not a farm — there is no sticky "
                "single-runtime-owner. Run one replica."
            )
        docker_runtime = os.environ.get("HESTIA_DOCKER_RUNTIME", "").strip().lower()
        if docker_runtime == "auto":
            docker_runtime = ""
        if docker_runtime not in {"", "runc", "runsc"}:
            raise RuntimeError("HESTIA_DOCKER_RUNTIME must be empty, auto, runc, or runsc")
        if profile == "prod" and runtime == "docker" and "HESTIA_DOCKER_RUNTIME" not in os.environ:
            docker_runtime = "runsc"
        require_gvisor = (
            _truthy("HESTIA_REQUIRE_GVISOR")
            or docker_runtime == "runsc"
            or (profile == "prod" and runtime == "docker" and docker_runtime != "runc")
        )
        docker_bin = os.environ.get("HESTIA_DOCKER_BIN", "docker")
        tenant_network = os.environ.get("HESTIA_TENANT_NETWORK", "hestia-tenants").strip() or "hestia-tenants"
        lowered_net = tenant_network.lower()
        if lowered_net in {"host", "bridge", "default"} or lowered_net.startswith(
            ("container:", "service:")
        ):
            raise RuntimeError("HESTIA_TENANT_NETWORK host/bridge/default is refused")
        max_tenants = int(os.environ.get("HESTIA_MAX_TENANTS", "32"))
        tenant_subnet_pool = (
            os.environ.get("HESTIA_TENANT_SUBNET_POOL", DEFAULT_TENANT_SUBNET_POOL).strip()
            or DEFAULT_TENANT_SUBNET_POOL
        )
        if runtime == "docker":
            # Both only mean something to the docker runtime. A stub hearth must
            # not refuse to start over a value it never uses.
            if lowered_net == "none":
                raise RuntimeError(
                    "HESTIA_TENANT_NETWORK=none is not supported by the docker runtime: every "
                    "tenant gets its own Internal bridge, and the edge reaches it there"
                )
            if not NETWORK_PREFIX_RE.fullmatch(tenant_network):
                raise RuntimeError(
                    "HESTIA_TENANT_NETWORK must be a network name prefix: 1–32 of "
                    "[A-Za-z0-9_.-], starting with a letter or digit"
                )
            pool = parse_subnet_pool(tenant_subnet_pool)
            if pool_capacity(pool) < max_tenants:
                raise RuntimeError(
                    f"HESTIA_TENANT_SUBNET_POOL {pool} holds {pool_capacity(pool)} tenant "
                    f"networks, fewer than HESTIA_MAX_TENANTS={max_tenants}"
                )
        allow_host_docker = resolve_allow_host_docker()
        docker_tls_verify = resolve_docker_tls_verify()
        docker_cert_path = resolve_docker_cert_path()
        docker_host = assert_operator_docker_host(
            resolve_docker_host(),
            allow_host_docker=allow_host_docker,
            runtime=runtime,
            tls_verify=docker_tls_verify,
            allow_plaintext_tcp=_truthy("HESTIA_ALLOW_PLAINTEXT_DOCKER_TCP"),
        )
        # Ask the engine this hearth will actually deploy to. Running this check
        # before docker_host was resolved probed the default docker context, which
        # can be a different engine than DOCKER_HOST names.
        if runtime == "docker" and require_gvisor:
            probe_env = client_env(
                host=docker_host,
                tls_verify=docker_tls_verify,
                cert_path=docker_cert_path,
            )
            if not docker_host:
                probe_env.update(DEFAULT_ENGINE_ENV)
            if not runsc_available(docker_bin, probe_env):
                raise RuntimeError(
                    "gVisor is required (HESTIA_REQUIRE_GVISOR or HESTIA_DOCKER_RUNTIME=runsc) "
                    f"but the Docker engine at {docker_host or 'the default context'} does not "
                    "list runsc as a registered runtime. A runsc binary on PATH is not enough: "
                    "register it with the engine (daemon.json runtimes)."
                )
        payments_enabled = _truthy("HESTIA_PAYMENTS_ENABLED")
        payment_rpc_url = os.environ.get("HESTIA_PAYMENT_RPC_URL", "").strip()
        if payments_enabled and not payment_rpc_url:
            # Without a chain to read, a "paid" call could only be taken on the
            # buyer's word. Refuse to start rather than serve priced work for free
            # while claiming it was paid for.
            raise RuntimeError(
                "HESTIA_PAYMENTS_ENABLED=1 requires HESTIA_PAYMENT_RPC_URL: "
                "payment is verified on chain, never asserted by the caller"
            )
        return cls(
            host=os.environ.get("HESTIA_HOST", "127.0.0.1"),
            port=int(os.environ.get("HESTIA_PORT", "9480")),
            public_base=public,
            data_dir=data,
            runtime=runtime,
            deploy_token=os.environ.get("HESTIA_DEPLOY_TOKEN", "").strip(),
            max_tenants=max_tenants,
            max_body_bytes=resolve_max_body_bytes(),
            max_tenant_response_bytes=int(
                os.environ.get("HESTIA_MAX_TENANT_RESPONSE_BYTES", str(4 * 1024 * 1024))
            ),
            tenant_cpu=os.environ.get("HESTIA_TENANT_CPUS", "0.5"),
            tenant_memory_mb=int(os.environ.get("HESTIA_TENANT_MEMORY_MB", "256")),
            tenant_pids=int(os.environ.get("HESTIA_TENANT_PIDS", "64")),
            hub_url=os.environ.get("HESTIA_HUB_URL", "").rstrip("/"),
            auto_announce=_truthy("HESTIA_AUTO_ANNOUNCE"),
            themis_url=os.environ.get("HESTIA_THEMIS_URL", "").rstrip("/"),
            admit_review=_truthy("HESTIA_ADMIT_REVIEW"),
            docker_bin=docker_bin,
            docker_host=docker_host,
            docker_tls_verify=docker_tls_verify,
            docker_cert_path=docker_cert_path,
            allow_host_docker=allow_host_docker,
            allow_image_digests=digests,
            egress_allowlist=egress,
            tenant_network=tenant_network,
            tenant_subnet_pool=tenant_subnet_pool,
            replicas=replicas,
            profile=profile,
            database_url=database_url,
            require_sandbox=require_sandbox,
            docker_runtime=docker_runtime,
            require_gvisor=require_gvisor,
            payments_enabled=payments_enabled,
            payment_chain=os.environ.get("HESTIA_PAYMENT_CHAIN", "base").strip().lower(),
            payment_token=os.environ.get("HESTIA_PAYMENT_TOKEN", "USDC").strip().upper(),
            # USDC on Base.
            payment_token_contract=os.environ.get(
                "HESTIA_PAYMENT_TOKEN_CONTRACT",
                "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            ).strip(),
            payment_decimals=int(os.environ.get("HESTIA_PAYMENT_DECIMALS", "6")),
            payment_rpc_url=payment_rpc_url,
            payment_min_confirmations=int(
                os.environ.get("HESTIA_PAYMENT_MIN_CONFIRMATIONS", "1")
            ),
            # A raw transfer is not bound to the call, so refuse one older than
            # this many seconds (0 disables). Generous by default — a buyer pays
            # then retries in seconds — while still rejecting a stale, unrelated
            # transfer to a shared payout address replayed as a free call.
            payment_max_age_s=int(os.environ.get("HESTIA_PAYMENT_MAX_AGE_S", "3600")),
            # On by default: a payment must carry the EIP-3009 authorization for
            # the nonce this hearth minted, so it can only pay for the call it was
            # signed for. Turning it off falls back to "any transfer of at least
            # the price to the payout address", which a transfer made for
            # something else can satisfy — only safe when the payout address is
            # dedicated to this hearth.
            payment_require_binding=_truthy("HESTIA_PAYMENT_REQUIRE_BINDING", "1"),
            payment_invoice_ttl_s=int(
                os.environ.get("HESTIA_PAYMENT_INVOICE_TTL_S", "900")
            ),
            # The EIP-712 domain a buyer signs the authorization against. The
            # hearth publishes it in the 402 so buyer tooling never has to guess
            # a token's domain — guessing it produces a signature the contract
            # rejects, which looks like "the hearth refused my payment".
            # Defaults are USDC on Base.
            payment_chain_id=int(os.environ.get("HESTIA_PAYMENT_CHAIN_ID", "8453")),
            payment_token_eip712_name=os.environ.get(
                "HESTIA_PAYMENT_TOKEN_EIP712_NAME", "USD Coin"
            ),
            payment_token_eip712_version=os.environ.get(
                "HESTIA_PAYMENT_TOKEN_EIP712_VERSION", "2"
            ),
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
            max_body_bytes=DEFAULT_MAX_BODY_BYTES,
            max_tenant_response_bytes=4 * 1024 * 1024,
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
            profile="dev",
            database_url="",
            require_sandbox=False,
            docker_runtime="",
            require_gvisor=False,
            payments_enabled=False,
            payment_chain="base",
            payment_token="USDC",
            payment_token_contract="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            payment_decimals=6,
            payment_rpc_url="",
            payment_min_confirmations=1,
            payment_max_age_s=0,
            payment_require_binding=False,
            payment_invoice_ttl_s=900,
            payment_chain_id=8453,
            payment_token_eip712_name="USD Coin",
            payment_token_eip712_version="2",
        )

    def docker_client_env(self) -> dict[str, str]:
        """Operator CLI env for a host-process docker runtime. Not tenant env."""
        env = client_env(
            host=self.docker_host,
            tls_verify=self.docker_tls_verify,
            cert_path=self.docker_cert_path,
        )
        if self.runtime == "docker" and not self.docker_host:
            env.update(DEFAULT_ENGINE_ENV)
        return env
