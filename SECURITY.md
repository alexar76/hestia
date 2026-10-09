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
  closed. `str.format` / `str.format_map` and `string.Formatter` (`get_field` / `vformat`)
  are refused too: they walk attributes through a template string the AST never sees, the
  canonical way past a dunder blocklist. Handlers format with f-strings (whose attribute
  access **is** scanned), `%`, or `str.join`. **String (forward-reference) annotations are
  refused**, and `typing` is off the allowlist: `typing.get_type_hints()` `eval()`s a string
  annotation, so an admitted `def f(x: "arbitrary code")` would have run that code — source the
  AST never sees inside the string. `from __future__ import annotations` is refused for the
  same reason (it stringifies every annotation). **Interpreter-internal introspection attributes
  are refused**: `gi_frame` / `cr_frame` / `ag_frame` / `tb_frame` (and the whole
  `gi_` / `cr_` / `ag_` / `tb_` prefixes), plus `f_globals` / `f_builtins` / `f_locals` /
  `f_back` / `f_code` — none are dunders, so a dunder-only blocklist let
  `(x for x in ()).gi_frame.f_builtins["__import__"]("os")` reach the builtins dict (a dict
  subscript is data the AST never inspects). A forbidden **name reached as an attribute**
  (`m.eval`, `m.open`) is now refused with the same force as the bare name, single-underscore
  attributes are refused, dunder **subscript keys** (`x["__globals__"]`) are refused, and the
  module names an allow-listed import re-exports (`re.enum`, `statistics.sys`, `json.codecs`,
  `collections._sys`) are refused as attributes — that module-graph walk reached `os`/`sys`/
  `builtins` with no dunder at all. **A static allow-list cannot be complete** over untrusted
  Python, so it is not the only layer: **`hestia.handler_guard` arms a CPython audit hook around
  the handler** (both at load and on every call) that refuses opening any path outside the
  interpreter's library and the tenant's own directory, spawning a process, opening a socket,
  loading native code, or mutating the filesystem — so a gadget that slips the scanner still
  cannot read `provider.key`, steal a sibling `tenant.key`, shell out, or exfiltrate. **AST
  admission is not a sandbox.** Admitted `handle(payload)` runs (compiled from the admitted
  source, contained by the guard) in the tenant subprocess. The HTTP server and signing stay in
  `hestia.tenant_stub`.
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
  no `--cap-add` / `--device` / `--mount`, no host pid/ipc, one `--network` spelling only. Each
  tenant gets its own `hestia-tenants-{slug}` Internal bridge, labelled for that slug, IPv6 off,
  on a /29 Hestia allocates from `HESTIA_TENANT_SUBNET_POOL`. Hestia refuses a network it did
  not create or one another container is attached to (stopped ones included). There is no
  shared bridge or `none` fallback, and no route from one tenant to another. A subnet that
  still holds an address a live ledger row points at is never reused, so a stale row can never
  make the unauthenticated edge proxy to someone else's container. Docker is digest-only: the
  image is the provider. The control-plane `data_dir` is never bind-mounted into the tenant.
- **An Internal bridge does not protect the engine host.** Its gateway address is the host,
  and Docker filters FORWARD, not INPUT: a tenant can connect to every host process listening
  on a wildcard address. All tenant bridges are named `hst…`; run
  `sudo scripts/tenant-host-firewall.sh apply` on the engine host (and after every reboot) to
  drop new connections from them to the host. Replies to the edge still pass. For the same
  reason a plaintext TCP engine is refused whatever address Hestia dials (`tcp://127.0.0.1`
  says nothing about where the daemon listens; a scheme-less `host:port` is TCP too): use a
  unix socket or TLS with `dockerd --tlsverify`. `HESTIA_ALLOW_PLAINTEXT_DOCKER_TCP=1`
  overrides it; use it only once the host firewall is applied. An empty engine address pins
  `DOCKER_CONTEXT=default`, so a `docker context use` cannot redirect the CLI unseen.
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
- Production ledger is PostgreSQL (`HESTIA_DATABASE_URL`). SQLite is the dev/test default
  (empty URL → `HESTIA_DATA_DIR/hestia.db`). Schema is versioned
  (`python -m hestia.migrations up`, revision `001_tenants`). `CREATE TABLE IF NOT EXISTS
  tenants` is not the production contract.
- `HESTIA_REPLICAS>1` is refused. Postgres is a shared ledger, not a farm. Tenants still
  run on this host. There is no sticky / single-runtime-owner model.
- The invoke edge (`/t/{slug}`) forwards only `Content-Type` and `Accept`
  (`hestia.app.edge_forward_headers`). `Authorization` never reaches a tenant. The method,
  body and query string are forwarded verbatim.
