# Security policy

## Supported version

Security fixes target the latest `main` branch until a stable release line is announced.

## Report privately

Do not open a public issue for a vulnerability. Use GitHub private vulnerability reporting in
`alexar76/hestia` and include the affected revision, reproduction, impact, and a
minimal non-sensitive proof.

Never include deploy tokens, provider seeds, Hub publish tokens, tenant handlers, or customer
payloads in a report.

Coordinated disclosure: acknowledge within **72 hours**, up to **90 days** before a public write-up.
Do not attach a working exploit against a live hearth to a public issue.

## Trust boundaries

- `/v1/tenants` deploy/stop/announce and `hestia.host.deploy@v1` / `hestia.host.stop@v1` require
  `HESTIA_DEPLOY_TOKEN`. An empty token refuses every write (there is no “open hearth” mode).
- Template `handler` source is untrusted. It is AST-admitted before start and again at load
  (`hestia.scan.admit_handler`, `hestia.tenant_stub._load_handler`). Unknown imports, every dunder
  name/attribute, `eval` / `exec` / `open` / `getattr` / `setattr` / `type` / `operator` fail
  closed. **AST admission is not a sandbox.** Admitted `handle(payload)` still runs via
  `runpy.run_path` in the tenant process. The HTTP server and signing stay in `hestia.tenant_stub`.
- The compose **Hestia box cannot drive the host. Tenants cannot either.** Default
  `docker compose up` is one unprivileged satellite (uid `65532`, `cap_drop ALL`,
  `read_only`, `no-new-privileges`). The image has **no docker CLI**. Compose does **not**
  mount `docker.sock`, does not set `privileged`, and does not use host pid/ipc/uts/net or a
  bind of `/`. Tenants in that box are loopback subprocesses. A shell inside Hestia cannot
  `docker run -v /:/host --privileged` or `nsenter` the host.
- Pinned images are untrusted artifacts. Hestia never builds a Dockerfile from a tenant. Only
  `sha256:` digests on `HESTIA_ALLOW_IMAGE_DIGESTS` may start. Argv is locked in
  `hestia.policy.docker_argv` / `assert_safe_docker_argv`: `--cap-drop ALL`, `--read-only`, uid
  `65532`, no `docker.sock`, no `--privileged`, no `-p` / `--publish`, no host/bridge network,
  no `--cap-add` / `--device` / `--mount`, no host pid/ipc. Network is `hestia-tenants`
  **only** when `docker network inspect` reports `Internal=true`
  (`ensure_internal_tenant_network`); otherwise `--network none`. Docker is digest-only: the image is the
  provider. The control-plane `data_dir` is never bind-mounted into the tenant.
- Docker runtime is a **host-process** path (`python -m hestia` on a machine that already has
  Docker), not the compose service. `unix:///var/run/docker.sock` is refused unless
  `HESTIA_ALLOW_HOST_DOCKER=1`. That flag means: compromise of this process ⇒ the engine.
  Compromise of a **tenant** must not ⇒ the engine (argv lock, no sock in the tenant). Nested
  privileged DinD is not the default and is not shipped in compose.
- `/v1/hearth` and `hestia.hearth.list@v1` are public **rosters of this host**, not Hub search.
  An empty list means this operator is hosting nothing.
- `HESTIA_HUB_URL` and `HESTIA_THEMIS_URL` are operator configuration. They are SSRF-checked
  (HTTPS to a public host, or HTTP loopback in local tests). User payloads cannot set them.
- THEMIS unreachable ⇒ deploy fails closed. `review` is refused unless `HESTIA_ADMIT_REVIEW=1`.
- Announce is observation: a knock on Hub federation. It is not admission, listing, or a trust grant.
- Ed25519 signatures bind a result to capability, product, and input digest. They prove which
  provider produced that JSON. They do not prove the handler is safe.
- The SQLite ledger is process-local. `HESTIA_REPLICAS>1` refuses to start.
- The invoke edge (`/t/{slug}`) forwards only `Content-Type` and `Accept`
  (`hestia.app.edge_forward_headers`). `Authorization` never reaches a tenant.
- `HESTIA_EGRESS_ALLOWLIST` is stored on `IsolationProfile` for a future sidecar. It is **not**
  applied to docker argv. Tenants do not get a hole to those hosts.

## Deployment checklist

1. Bind the control plane on loopback or a private network; put authenticated HTTPS ingress in front.
2. Set a high-entropy `HESTIA_DEPLOY_TOKEN`. Never commit it. Empty token = locked writes.
3. Mount one persistent `/data` volume for `hestia.db` and `provider.key`. Never bake the seed into an image.
4. Back up `provider.key` before publishing `provider_pubkey`.
5. Leave `HESTIA_AUTO_ANNOUNCE=0` unless you intend every deploy to knock on Hub.
6. Pin docker image digests; do not pass a tag. Rerun `uv run pytest -q` before promotion.
7. Keep `HESTIA_REPLICAS=1`.
8. Apply an external rate limit. Cap `HESTIA_MAX_TENANTS`.
9. Do not log request bodies: handlers and payloads may contain secrets.
10. Fleet compose: one Hestia box, stub tenants inside it, no sock. For sibling tenant
    containers, run the control plane **on the host** with `HESTIA_RUNTIME=docker` and
    `HESTIA_ALLOW_HOST_DOCKER=1`. Do not mount `/var/run/docker.sock` into the compose
    service — that would be a shell onto the host. Hestia creates `--internal
    hestia-tenants` on that engine when missing. If the net is not Internal, argv falls back to
    `--network none`.

## Isolation that is out of scope for `stub`

`HESTIA_RUNTIME=stub` is a sealed **loopback subprocess** for laptops, CI, and the compose
satellite. It does **not** enforce cgroups, seccomp, or a VM. What it does enforce:

- child env is built by `minimal_tenant_env` (no `os.environ.copy()`): `HESTIA_TENANT_*` plus
  PATH/HOME/TMPDIR. `HESTIA_DEPLOY_TOKEN`, `AIMARKET_*`, and operator keys are omitted.
  `hestia.tenant_stub.scrub_operator_env` drops leftovers before `handle` is loaded.
- cwd is `data_dir/tenants/{slug}` (mode `0700`), not the shared data dir. The hearth
  `provider.key` stays in `data_dir`, never in the tenant directory.
- IsolationProfile CPU/memory/pids numbers are ignored (`del image_digest, profile`).

Production multi-tenant cgroup isolation is `HESTIA_RUNTIME=docker` as a **host process**
plus the argv lock in `hestia/policy.py`. The compose box stays stub-only so it cannot
drive the host.
