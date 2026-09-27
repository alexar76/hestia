# User guide

🌐 **English** · [Русский](user-guide.ru.md) · [Español](user-guide.es.md) · [Français](user-guide.fr.md) · [中文](user-guide.zh.md)

**Operator workshop (90 min, not a course):** [workshop.md](workshop.md) · [RU](workshop.ru.md) · [ES](workshop.es.md) · [FR](workshop.fr.md) · [ZH](workshop.zh.md)

## What you are operating

A **hearth**: machines you control that run isolated AIMarket providers. The console at `/ui/` talks to **this** process. It is not Hub search.

Agents appear on the roster only after `POST /v1/tenants` (or `hestia.host.deploy@v1`) succeeds on this host.

## Local

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
uv sync --extra dev --project .
uv run --project . python -m hestia
```

Open http://127.0.0.1:9480/ui/ — paste the token, deploy the demo echo, hit `/t/demo-echo/health`.

## Invoke a tenant

```bash
curl -sS http://127.0.0.1:9480/t/demo-echo/invoke \
  -H 'content-type: application/json' \
  -d '{"hello":"hearth"}'
```

The JSON includes `result`, `provider_pubkey`, and an Ed25519 `signature`. Verify against the tenant’s key, not Hestia’s control-plane key, when the stub minted a per-tenant key.

## Custom handler

Python only, after AST admission. Must define `handle(payload) -> dict`. Allowed imports are a closed stdlib set (`json`, `math`, `datetime`, …). No `os`, no network, no files. AST admission is **not** a sandbox; the stub runtime is **not** a VM.

## Pinned image

```bash
export HESTIA_RUNTIME=docker
export HESTIA_ALLOW_IMAGE_DIGESTS=sha256:<64 hex>
```

The digest must already be on the **same Docker** you already run. Hestia will not `docker build` for you. Run this as a **host process** — do not mount `docker.sock` into the compose box (that is a shell onto the host). Host `docker.sock` is refused unless `HESTIA_ALLOW_HOST_DOCKER=1`.

```bash
HESTIA_RUNTIME=docker HESTIA_ALLOW_HOST_DOCKER=1 \
  HESTIA_ALLOW_IMAGE_DIGESTS=sha256:<64 hex> \
  HESTIA_DEPLOY_TOKEN=$HESTIA_DEPLOY_TOKEN \
  uv run --project . python -m hestia
```

## Announce to Hub

Off by default.

```bash
export HESTIA_HUB_URL=https://modelmarket.dev
# still a separate POST, unless you also set HESTIA_AUTO_ANNOUNCE=1
curl -X POST http://127.0.0.1:9480/v1/tenants/demo-echo/announce \
  -H "Authorization: Bearer $HESTIA_DEPLOY_TOKEN"
```

Announce can fail while the tenant keeps running. That is correct: hosting ≠ listing.

## Optional THEMIS

```bash
export HESTIA_THEMIS_URL=https://themis.modelmarket.dev
```

If THEMIS is down, deploys fail. That is fail-closed, not a skip.

## Self-host vs AICOM fleet

| | |
|---|---|
| You run `python -m hestia` or compose | Tenants live on **your** machine |
| AICOM reference hearth | Tenants live on the AICOM host behind `hestia.modelmarket.dev` |

Factory finishing a pipeline does not deploy anywhere. Wire a deploy step if you want that.

## Fleet (reference hearth)

On the host that will serve `hestia.modelmarket.dev` — A record must point **here**. Do not invent a box. Empty token refuses every write.

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
export HESTIA_PUBLIC_BASE=https://hestia.modelmarket.dev
sudo ./scripts/deploy_hestia.sh
```

Compose stays stub-only (no `docker.sock`). nginx is the TLS edge in front of `127.0.0.1:9480`.

## SQLite vs Postgres

| | When |
|---|---|
| SQLite (`HESTIA_DATABASE_URL` empty) | Laptop, CI, `pytest`. File under `HESTIA_DATA_DIR`. |
| Postgres (`postgresql://…`) | Production ledger. Dedicated `hestia` database. Fleet deploy starts `hestia-postgres` and writes `hestia/.env` (`chmod 600`). |

Postgres does **not** turn Hestia into a farm. `HESTIA_REPLICAS>1` still refuses: tenants are processes or containers on **this** host. There is no sticky runtime-owner.

Untrusted `handler.py` stub is not the advertised production sandbox. AST admission is not a sandbox. For cgroup/seccomp/gVisor isolation run `HESTIA_RUNTIME=docker` as a host process (`HESTIA_REQUIRE_SANDBOX=1` refuses stub). The live factory reference stays stub + Postgres on purpose — a production ledger without putting tenant containers on factory Docker.
