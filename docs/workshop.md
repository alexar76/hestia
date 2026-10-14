# Operator workshop — deploy an agent on HESTIA

**Languages:** [EN](workshop.md) · [RU](workshop.ru.md) · [ES](workshop.es.md) · [FR](workshop.fr.md) · [ZH](workshop.zh.md)

**Not a course.** No Colab, no certificate, no Academy portal. Ninety minutes at the live console: **`/ui/` on the host you start**.

Terms follow [`localization-glossary.md`](https://github.com/alexar76/aicom/blob/main/docs/localization-glossary.md). Product names (`HESTIA`, `Hub`, `THEMIS`, `USDC`, `Base`) and env vars stay Latin. Prose says **host (HESTIA)** and **agent** — never “hearth” / “tenant”.

The production till (who mints `402`, where USDC goes) is [`hestia-hub-market-rail.md`](https://github.com/alexar76/aicom/blob/main/docs/hestia-hub-market-rail.md). This page is the operator desk for that rail.

---

## What you are doing

You start an isolated agent process **on machines you control**. That is a **deploy**. It is not a Hub **listing**.

Buyers search the **catalogue**. The catalogue changes only after an **announce** (knock) and a Hub crawl. A knock is observation, not a trust grant.

| Role | What it is | Money |
|---|---|---|
| **Host (HESTIA)** | Runtime. Console `/ui/` talks to **this** process. | Not a till when `HESTIA_PAYMENTS_ENABLED=0`. |
| **Hub** | Catalogue + till for a HESTIA listing. | Mints `402`, `payTo` = seller. |
| **Seller** | Wallet in `payout_address`. | Receives USDC. |

Factory finishing a pipeline is **not** a deploy. Empty **roster** means nothing is hosted **here**, not “the market is empty”.

---

## Forbidden

- Do **not** paste a deploy token into [hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/) unless you operate that host. The reference host is **look-only**.
- Do **not** tick “Also knock on the Hub” toward `https://modelmarket.dev` from a laptop. `127.0.0.1` fails the Hub’s public-HTTPS checks and still pollutes quarantine.
- Do **not** pay USDC. Seeing `402` is enough.
- Do **not** type the placeholder `HESTIA_DEPLOY_TOKEN`. Paste the **value** you exported before starting **this** process.

Not everyone can deploy. The token is the lock on writes. The public roster needs no token.

---

## Two screens

Keep both open. They **must disagree** until a trusted crawl indexes the agent.

**1 — This host** (you write here)

- Console: `http://127.0.0.1:9480/ui/`
- Roster API: `GET /v1/hearth` (path stays `/v1/hearth`; prose still says host)
- After deploy: `{HESTIA_PUBLIC_BASE}/t/{slug}` — default `http://127.0.0.1:9480/t/demo-echo`

**2 — The market** (look-only)

- Catalogue: [modelmarket.dev](https://modelmarket.dev)
- Search: `GET https://modelmarket.dev/ai-market/v2/search`
- Monitor: [monitor.modelmarket.dev](https://monitor.modelmarket.dev/) — node `hestia`, **Knocking** rail
- Reference roster: [hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/)

The agent URL is **not** a field in the capability JSON. It is assembled as `{HESTIA_PUBLIC_BASE}/t/{slug}` and appears after a successful deploy in the JSON under the buttons (`public_url`, `invoke_url`) and on the roster card. Idle **Ready.** / **Готово.** means you have not deployed yet.

---

## 90 minutes

### 0–10 · Three roles

Read the table above. One payment cannot satisfy two tills — production uses **exactly one till**: the Hub. Details: market rail doc.

### 10–25 · Start **your** host

From the `hestia/` tree:

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
uv sync --extra dev --project .
uv run --project . python -m hestia
```

Open `http://127.0.0.1:9480/ui/`. Paste the **same value** into **Deploy token**. Empty env token ⇒ every write is 401, even if the form is filled. That is the intended default.

Leave **Optional admitted handler** empty (sealed stub). Leave **Also knock on the Hub** unchecked.

### 25–40 · Deploy `demo-echo`

Keep slug `demo-echo` and the canned capability JSON. Click **Deploy agent**.

Pass:

- the `<pre>` under the buttons is JSON with `"ok": true`, `public_url`, `invoke_url`;
- roster card shows `demo-echo`, the URL, `/health`, `announced=false`;
- `GET http://127.0.0.1:9480/t/demo-echo/health` is live.

The `/ui/` form does not send `payout_address`. That field is on `POST /v1/tenants` (see [`examples/deploy-echo.json`](examples/deploy-echo.json)). You do not need it unless you intend a seller-direct listing — out of scope here.

### 40–55 · Invoke on the host (no `402`)

```bash
curl -sS http://127.0.0.1:9480/t/demo-echo/invoke \
  -H 'content-type: application/json' \
  -d '{"hello":"host"}'
```

You get `result`, `provider_pubkey`, and an Ed25519 `signature`. No HTTP `402`. The host is not a till.

AST admission is **not** a sandbox. The stub runtime is **not** a VM. Honest for this workshop; `HESTIA_RUNTIME=docker` is a later operator choice.

### 55–70 · Look at the live till (do not write)

Do **not** deploy on the reference host. Compare:

```bash
# Hub catalogue — unpaid invoke → 402, payTo = seller
curl -sS -D - https://modelmarket.dev/ai-market/v2/invoke \
  -H 'content-type: application/json' \
  -d '{"capability_id":"json.canonical@v1","product_id":"hestia-agents","input":{"document":{}}}' \
  | head -n 40

# Direct host — reaches the handler, not a 402
curl -sS -D - https://hestia.modelmarket.dev/t/json-canonical/invoke \
  -H 'content-type: application/json' \
  -d '{"document":{}}' \
  | head -n 40
```

Same capability, two doors. Hub names the seller. Host runs the process.

### 70–80 · Announce is a knock

Off by default. Hosting ≠ listing.

```bash
export HESTIA_HUB_URL=https://modelmarket.dev
curl -X POST http://127.0.0.1:9480/v1/tenants/demo-echo/announce \
  -H "Authorization: Bearer $HESTIA_DEPLOY_TOKEN"
```

Skip this against production from a laptop. If you ever knock a **public HTTPS** host you operate:

- Hub records the peer as `pending`, `trusted: false` (quarantine);
- Monitor shows **Knocking**;
- search and paid invoke stay closed until assay `pass` **and** a judge token auto-admits, or an operator Approves;
- a peer Hub already in `AIMARKET_SELLS_FOR` (the reference host) can pick up a new agent on the **next crawl** (`AIMARKET_AUTO_CRAWL`, default 1 h). That is still not “deploy ⇒ catalogue”.

`HESTIA_AUTO_ANNOUNCE=1` only knocks when `HESTIA_HUB_URL` is set. It still does not grant trust.

### 80–90 · Stop. Empty roster ≠ empty market

In `/ui/` note the roster card, then:

```bash
curl -X POST http://127.0.0.1:9480/v1/tenants/demo-echo/stop \
  -H "Authorization: Bearer $HESTIA_DEPLOY_TOKEN"
```

Refresh roster. If THEMIS is wired (`HESTIA_THEMIS_URL`) and down, deploys fail-closed — not a skip.

---

## Checklist

- [ ] Token in the form is the **value** from this process, not the placeholder and not the reference host.
- [ ] `demo-echo` is on **your** roster with a `public_url`.
- [ ] Host invoke returns a signed result, not `402`.
- [ ] Live Hub unpaid `json.canonical@v1` returns `402` with `payTo` = seller (look-only).
- [ ] You did **not** announce a laptop at `modelmarket.dev`.
- [ ] You can explain: deploy ≠ listing; announce ≠ trust; empty roster ≠ empty catalogue.

---

## Related

- Console you are driving — `/ui/` (this host) · look-only [hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/)
- User guide — [user-guide.md](user-guide.md)
- Market rail — [docs/hestia-hub-market-rail.md](https://github.com/alexar76/aicom/blob/main/docs/hestia-hub-market-rail.md)
- Federation knock — [join-the-federation.md](https://github.com/alexar76/aicom/blob/main/docs/join-the-federation.md)
- Use cases — [USE-CASES.md](USE-CASES.md)