- **The edge may only reach a tenant this process started.** A stub tenant is a child of the
  control-plane process on an ephemeral loopback port. Shared Postgres does not change that.
  A row left `running` by a previous process names a port the OS has since handed to somebody
  else — and `/t/{slug}` needs no token. At start-up `restore_stub_tenants` re-launches each
  running stub row from the data volume (`capability.json`, `handler.py`, `tenant.key`), which
  gives it a fresh loopback port and rewrites the row; one that cannot come back is marked
  stopped. Either way the invariant holds: a `running` row never points at a port this process
  does not own. Image containers outlive the control plane, so a docker hearth's startup
  (`reconcile_docker_tenants`) verifies each image tenant's private network and re-reads its
  address; every other one is removed, all of them before any restarts, and then started on a
  private network. The former shared bridge is taken apart: containers the ledger does not
  reach are disconnected from it. A tenant that cannot be verified or restored is
  `quarantined` (not served, retried at the next boot or `POST /v1/admin/reconcile`), so an
  unreachable engine never stops the hearth. A stub hearth never calls docker: its CLI would
  reach the host socket without the opt-in, so its image rows are quarantined, untouched.
- **The body ceiling counts bytes, not headers** (`hestia.limits.BodyLimitMiddleware`). A
  chunked request declares no `Content-Length`, and `/ai-market/v2/invoke` parses its body
  before it knows whether the capability needs auth — so a header-only check let an
  unauthenticated client stream an unbounded body into the hearth. An unparsable
  `Content-Length` is a `400`, not a traceback.
- Runtime handles are derived from the slug (`hestia.app.tenant_handle`), never held in
  process memory. A remembered map empties on restart, and `stop` then asked docker to remove
  the bare slug — removing nothing while the ledger recorded "stopped".
- `HESTIA_EGRESS_ALLOWLIST` is stored on `IsolationProfile` for a future sidecar. It is **not**
  applied to docker argv. Tenants do not get a hole to those hosts.

## Deployment checklist

1. Bind the control plane on loopback or a private network; put authenticated HTTPS ingress in front.
2. Set a high-entropy `HESTIA_DEPLOY_TOKEN`. Never commit it. Empty token = locked writes.
3. Production: set `HESTIA_DATABASE_URL=postgresql://…` (chmod 600 on the host `.env`) and
   keep `/data` for `provider.key` only. Never bake the seed into an image. SQLite under
   `/data/hestia.db` is not the long-term store.
4. Back up `provider.key` before publishing `provider_pubkey`.
5. Leave `HESTIA_AUTO_ANNOUNCE=0` unless you intend every deploy to knock on Hub.
6. Pin docker image digests; do not pass a tag. Rerun `uv run pytest -q` before promotion.
7. Keep `HESTIA_REPLICAS=1`. Do not scale the compose satellite. Tenants are still one-host.
8. Apply an external rate limit. Cap `HESTIA_MAX_TENANTS`.
9. Do not log request bodies: handlers and payloads may contain secrets.
10. Fleet compose: one Hestia box, stub tenants inside it, no sock. For sibling tenant
    containers, run the control plane **on the host** with `HESTIA_RUNTIME=docker` and
    `HESTIA_ALLOW_HOST_DOCKER=1`. Do not mount `/var/run/docker.sock` into the compose
    service — that would be a shell onto the host. Hestia creates a labelled, `--internal`
    bridge per tenant and fails closed if it cannot verify ownership and isolation. Apply
    `scripts/tenant-host-firewall.sh` on the engine host before the first tenant starts. On
    upgrade, image tenants still attached to the former shared bridge are all removed before
    any is restarted on its own network; check `/v1/admin/overview` for `quarantined` rows.

## Isolation that is out of scope for `stub`

`HESTIA_RUNTIME=stub` is a sealed **loopback subprocess** for laptops, CI, and the compose
satellite. It does **not** enforce cgroups, seccomp, or a VM. What it does enforce:

- child env is built by `minimal_tenant_env` (no `os.environ.copy()`): `HESTIA_TENANT_*` plus
  PATH/HOME/TMPDIR. `HESTIA_DEPLOY_TOKEN`, `AIMARKET_*`, and operator keys are omitted.
  `hestia.tenant_stub.scrub_operator_env` drops leftovers before `handle` is loaded.
- cwd is `data_dir/tenants/{slug}` (mode `0700`), not the shared data dir. The hearth
  `provider.key` stays in `data_dir`, never in the tenant directory.
