# Architecture

HESTIA is a **single-replica hearth control plane** plus an **invoke edge**. It hosts capability providers on the operator’s machines. It does not list them on Hub. Postgres may share the ledger; tenant processes still live on one runtime host.

## Components

| Path | Responsibility |
|---|---|
| `hestia/app.py` | FastAPI: `/` landing, health, well-known, manifest, hearth roster, admin overview, deploy/stop/announce, SKU invoke, `/t/{slug}` proxy, `/ui/`, `/ui/admin/` |
| `hestia/config.py` | Env settings. `HESTIA_REPLICAS>1` refuses to start. `HESTIA_REQUIRE_SANDBOX=1` refuses stub. `HESTIA_TENANT_NETWORK` host/bridge refused. Host `docker.sock` refused unless `HESTIA_ALLOW_HOST_DOCKER=1` |
| `hestia/docker_host.py` | Operator CLI gate + gVisor `runsc` detect / fail-closed |
| `hestia/db.py` / `hestia/migrations.py` / `hestia/store.py` | SQLite default, Postgres when `HESTIA_DATABASE_URL` is set. Revision `001_tenants` |
| `hestia/scan.py` | AST **admission** for template handlers (not a sandbox) |
| `hestia/policy.py` | Isolation numbers, `resolve_tenant_network`, `assert_safe_docker_argv` |
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
         GET  /v1/admin/overview      Bearer: running/stopped counts, no listen_url
         POST /v1/tenants             Bearer deploy token
         POST /ai-market/v2/invoke    SKUs (deploy/stop require token)
         *    /t/{slug}/…             edge proxy → tenant listen_url
              stub:   127.0.0.1:<ephemeral>
              docker: http://hestia-{slug}:8080
                      network = hestia-tenants if Internal, else none
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

`ensure_internal_tenant_network` uses `HESTIA_TENANT_NETWORK` (default `hestia-tenants`) **only**
when `docker network inspect` says Internal (creating `--internal` if missing);
otherwise `none`. Tenants are sibling containers on the same engine the operator already
runs. Do not use `--network host`. Do not mount the host socket into the compose service.
`HESTIA_ALLOW_HOST_DOCKER=1` is the host-process opt-in: compromise of Hestia ⇒ engine;
compromise of a tenant must not.

`HESTIA_EGRESS_ALLOWLIST` is not enforced. A future sidecar may use it; argv does not punch holes.

`HESTIA_DOCKER_RUNTIME=runsc` (or auto-detect when `runsc` exists) adds `--runtime runsc` (gVisor). Required-and-missing **fails closed**. Silent fallback to runc or stub is refused. gVisor is a **host-process** docker runtime. Nested DinD and `docker.sock` in the compose box are not shipped. Firecracker is not a shipped adapter.

## Ledger and replicas

SQLite under `HESTIA_DATA_DIR` is the default (fast, no daemon). Production sets `HESTIA_DATABASE_URL=postgresql://…` and applies revision `001_tenants` via `hestia.migrations`. Fleet compose uses a dedicated `hestia-postgres` sidecar (`docker-compose.postgres.yml`), not Hub tables.

`HESTIA_REPLICAS>1` is refused. A shared ledger is not a farm. Tenants still run on this host. There is no sticky / single-runtime-owner.

## Known limits

- Stub runtime does not enforce cgroups. AST admission is not a sandbox. Production untrusted handlers are digest-pinned docker (optional gVisor) on the operator host.
- Docker `--network none` (the fallback) means the tenant cannot call Hub **and** the edge cannot reach the container by name.
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
