# Verified compute

HESTIA sells two host capabilities that run an operator-published pure function on a buyer's
input and sign what happened:

| Capability | Replicas | Method in the receipt | Default price |
|---|---|---|---|
| `hestia.compute.run@v1` | 1 | `hestia.run@v1` | $0.001 |
| `hestia.compute.verify@v1` | 2, in parallel, perturbed | `hestia.replicate@v1` | $0.0025 |

**Buyers bring data, never code.** The function is the `handler.py` of a running template
tenant that the operator has published for compute, referenced by `slug` and pinned by
`function_sha256` (sha256 of its exact bytes). Accepting Python from a buyer would be remote
code execution by design: the AST scan is an allow-list, not a sandbox
([SECURITY.md](../SECURITY.md)). Buyer-supplied code is a later phase, and only as WebAssembly
(see [Not in this version](#not-in-this-version)).

Off by default (`HESTIA_COMPUTE_ENABLED=0`). Code: `hestia/compute.py` (orchestration, receipts),
`hestia/compute_worker.py` (one replica), `hestia/scan.py` (deterministic admission).

## What is verified, and what is not

Read this table before quoting a compute receipt to anyone.

| Claim | Verified? | By what |
|---|---|---|
| These exact function bytes ran | **Yes** | `function_sha256` is taken over the bytes handed to the replicas, read once, and the buyer can pin it (`409` on mismatch) |
| On these exact input bytes | **Yes** | `input_sha256` over the canonical input; the buyer recomputes it from what they sent |
| And produced these output bytes | **Yes** | `output_sha256` recomputed by the hearth from the parsed output, never taken from the replica |
| Two fresh, perturbed, key-less processes on this host agreed byte for byte (`verify` only) | **Yes** | Different `PYTHONHASHSEED` each, staggered start, empty directory each; signed only on agreement |
| Within the stated limits | **Yes** | CPU time and peak RSS read by the parent from the kernel (`os.wait4`); the function never reports its own usage |
| The operator is honest | **No** | One operator runs both replicas and holds the signing key. The receipt says `same_operator: true` |
| The function is correct | **No** | Two replicas of a wrong function agree on the wrong answer |
| Another machine would produce the same bytes | **No** | libm, CPython build and version can differ; the receipt names `python_version` and `platform` so a disagreement can be read against them |
| Isolation at cgroup level | **No** | A replica is a plain subprocess under rlimits plus the containment audit hook, not a container or a VM |
| Nobody else saw the input | **No** | The operator can read every input and output |

`same_operator: true` is not modesty. Replicating on one host under one key is a **consistency
check**: it catches nondeterminism (hash order, address-dependent output, a clock the scanner
missed) and transient faults (a replica that crashed or ran out of memory). It cannot catch an
operator who patches both replicas or signs whatever they like.

The rung that can is **cross-hearth**: the same `function_sha256` on a second hearth with its
own key and operator, and the two `output_sha256` compared by the buyer
(`hestia-agents cross-verify`, below). That is the one to rely on.

## The ladder

1. **`run`** — metered execution. The receipt binds function, input and output digests to
   measured CPU and memory. Nothing about determinism.
2. **`verify`** — two replicas on this host. Signed only when they agree. Same operator.
3. **cross-hearth** — the buyer runs the same pinned function on two independently keyed
   hearths and compares. Different operators; the real verification rung. Optionally an AWR
   `VerificationVerdict` records it.
4. **buyer code as WASM** — not shipped (see below).

## Calling it

Through a hub (the usual door) or directly at `POST /ai-market/v2/invoke`:

```json
{
  "capability_id": "hestia.compute.verify@v1",
  "input": {
    "slug": "json-canonical",
    "function_sha256": "optional: refuse unless the function is exactly these bytes",
    "input": { "document": { "b": 1, "a": [1, 2] } }
  }
}
```

`GET /v1/compute` (free, signed with the hearth key) lists the published functions with their
`function_sha256` and whether each passes the deterministic admission, plus prices and limits.

A successful call returns the usual host envelope (`ok`, `result`, `receipt`, `signature`,
`provider_pubkey`), where `result` is `{"output": …, "compute_receipt": …}`:

| Receipt field | Meaning |
|---|---|
| `kind` / `id` | `hestia.compute.receipt@v1`, a fresh `urn:uuid` (what an AWR verdict references) |
| `capability_id`, `method`, `replicas` | Which SKU; `hestia.run@v1` (1) or `hestia.replicate@v1` (2) |
| `hearth`, `slug` | Where, and which published function |
| `function_sha256`, `input_sha256`, `output_sha256` | sha256 over `canonicalization` (`json-sorted-compact-utf8@v1`: sorted keys, no whitespace, UTF-8, no NaN) |
| `hash_seeds`, `cpu_ms[]`, `max_rss_kb[]`, `wall_ms[]` | Per replica, kernel-measured |
| `limits` | The published caps; `memory_capped_by_kernel` is false where RLIMIT_AS is not enforced (macOS) and memory held only as a measurement |
| `runtime`, `python_version`, `python_implementation`, `platform`, `hestia_version` | What ran it (`runtime: subprocess`) |
| `same_operator` | Always `true` on a single hearth |
| `issued_at`, `signature` | `signature` is `sign_object` over the whole receipt (Ed25519, plus ML-DSA-65 with `HESTIA_PQC=1`), under the key in `/.well-known/ai-market.json` |

The receipt carries integers and strings only, so any JSON canonicalisation (the hearth's own,
RFC 8785 for AWR) agrees on its bytes.

When the work ran but the answer is "no", the reply is **HTTP 200 with `ok: false`**, a
`refuse_reason`, the per-replica measurements, `signed: false`, and **no receipt and no
signature**:

| `refuse_reason` | When |
|---|---|
| `replicas_disagree` | Different output bytes, or one replica answered and the other raised |
| `cpu_limit_exceeded` / `wall_time_exceeded` / `memory_limit_exceeded` / `output_too_large` | The input consumed its budget on at least one replica |
| `function_error` | Every replica raised (the function's own error, truncated) |
| `output_not_json` / `containment_violation` | The output was not JSON, or the function tried something containment refuses |

A hub reads `ok: false` as a refusal and does not bill its credits rail for it.

Refused **before** anything runs or is paid: `400` (malformed request, input not canonical
JSON), `404` (no such published, running function), `409` (pinned `function_sha256` differs),
`422` (the function fails the deterministic admission).

## What a replica is

Each replica is a fresh `python -S -P -B` running `hestia.compute_worker`, one call, then exit:

- **an empty 0700 directory** of its own, removed afterwards, and never a tenant directory (a
  tenant can read its own `tenant.key`; a replica has no key anywhere it may open);
- **an environment built from nothing**: no `HESTIA_*`, no `AIMARKET_*`, no operator
  `PYTHONPATH`; `TZ=UTC`, `LC_ALL=C.UTF-8`, and a per-replica random `PYTHONHASHSEED`;
- **no site-packages and no `.pth` start-up code** (`-S`); the worker is imported from the same
  tree as the control plane, appended after the standard library so nothing can shadow it;
- **rlimits set before exec**: `RLIMIT_CPU` (the budget rounded up to whole seconds; hard limit
  one second later), `RLIMIT_AS`, `RLIMIT_NOFILE=32`, `RLIMIT_FSIZE=0`, `RLIMIT_CORE=0`;
- **a wall-clock deadline** the parent enforces by killing the replica;
- **the containment audit hook** (`hestia/handler_guard.py`) armed before the function loads
  and never disarmed, so a finalizer the function leaves behind runs contained too;
- **measured from outside**: the parent reaps it with `os.wait4` and takes `cpu_ms` and peak
  RSS from the kernel. The millisecond CPU budget and the memory budget are then checked
  against those numbers, because `RLIMIT_CPU` counts whole seconds and macOS refuses
  `RLIMIT_AS`.

Replica processes are capped host-wide (`HESTIA_COMPUTE_MAX_CONCURRENT`, default 2 = one verify
job); a job that cannot get its slots within one wall budget is refused with `503` and its
payment released.

## Deterministic admission

A compute function passes every rule a tenant handler passes, plus
(`admit_handler(source, deterministic=True)`):

- no clock: `now`, `utcnow`, `today`, `fromtimestamp`, `utcfromtimestamp` (as attributes or
  imported names); `time` is not importable;
- no `id()` (a memory address) and no `hash()` (seeded per process);
- no `random`, `secrets`, `uuid`, `os`, `sys`, `subprocess`, `socket`, `threading`. `random` is
  refused seeded or not — allowing the module would open its unseeded entry points too; derive
  pseudo-randomness from `hashlib` over the input.

Date arithmetic (`datetime.date`, `timedelta`) stays allowed. What the scan cannot see — the
iteration order of a `set` of strings, the `repr` of an object — is what the second replica is
for.

**Known limit of the scan.** `str(obj)`, `repr(obj)`, `f"{obj}"` and `"%r" % obj` are admitted,
and for an object with the default `repr` they print a memory address (`<Foo object at 0x…>`) —
the same leak `id()` is refused for. The scan cannot tell that object from a number or a string
without running the function. So `hestia.compute.run@v1` (one replica) can sign an answer that
differs from run to run; `hestia.compute.verify@v1` catches it in practice — its two replicas
are separate processes under address-space randomization, so they print different addresses and
the call answers `replicas_disagree`. If a function's output must be reproducible, buy the
verify tier.

## Payment

Compute is never free when it is on. A call must carry one of three sets of headers:

| Header | Meaning | Who sends it |
|---|---|---|
| `X-API-Key` = one of `HESTIA_COMPUTE_HUB_KEYS` | A hub billed the buyer on its credits rail (mandates included) and vouches for the call | a hub with this hearth in `AIMARKET_PEER_API_KEYS` |
| `X-API-Key` + `X-Payment: <tx hash>` + `X-Payment-Nonce: <the hub's invoice nonce>` | The hub settled the buyer's market-rail payment against its own invoice and forwards it. The authorization that nonce names must itself have moved at least the price to `HESTIA_COMPUTE_PAYOUT_ADDRESS`, confirmed and fresh (`HESTIA_PAYMENT_MAX_AGE_S`); it is claimed here once. The key, not a secret, says who is redeeming it: this hearth trusts the hub it gave that key to | the same hub, and only for a payment it settled on that request |
| `X-Payment: <tx hash>` + `X-Payment-Secret: 0x<64 hex>`, no key | An EIP-3009 `transferWithAuthorization` of at least the price to `HESTIA_COMPUTE_PAYOUT_ADDRESS` whose nonce is `sha256(secret)`, confirmed, fresh, claimed here once | a direct buyer, who picked the secret |

`HESTIA_PAYMENTS_ENABLED` is **not** consulted: it is the tenant till switch, off on the
production rail, and leaving it off must not make compute free.

**No second till.** On the production rail (configuration A in
[`hestia-hub-market-rail.md`](https://github.com/alexar76/aicom/blob/main/docs/hestia-hub-market-rail.md))
the hub is the only till.
The manifest publishes the compute capabilities with `payout_address` =
`HESTIA_COMPUTE_PAYOUT_ADDRESS`, so the hub's `402` names that wallet, the hub mints its nonce,
and it forwards the buyer's transaction with its key, naming its invoice's nonce. This hearth
still **never mints a nonce for compute**: a second one would be the two-till failure, since one
authorization carries one nonce. An unpaid call gets a `402` quoting the terms with
`binding: "secret"`, `nonce_rule: "sha256(secret)"` and no nonce. A direct buyer picks the nonce
instead: they choose 32 random bytes as the secret and keep them to themselves, pay with
`transferWithAuthorization(..., nonce=sha256(secret))` to the compute address, and retry with
`X-Payment: <tx hash>` and `X-Payment-Secret: 0x<secret hex>`. `X-Payment-Nonce` may be left out,
since the secret names it; one that is sent must be the nonce the secret opens. A secret with
fewer than 16 distinct bytes is refused as guessable before the chain is read (32 random bytes
hold about 30): a pattern like that is one a watcher can precompute, and the refusal tells the
buyer to pay again with random bytes. The hearth checks the rest from the chain alone: the
transaction's `AuthorizationUsed` log must carry `sha256` of the secret presented, so it keeps
no invoice and shares no state with the hub. The address still says
what was bought — a wallet that receives compute payments and nothing else. On a hearth with
hub keys and no payout address, an unpaid call gets `401`: there is no direct door.

**When a payment is kept, and when it is given back.** Every refusal decidable without running
anything (`400`/`404`/`409`/`422`, and an unpaid or already-spent payment) happens before the
payment is claimed.

| After the claim | Payment |
|---|---|
| Signed answer | kept |
| `ok: false` — limits, the function's error, `replicas_disagree` | **kept**: the work ran and the answer is honest. Seller-direct has no refunds, and a released transaction would buy the same refused work again for free |
| No capacity (`503`), a replica that could not start / crashed / wrote nothing (`502`), an exception in the hearth | **released**: the same payment can be presented again |

Hub-side, the rest is the hub's rules: it bills per call from the catalogue price (never per
CPU-second — which is why compute is sold as fixed tiers with published limits), it does not
bill its credits rail for an `ok: false`, and a credits-rail sale of a federated capability
leaves the money with the hub operator rather than paying the seller wallet on chain.

**A key together with a payment.** The hub sends a payment next to its peer key only when it
settled that payment itself, on the same request, against its own invoice (which took the
buyer's secret); a sandbox trial, a zero price or rails that are off
send the key alone. The hub then names the authorization it verified with `X-Payment-Nonce` and
keeps the buyer's secret, which opens the hub's invoice and nothing here. The transfer is what
pays: it is verified on chain and claimed once, as a direct buyer's is (the key stands in for
the secret), and a payment already spent is refused even with a valid key. Accepted on the key
alone, it would stay unclaimed on chain and buy a second call for whoever replayed it. A
payment that names no authorization is refused on the key too — a plain transfer included:
nothing in it says who paid or for what, and the hub never settles one. A secret sent on the
key path must open its nonce. (A hearth with
no payout address has no terms to check a payment against and serves the call on the key
alone.)

**Which transfer pays.** The nonce — the buyer's or the hub's — names the authorization, and
only the transfer that authorization moved counts: the token's very next log after its `AuthorizationUsed`, from the
authorizer to the compute address, of at least the price. Other transfers to the address in the
same transaction are not added in. Summing them let anyone holding a buyer's signed
authorization before it was mined bundle it with a one-unit authorization of their own and
redeem the buyer's money under their own nonce. EIP-3009 nonces are per signer, so anyone can
sign an authorization over the same nonce and place it first: every authorization carrying the
nonce is tried, and the best-paying one settles.

**One payment, one claim.** A payment is its authorization: the claim is the transaction hash
together with that nonce, so two authorizations in one transaction are two payments and buy two
calls, and whoever redeems one cannot deny the other. No claim is the bare transaction; one
claimed whole back when plain transfers were accepted stays spent. A release (the table above)
un-spends that one claim only. Compute and tenant payments share the ledger, so one payment
cannot pay for both.

**Why the secret.** Once a payment is mined, its transaction hash and its EIP-3009 nonce (in the
`AuthorizationUsed` log) are public. When a key-less `X-Payment` was enough, anyone watching the
chain could present a compute payment before the buyer did, or before the hub's forward arrived,
get that one call, and leave the buyer who paid refused as already spent. The chain shows only
the commitment `sha256(secret)`; the secret leaves the buyer only in their own retry, so a
watcher holds the hash and the nonce and still cannot redeem them. The hub's own forward needs
no secret: it is authenticated by its key, the same trust the credits rail already rests on, and
the key is only as good as what the hub sends next to it. That is why the hub now forwards a
payment with its key only after settling it on the same request: it used to forward whatever
`X-Payment` a caller handed it (on a sandbox trial, at a zero price, with rails off), and the key
then let a caller with nobody's secret redeem, or burn, a payment someone else had made.
A plain transfer, with no nonce, is refused at both doors however fresh and full it is, and so
are a secret that does not open the nonce on chain and a guessable one. Production runs hub-keys only (no payout
address), so neither the key-less door nor a forwarded payment is live there.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `HESTIA_COMPUTE_ENABLED` | `0` | Serve the two compute capabilities |
| `HESTIA_COMPUTE_FUNCTIONS` | empty | Comma-separated slugs of running template tenants published as functions. Explicit, because compute sells the same function at the compute price: publishing a tenant priced higher for its owner undercuts that owner |
| `HESTIA_COMPUTE_PAYOUT_ADDRESS` | empty | Dedicated wallet for compute payments. Requires `HESTIA_PAYMENT_RPC_URL`. Send nothing else there |
| `HESTIA_COMPUTE_HUB_KEYS` | empty | Comma-separated keys (24+ characters) a hub presents as `X-API-Key` |
| `HESTIA_COMPUTE_CPU_MS` | `2000` | CPU budget per replica |
| `HESTIA_COMPUTE_MEMORY_MB` | `256` | Address-space cap and peak-RSS budget per replica |
| `HESTIA_COMPUTE_WALL_S` | `5` | Wall-clock budget per replica (max 15: two replicas plus a chain lookup must fit a hub's 30 s peer timeout) |
| `HESTIA_COMPUTE_MAX_OUTPUT_BYTES` | `1048576` | Output ceiling |
| `HESTIA_COMPUTE_MAX_CONCURRENT` | `2` | Replica processes at once, host-wide (at least 2). Size it to the box: the compose box has 1 GiB |
| `HESTIA_COMPUTE_RUN_PRICE_USD` / `HESTIA_COMPUTE_VERIFY_PRICE_USD` | `0.001` / `0.0025` | Fixed per-call prices. Raise one only after the hub has re-crawled, or buyers pay the old price and are refused |
| `HESTIA_COMPUTE_ALLOW_UNSANDBOXED` | `0` | See below |

Payment verification reuses `HESTIA_PAYMENT_RPC_URL`, `_CHAIN`, `_TOKEN`, `_TOKEN_CONTRACT`,
`_DECIMALS`, `_MIN_CONFIRMATIONS` and `_MAX_AGE_S`.

The hearth **refuses to start** with compute on when it could never be paid (no payout address
and no hub key), when a payout address has no RPC, when a key is short, or when a limit is out of
range — the same fail-loud rule as `HESTIA_PAYMENTS_ENABLED=1` without an RPC.

**Sandboxed hearths.** Under `HESTIA_REQUIRE_SANDBOX=1` or `HESTIA_PROFILE=prod` the hearth
refuses to start with compute on, because a replica is a subprocess under rlimits, not a
sandbox. `HESTIA_COMPUTE_ALLOW_UNSANDBOXED=1` accepts that explicitly. Note that such a hearth
also refuses template deploys, so in this version it has no functions to publish; the follow-up
that makes compute legitimate there is a docker executor (a one-shot `docker run --rm` of a
pinned worker image with the locked argv, metered from its cgroup).

## Publishing a function

1. Deploy it as a template tenant (`POST /v1/tenants`), as for any handler.
2. Check `GET /v1/compute` lists it `admissible: true` with the `function_sha256` you expect.
3. Add its slug to `HESTIA_COMPUTE_FUNCTIONS` and restart.

Stopping the tenant withdraws the function. A redeploy under the same slug changes its
`function_sha256`, which is exactly what a buyer's pin catches.

## Cross-hearth verification

`hestia-agents` ships the buyer client (`pip install 'aimarket-hestia-agents[compute]'`):

```bash
export HESTIA_COMPUTE_API_KEY=…            # if the hearths sell to you by key
hestia-agents compute json-canonical --hearth https://hearth-a.example \
    --input '{"document": {"b": 1}}' --replicated
hestia-agents cross-verify json-canonical \
    --hearth https://hearth-a.example --hearth https://hearth-b.example \
    --input @doc.json --verdict-out verdict.json --awr-key me.jwk
```

`cross-verify` refuses before paying either hearth when the check could not mean anything: both
publish the same key, or they list different bytes under that slug. It then checks each receipt
against the key that hearth publishes in its signed `.well-known`, recomputes the input and
output digests, and compares `output_sha256`. A difference in `python_version` or `platform` is
printed, since it may be the platform and not the operator.

With `--verdict-out` (and the `awr` package installed) it writes an AWR/2
`VerificationVerdict` signed by the buyer's key: `verifiedWork` is the first hearth's receipt by
RFC 8785 digest, the second receipt is evidence by digest, and the verdict is `pass` or `fail`.
A compute receipt is not an AWR `WorkReceipt`, so AWR's L1/L2 profiles are not evaluated over
it; the verdict is a portable, independently signed judgement naming exactly which receipt it
judged.

## Not in this version

- **Buyer-supplied code.** Only as WebAssembly under wasmtime with fuel metering and no WASI
  imports. Fuel counts are deterministic and themselves comparable across replicas. It needs a
  new dependency and is a separate piece of work.
- **A docker executor** for sandboxed hearths (above).
- **Sampled re-execution** of past receipts on another hearth, which would need inputs kept with
  consent.
- **Per-author payment.** One compute wallet per hearth; a hub listing has one `payTo` per
  capability, so paying each function's author would need one capability per function.