- IsolationProfile CPU/memory/pids numbers are ignored by the stub (`del image_digest, profile`).
- **Stub is not a cgroup, but it is bounded.** A template handler runs in its own subprocess,
  not in the control plane, so a runaway call cannot corrupt the plane — but with no cgroup it
  could still burn a core or memory. Four bounds contain that:
  - a **per-call wall-clock deadline** (`HESTIA_TENANT_CALL_TIMEOUT_S`, default 10s) delivered
    by SIGALRM, which is why the tenant server is single-threaded — it aborts even a
    catastrophic-backtracking regex like `(a+)+$`, the case CPython cannot break between
    bytecodes;
  - an **address-space ceiling on by default** (`HESTIA_STUB_MEMORY_CAP_MB`, default 512;
    `RLIMIT_AS`, 0 disables);
  - the **container's own** `mem_limit` (1 GiB) and `pids_limit` (256) in compose, so all
    agents together cannot take the host;
  - the **edge caps the tenant response** (`HESTIA_MAX_TENANT_RESPONSE_BYTES`, default 4 MiB),
    so an amplifying handler cannot exhaust the control plane, and the tenant's unread stderr
    is drained so a burst of requests cannot wedge it.
  None of this replaces a cgroup: for enforced CPU/memory isolation run `HESTIA_RUNTIME=docker`.
- **Payment is released on a platform failure.** A priced call claims its payment before
  the tenant runs (anti-replay); if the tenant is then unreachable, returns a **5xx other than
  504**, or produces an **oversized response** (the buyer got nothing), the claim is released so
  the same payment can be presented again. A call that runs to its deadline (504) or is rejected by
  the handler as bad input (4xx) is billed — the caller supplied the input.
- **A payment is bound to the call it pays for.** A raw ERC-20 transfer carries nothing naming
  the call it settles, so a transfer to a payout address that also receives funds from elsewhere
  (a shared treasury does) could be presented as payment for a call it was never meant for.
  Every `402` now mints an invoice (`tenant_invoices`, revision `004`) whose nonce is
  `sha256` of a fresh 32-byte secret, and settlement requires the transaction to carry an
  EIP-3009 `AuthorizationUsed` log for exactly that nonce — always: there is no unbound payment.
  The buyer signs the nonce into a `transferWithAuthorization`; the **token contract**
  verifies that signature on chain and emits the log, so the hearth needs no key, no gas and no
  signature-recovery library — it only reads a log. A transfer made for anything else can never
  satisfy it. Only the transfer that authorization moved counts — the token's next log, from the
  authorizer to the payout address — so a buyer's authorization bundled into someone else's
  transaction pays nothing to anyone but that buyer. Nonces are per signer, so every authorization
  over the nonce is tried and the best-paying one settles: one signed first by someone else
  cannot shadow the buyer's. The nonce is single-use (the database decides
  the winner of a race), scoped to one tenant, and expires (`HESTIA_PAYMENT_INVOICE_TTL_S`, default
  900s). The 402 publishes the asset's EIP-712 domain in `accepts[0].extra` so buyer tooling never
  guesses it. `HESTIA_PAYMENT_REQUIRE_BINDING=0` used to fall back to "any transfer of at least
  the price to the payout address". That is gone: nothing in a plain transfer says who paid, so
  whoever presented it first took the call. The setting is ignored and logged as an error at
  startup.
- **A payment is redeemed only by whoever took the quote.** Once the transfer is mined, its hash
  and its nonce are public, so a redemption that needed only those let anyone watching the chain
  present them first and leave the buyer who paid refused as already spent. The bound `402`
  therefore hands its secret (`payment_secret`) to its caller in the JSON body only, never in a
  header, and settlement requires `X-Payment-Secret` to open the nonce (`X-Payment-Nonce` is then
  optional; one that is sent must be the nonce the secret opens).
- **One payment, one claim.** A payment is its authorization: the claim is the transaction hash
  together with the nonce, so two authorizations in one transaction are two payments and buy two
  calls, and whoever redeems one cannot deny the other. No claim is ever the bare transaction; a
  transaction claimed whole back when unbound payments existed stays spent and pays nothing more.
- **A payment also carries a freshness window.** `HESTIA_PAYMENT_MAX_AGE_S` (default 3600s)
  refuses a transfer whose block is older than the window; the age is read from the same node that
  served the receipt, and a payment whose age cannot be established is refused (fail-closed).
  Defence in depth behind the binding above.
- **Releasing gives back both.** A platform-side failure releases the spent claim *and* the
  invoice nonce — releasing only the claim would leave the buyer holding a burned nonce and no way
  to retry a payment they already made. It releases that one payment's claim, never another
  authorization's in the same transaction.

## Runtime ladder (honest)

1. **stub** — laptops, CI, compose satellite, and the locked reference hearth
   (hestia.modelmarket.dev runs this). Not a sandbox: AST admission is a handler allowlist, and
   the code runs as an unprivileged same-host subprocess. Bounded by a per-call deadline, an
   address-space cap (on by default), a container mem/pids limit and a response cap — see above.
   `HESTIA_REQUIRE_SANDBOX=1` (and `HESTIA_PROFILE=prod`) refuse this path for template handlers.
