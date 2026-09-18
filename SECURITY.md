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
  does not own. Image rows are left alone: a container does outlive the control plane.
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
- **Payment is released on a platform failure.** A priced call claims its transaction before
  the tenant runs (anti-replay); if the tenant is then unreachable, returns a **5xx other than
  504**, or produces an **oversized response** (the buyer got nothing), the claim is released so
  the same transaction can be retried. A call that runs to its deadline (504) or is rejected by
  the handler as bad input (4xx) is billed — the caller supplied the input.
- **A payment is bound to the call it pays for.** A raw ERC-20 transfer carries nothing naming
  the call it settles, so a transfer to a payout address that also receives funds from elsewhere
  (a shared treasury does) could be presented as payment for a call it was never meant for.
  Every `402` now mints an invoice with a random nonce (`tenant_invoices`, revision `004`), and
  with `HESTIA_PAYMENT_REQUIRE_BINDING=1` (**the default**) settlement requires the transaction to
  carry an EIP-3009 `AuthorizationUsed` log for exactly that nonce. The buyer signs the nonce into
  a `transferWithAuthorization`; the **token contract** verifies that signature on chain and emits
  the log, so the hearth needs no key, no gas and no signature-recovery library — it only reads a
  log. A transfer made for anything else can never satisfy it. The nonce is single-use (the
  database decides the winner of a race), scoped to one tenant, and expires
  (`HESTIA_PAYMENT_INVOICE_TTL_S`, default 900s). The 402 publishes the asset's EIP-712 domain in
  `accepts[0].extra` so buyer tooling never guesses it. Turning binding **off** falls back to
  "any transfer of at least the price to the payout address", which is only safe when that address
  is dedicated to this hearth.
- **A payment also carries a freshness window.** `HESTIA_PAYMENT_MAX_AGE_S` (default 3600s)
  refuses a transfer whose block is older than the window; the age is read from the same node that
  served the receipt, and a payment whose age cannot be established is refused (fail-closed).
  Defence in depth behind the binding above, and the main guard when binding is off.
- **Releasing gives back both.** A platform-side failure releases the spent transaction *and* the
  invoice nonce — releasing only the transaction would leave the buyer holding a burned nonce and
  no way to retry a payment they already made.

## Runtime ladder (honest)

1. **stub** — laptops, CI, compose satellite, and the locked reference hearth
   (hestia.modelmarket.dev runs this). Not a sandbox: AST admission is a handler allowlist, and
   the code runs as an unprivileged same-host subprocess. Bounded by a per-call deadline, an
   address-space cap (on by default), a container mem/pids limit and a response cap — see above.
   `HESTIA_REQUIRE_SANDBOX=1` (and `HESTIA_PROFILE=prod`) refuse this path for template handlers.
2. **docker digest-only** — host process. cap-drop ALL, read-only, uid 65532, pids/memory/cpu,
   no-new-privileges, the daemon's default seccomp profile (the flag is omitted, not set to a
   literal `default`), internal network or `none`. **gVisor `runsc`**
   when the binary exists or `HESTIA_DOCKER_RUNTIME=runsc`. Required-and-missing fails
   closed. No silent fallback.
3. **Firecracker** — not shipped. A half-adapter is worse than honest docker+gVisor.

The compose box stays stub-only so it cannot drive the host. Nested DinD and a live
`docker.sock` in that box are not shipped. The live factory reference hearth is
stub + Postgres: a production **ledger**, not a production **sandbox**.
