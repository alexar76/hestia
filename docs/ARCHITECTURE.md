# Architecture

HESTIA is a **single-replica hearth control plane** plus an **invoke edge**. It hosts capability providers on the operator’s machines. It does not list them on Hub. Postgres may share the ledger; tenant processes still live on one runtime host.

## Components

| Path | Responsibility |
|---|---|
| `hestia/app.py` | FastAPI: `/` landing, health, well-known, manifest, hearth roster, admin overview, deploy/stop/announce, SKU invoke, `/t/{slug}` proxy, `/ui/`, `/ui/admin/` |
| `hestia/config.py` | Env settings. `HESTIA_REPLICAS>1` refuses to start. `HESTIA_REQUIRE_SANDBOX=1` refuses stub. `HESTIA_TENANT_NETWORK` host/bridge refused; prefix and `HESTIA_TENANT_SUBNET_POOL` validated for docker only. Host `docker.sock` refused unless `HESTIA_ALLOW_HOST_DOCKER=1`; plaintext non-loopback `tcp://` engine refused |
| `hestia/docker_host.py` | Operator CLI gate + gVisor `runsc` detect / fail-closed |
| `hestia/db.py` / `hestia/migrations.py` / `hestia/store.py` | SQLite default, Postgres when `HESTIA_DATABASE_URL` is set. Revision `001_tenants` |
| `hestia/scan.py` | AST **admission** for template handlers (not a sandbox) |
| `hestia/policy.py` | Isolation numbers, per-tenant network create/verify/remove, retiring the former shared bridge, `assert_safe_docker_argv` |
| `hestia/subnets.py` | Per-tenant /29 allocation from `HESTIA_TENANT_SUBNET_POOL`, `hst…` bridge names. Pure functions |
| `hestia/runtime/stub.py` | Loopback subprocess of `hestia.tenant_stub`. Filtered env, per-tenant cwd. Default in compose |
| `hestia/runtime/docker.py` | `docker` CLI as a **host process**. Allowlisted digest only. Optional `--runtime=runsc`. No data_dir mount |
| `hestia/tenant_stub.py` | Sealed HTTP `/health` + `/invoke` + Ed25519. Re-admits handler. Scrubs operator env |
| `hestia/signing.py` | Persistent Ed25519 identity (`provider.key`, `0600`, no symlinks) |
| `hestia/ssrf.py` | Fail-closed URL checks for operator Hub / THEMIS targets |
| `hestia/themis.py` | Optional admit. Unreachable fails closed |
| `hestia/hub.py` | Optional federation announce. Observation, not trust |
| `hestia/slugs.py` | Public path segments: `[a-z0-9]+(-[a-z0-9]+)*`, reserved `admin` / `ui` / `t` / … |
| `console/` | Operator UI for **this** host: `/ui/` deploy, `/ui/admin/` token-locked watch |
| `docs/landing/` | Public 3D landing served at `/` (also GitHub Pages) |

## Request path

```text
Buyer or operator
  → HTTPS ingress
    → HESTIA control plane  (:9480)
         GET  /                       3D hearth landing (HTML)
         GET  /v1/hearth              public roster of THIS host
         GET  /v1/admin/overview      Bearer: running/stopped/quarantined counts, no listen_url
         POST /v1/admin/reconcile     Bearer: re-verify image tenants (docker runtime)
         POST /v1/tenants             Bearer deploy token
         POST /ai-market/v2/invoke    SKUs (deploy/stop require token)
         *    /t/{slug}/…             edge proxy → tenant listen_url
              stub:   127.0.0.1:<ephemeral>
              docker: http://<bridge IP>:8080 on hestia-tenants-{slug}
                      (own Internal /29, IPv6 off; proven reachable at start)
```

The edge (`hestia.app.edge_forward_headers`) copies only `Content-Type` and `Accept`.
`Authorization` is not forwarded.

## Deploy gate chain

1. Bearer token (empty token ⇒ 401)
2. Strict Pydantic body (`extra=forbid`), slug rules, body size cap
3. Template handler AST admit **or** image digest on the allowlist
4. Optional THEMIS: `approve` required; `review` only if `HESTIA_ADMIT_REVIEW=1`; unreachable ⇒ 403
5. Runtime start (stub subprocess or docker argv)
6. Ledger upsert
7. Announce **only** if requested **and** `HESTIA_HUB_URL` is set

## Runtimes

### stub (default)

`StubRuntime.start` writes sealed files under `data_dir/tenants/{slug}/` (`capability.json`,
optional `handler.py`, child-minted `tenant.key`). cwd is that directory. Child env comes from
`minimal_tenant_env` — not `os.environ.copy()`. The hearth key stays at `data_dir/provider.key`.
CPU/memory/pids from `IsolationProfile` are **not** applied. This is not a VM.

Compose (`docker compose up`) is this runtime: one unprivileged box, tenants inside it.
The Hestia box cannot drive the host. Tenants cannot either.

### docker

`DockerRuntime.start` drops `capability`, `handler`, and operator `env`. Only an allowlisted
`sha256:` digest starts. This path is a **host process** (`python -m hestia` + docker on PATH),
not the compose service. The compose image has no docker CLI and no socket — a live sock
inside that container would be a shell onto the host.