2. **docker digest-only** — host process. cap-drop ALL, read-only, uid 65532, pids/memory/cpu,
   no-new-privileges, the daemon's default seccomp profile (the flag is omitted, not set to a
   literal `default`), its own Internal /29 network (never shared, never `none`). **gVisor `runsc`**
   when the binary exists or `HESTIA_DOCKER_RUNTIME=runsc`. Required-and-missing fails
   closed. No silent fallback.
3. **wasm** — `HESTIA_RUNTIME=wasm`, the reference hearth since 2026-10-04. A template handler
   runs in a fresh WebAssembly instance per call: CPython 3.14 for WASI (fetched by SHA-256) under
   wasmtime 49 (pinned), in the separate `hestia-runner` container — `network_mode: none`, none of
   the hearth's environment, read-only rootfs, uid 65532, cap-drop ALL, no-new-privileges, its own
   memory/pids caps, one Unix socket in a volume shared with the hearth and nothing of `/data`.
   Inside the instance: the standard library read-only, no other path, no sockets (WASI preview 1),
   no processes or native code, two fixed environment variables; linear memory (256 MiB), wall
   clock (epoch interruption, 10 s) and output (4 MiB) capped. Keys never enter it: the hearth signs
   outside, with the tenant's own key. An escape would need a wasmtime bug, and would land in a
   container with no network and nothing to read. This is the rung strangers' code runs on
   (`HESTIA_OPEN_OWNER_CODE=1` is refused under any other runtime), and `HESTIA_REQUIRE_SANDBOX=1`
   accepts it.
   **Fair share.** A handler may spin until its deadline, and a key admits itself for free. Two
   bounds keep that from becoming a free way to take the host or the hearth. The runner has a CPU
   ceiling (`cpus`, `HESTIA_RUNNER_CPUS`, default 1.5) beside its memory and pids caps; the hearth
   box has one too (`HESTIA_CPUS`, default 2). And the agents of every self-admitted owner
   (`admitted_by: open`) share one **open lane**: together they hold at most
   `HESTIA_OPEN_LANE_SLOTS` (default 1) of the runner's `HESTIA_RUNNER_WORKERS` (default 2), one
   call per owner, and the hearth refuses to start unless the lane stays below the workers. So the
   operator's agents, and those of owners it admitted (`POST /v1/owners`, or `admitted_by:
   "operator"` to vouch for an open one), always find a slot. A call that finds the lane full
   waits `HESTIA_OPEN_LANE_WAIT_S` (2 s), then gets `503`; a claimed payment is released. A cap
   per owner alone would not do it: ten keys are ten owners.
4. **Firecracker** — not shipped. A half-adapter is worse than honest docker+gVisor or wasm.

The compose box itself cannot drive the host (no docker CLI, no `docker.sock`, no DinD); the runner
is a second unprivileged box beside it. The live reference hearth is wasm + Postgres.

**Compute replicas** (`hestia.compute.*`, off by default, [docs/COMPUTE.md](docs/COMPUTE.md))
sit on the first rung too: each is a one-shot `python -S -P -B` subprocess under `RLIMIT_CPU`,
`RLIMIT_AS`, `RLIMIT_NOFILE`, `RLIMIT_FSIZE=0` and `RLIMIT_CORE=0`, with a parent-enforced wall
clock, an empty 0700 directory, an environment built from nothing, no key anywhere it may open,
and the containment hook armed for its whole life. They run only operator-published template
handlers that pass a stricter, deterministic admission — **never buyer code**. Because this is
not a sandbox, `HESTIA_REQUIRE_SANDBOX=1` / `HESTIA_PROFILE=prod` refuse to start with compute
on unless `HESTIA_COMPUTE_ALLOW_UNSANDBOXED=1` says so explicitly. Two replicas under one
operator and one key are a consistency check (`same_operator: true` in every receipt), not
third-party verification. Compute is never served without a hub key or a payment verified on
chain to a dedicated wallet, and the hearth mints no nonce for it (no second till). Its key-less
door takes only an EIP-3009 payment whose nonce is `sha256` of a secret the buyer picked and
presents with `X-Payment-Secret`; a plain transfer, a secret that does not open the nonce and a
guessable one (fewer than 16 distinct bytes) are refused. A hub forwards a payment next to its
key only when it settled that payment on the same request, and names that authorization with
`X-Payment-Nonce`; a payment that names none — a plain transfer included — is refused on the
key too. At both doors only the named authorization's own transfer pays, and each authorization
is its own claim.