Each image tenant gets its own Internal bridge, `hestia-tenants-{slug}` by default, labelled
for that slug, with IPv6 off and a /29 Hestia allocates from `HESTIA_TENANT_SUBNET_POOL`
(`hestia/subnets.py`). Letting Docker choose would take a whole default pool per tenant (the
engine runs out near 30), and a freed subnet would go back to the engine-wide pool, where the
next network anyone creates could inherit an address a stale ledger row still points at. The
allocator never hands out a /29 holding an address a live row of another tenant points at.

Before a start Hestia verifies label, driver, Internal, scope, IPv6, the `hst…` bridge name,
the subnet, and every attached container, stopped ones included (`ps -a`, since `network
inspect` lists running endpoints only). A network carrying the tenant's label with nothing
attached is rebuilt; anything else is refused. There is no shared network and no `none`
fallback. `start` then proves the tenant's address answers from this process: the edge
connects to that bridge IP, so the control plane must run on the engine host. A failed start
removes exactly the container its own `docker run` created (a per-run label) and the network
if it created it.

At boot (and on `POST /v1/admin/reconcile`), `reconcile_docker_tenants` checks every image row.
A tenant on its own verified network keeps running and its address is re-read. Every other one
is removed before any is started again, the former shared bridge is taken apart (containers the
ledger does not reach are disconnected, not deleted), and then they start on private networks.
A container running another image is replaced; one whose digest left the allowlist is removed.
A row that cannot be verified or restored is `quarantined`: not served, retried next time. One
short `docker version` probe runs first, so a hung engine costs seconds, not a timeout per
tenant. The hearth always boots. A stub hearth never calls docker at all; its image rows are
quarantined.

Deploy, stop and reconcile run in worker threads under one lock (a waiter gives up with 409
after 15 s rather than pinning a thread). A redeploy takes the row off the edge before it stops
the old tenant, the hub announce runs after the new row is written and outside the lock, and
the edge re-reads the row just before it connects: a stop that lands mid-request answers 503
instead of reaching whoever now holds the old address. A stopped tenant's /29 stays off the
allocator for ten minutes for the same reason.

The bridge gateway is the engine host, and Docker filters FORWARD, not INPUT, so a tenant can
reach host processes listening on wildcard addresses. `scripts/tenant-host-firewall.sh apply`
drops new connections from `hst+` to the host (replies to the edge still pass). Hestia cannot
install it itself: it may not be root.

Tenants remain on the same Docker engine the operator already runs. Do not use
`--network host`. Do not mount the host socket into the compose service.
`HESTIA_ALLOW_HOST_DOCKER=1` is the host-process opt-in: compromise of Hestia ⇒ engine;
compromise of a tenant must not.

`HESTIA_EGRESS_ALLOWLIST` is not enforced. A future sidecar may use it; argv does not punch holes.

`HESTIA_DOCKER_RUNTIME=runsc` (or auto-detect when `runsc` exists) adds `--runtime runsc` (gVisor). Required-and-missing **fails closed**. Silent fallback to runc or stub is refused. gVisor is a **host-process** docker runtime. Nested DinD and `docker.sock` in the compose box are not shipped. Firecracker is not a shipped adapter.

## Ledger and replicas

SQLite under `HESTIA_DATA_DIR` is the default (fast, no daemon). Production sets `HESTIA_DATABASE_URL=postgresql://…` and applies revision `001_tenants` via `hestia.migrations`. Fleet compose uses a dedicated `hestia-postgres` sidecar (`docker-compose.postgres.yml`), not Hub tables.

`HESTIA_REPLICAS>1` is refused. A shared ledger is not a farm. Tenants still run on this host. There is no sticky / single-runtime-owner.

## Known limits

- Stub runtime does not enforce cgroups. AST admission is not a sandbox. Production untrusted handlers are digest-pinned docker (optional gVisor) on the operator host.
- Each Docker tenant has no public egress and no route to another tenant. It can reach host services on wildcard addresses until `scripts/tenant-host-firewall.sh apply` has run on the engine host.
- The edge connects to the tenant's bridge IP, so the docker runtime needs the control plane on the engine host itself. A remote engine (`tcp://…`) whose bridges this host cannot route to fails at start, loudly, instead of listing a dead tenant.
- A live `docker.sock` inside the compose box would be a host shell. We do not ship that. Nested privileged DinD is not the default and is not in compose.
- Docker is digest-only: Hestia does not inject `capability.json` into the image.
- Postgres shares the ledger, not the invoke processes. Replicas stay refused.
- A signed receipt proves which key produced JSON for an input digest. It does not prove the handler is correct or safe.
- Announce does not create a Hub SKU by itself; Hub policy still applies.
- This is not legal, privacy, compliance, or financial advice.

## Primary references

- [AIMarket Protocol v2](https://github.com/alexar76/aimarket-protocol)
- [create-aimarket-agent](https://github.com/alexar76/create-aimarket-agent)
- [THEMIS](https://github.com/alexar76/themis)
- [OWASP Top 10 for Agentic Applications](https://genai.owasp.org/2025/12/09/owasp-top-10-for-agentic-applications-the-benchmark-for-agentic-security-in-the-age-of-autonomous-ai/)
