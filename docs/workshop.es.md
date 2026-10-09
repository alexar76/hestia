# Taller de operador — desplegar un agente en HESTIA

**Idiomas:** [EN](workshop.md) · [RU](workshop.ru.md) · [ES](workshop.es.md) · [FR](workshop.fr.md) · [ZH](workshop.zh.md)

**No es un curso.** Sin Colab, sin certificado, sin portal de Academia. Noventa minutos en la consola en vivo: **`/ui/` en el host que arrancas**.

Términos: [`localization-glossary.md`](https://github.com/alexar76/aicom/blob/main/docs/localization-glossary.md). Nombres de producto (`HESTIA`, `Hub`, `THEMIS`, `USDC`, `Base`) y variables de entorno se quedan en latín. En prosa: **host (HESTIA)** y **agente**.

La caja de producción (quién emite el `402`, adónde va el USDC) es [`hestia-hub-market-rail.es.md`](https://github.com/alexar76/aicom/blob/main/docs/hestia-hub-market-rail.es.md). Esta página es el escritorio del operador para ese rail.

---

## Qué estás haciendo

Arrancas un proceso de agente aislado **en máquinas que controlas**. Eso es un **despliegue**. No es un **listing** del Hub.

Los compradores buscan en el **catálogo**. El catálogo solo cambia tras un **announce** (golpe al Hub) y un crawl. El golpe es observación, no una concesión de confianza.

| Papel | Qué es | Dinero |
|---|---|---|
| **Host (HESTIA)** | Runtime. La consola `/ui/` habla con **este** proceso. | No es caja cuando `HESTIA_PAYMENTS_ENABLED=0`. |
| **Hub** | Catálogo + caja de un listing HESTIA. | Emite `402`, `payTo` = vendedor. |
| **Vendedor** | Cartera en `payout_address`. | Recibe USDC. |

Que Factory termine un pipeline **no** es un despliegue. Un **roster** vacío significa que aquí no se aloja nada, no que «el mercado está vacío».

---

## Prohibido

- **No** pegues un token de despliegue en [hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/) salvo que operes ese host. El host de referencia es **solo lectura**.
- **No** marques «Anunciar también al Hub» hacia `https://modelmarket.dev` desde un portátil. `127.0.0.1` falla las comprobaciones HTTPS públicas del Hub y aun así ensucia la cuarentena.
- **No** pagues USDC. Ver el `402` basta.
- **No** escribas el placeholder `HESTIA_DEPLOY_TOKEN`. Pega el **valor** que exportaste antes de arrancar **este** proceso.

No todo el mundo puede desplegar. El token cierra las escrituras. El roster público no pide token.

---

## Dos pantallas

Déjalas abiertas. **Deben discrepar** hasta que un crawl de confianza indexe el agente.

**1 — Este host** (aquí escribes)

- Consola: `http://127.0.0.1:9480/ui/`
- API del roster: `GET /v1/hearth` (la ruta sigue `/v1/hearth`; en prosa, host)
- Tras el despliegue: `{HESTIA_PUBLIC_BASE}/t/{slug}` — por defecto `http://127.0.0.1:9480/t/demo-echo`

**2 — El mercado** (solo lectura)

- Catálogo: [modelmarket.dev](https://modelmarket.dev)
- Búsqueda: `GET https://modelmarket.dev/ai-market/v2/search`
- Monitor: [monitor.modelmarket.dev](https://monitor.modelmarket.dev/) — nodo `hestia`, rail **Knocking**
- Roster de referencia: [hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/)

La URL del agente **no** es un campo del JSON de capability. Se arma como `{HESTIA_PUBLIC_BASE}/t/{slug}` y aparece tras un despliegue correcto en el JSON bajo los botones (`public_url`, `invoke_url`) y en la tarjeta del roster. El idle **Listo.** significa que aún no has desplegado.

---

## 90 minutos

### 0–10 · Tres papeles

La tabla de arriba. Un pago no satisface dos cajas: en producción hay **exactamente una caja**, el Hub. Detalle: documento del market rail.

### 10–25 · Arranca **tu** host

Desde el árbol `hestia/`:

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
uv sync --extra dev --project .
uv run --project . python -m hestia
```

Abre `http://127.0.0.1:9480/ui/`. Pega el **mismo valor** en **Token de despliegue**. Token vacío en el env ⇒ toda escritura es 401, aunque el formulario esté lleno. Es el valor por defecto buscado.

Deja vacío el **handler admitido opcional** (stub sellado). No marques **Anunciar también al Hub**.

### 25–40 · Desplegar `demo-echo`

Slug `demo-echo` y el JSON de capability de fábrica. **Desplegar agente**.

Pasa si:

- el `<pre>` bajo los botones es JSON con `"ok": true`, `public_url`, `invoke_url`;
- la tarjeta del roster muestra `demo-echo`, la URL, `/health`, `announced=false`;
- `GET http://127.0.0.1:9480/t/demo-echo/health` está vivo.

El formulario `/ui/` **no** envía `payout_address`. El campo está en `POST /v1/tenants` (véase [`examples/deploy-echo.json`](examples/deploy-echo.json)). Aquí no lo necesitas: un listing seller-direct queda fuera del taller.

### 40–55 · Invocación (invoke) en el host (sin `402`)

```bash
curl -sS http://127.0.0.1:9480/t/demo-echo/invoke \
  -H 'content-type: application/json' \
  -d '{"hello":"host"}'
```

Respuesta: `result`, `provider_pubkey` y `signature` Ed25519. No hay HTTP `402`. El host no es caja.

La admisión AST **no** es un sandbox. El stub **no** es una VM. Honesto para este taller; `HESTIA_RUNTIME=docker` es una decisión posterior.

### 55–70 · La caja en vivo (no escribas)

**No** despliegues en el host de referencia. Compara:

```bash
# Catálogo Hub — invoke sin pago → 402, payTo = vendedor
curl -sS -D - https://modelmarket.dev/ai-market/v2/invoke \
  -H 'content-type: application/json' \
  -d '{"capability_id":"json.canonical@v1","product_id":"hestia-agents","input":{"document":{}}}' \
  | head -n 40

# Host directo — llega al handler, no es un 402
curl -sS -D - https://hestia.modelmarket.dev/t/json-canonical/invoke \
  -H 'content-type: application/json' \
  -d '{"document":{}}' \
  | head -n 40
```

La misma capability, dos puertas. El Hub nombra al vendedor. El host ejecuta el proceso.

### 70–80 · Announce es un golpe

Desactivado por defecto. Alojar ≠ listing.

```bash
export HESTIA_HUB_URL=https://modelmarket.dev
curl -X POST http://127.0.0.1:9480/v1/tenants/demo-echo/announce \
  -H "Authorization: Bearer $HESTIA_DEPLOY_TOKEN"
```

Omítelo contra producción desde un portátil. Si algún día golpeas un host **HTTPS público** que operas:

- el Hub registra el peer como `pending`, `trusted: false` (cuarentena);
- el Monitor muestra **Knocking**;
- búsqueda e invoke de pago siguen cerrados hasta assay `pass` **y** auto-admit con judge token, o Approve del operador;
- un peer ya en `AIMARKET_SELLS_FOR` (el host de referencia) puede recoger un agente nuevo en el **siguiente crawl** (`AIMARKET_AUTO_CRAWL`, 1 h por defecto). Sigue sin ser «desplegar ⇒ catálogo».

`HESTIA_AUTO_ANNOUNCE=1` solo golpea si `HESTIA_HUB_URL` está definido. Aun así no otorga confianza.

### 80–90 · Stop. Roster vacío ≠ mercado vacío

Mira la tarjeta del roster en `/ui/`, luego:

```bash
curl -X POST http://127.0.0.1:9480/v1/tenants/demo-echo/stop \
  -H "Authorization: Bearer $HESTIA_DEPLOY_TOKEN"
```

Actualiza el roster. Si THEMIS está cableado (`HESTIA_THEMIS_URL`) y caído, el despliegue falla cerrado — no se salta.

---

## Lista

- [ ] El token del formulario es el **valor** de este proceso, no el placeholder ni el host de referencia.
- [ ] `demo-echo` está en **tu** roster con `public_url`.
- [ ] El invoke en el host devuelve un resultado firmado, no un `402`.
- [ ] El Hub en vivo, sin pagar `json.canonical@v1`, devuelve `402` con `payTo` = vendedor (solo lectura).
- [ ] **No** anunciaste un portátil a `modelmarket.dev`.
- [ ] Puedes explicar: desplegar ≠ listing; announce ≠ confianza; roster vacío ≠ catálogo vacío.

---

## Relacionado

- Consola que estás usando — `/ui/` (este host) · solo lectura [hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/)
- Guía — [user-guide.es.md](user-guide.es.md)
- Market rail — [docs/hestia-hub-market-rail.es.md](https://github.com/alexar76/aicom/blob/main/docs/hestia-hub-market-rail.es.md)
- Golpe de federación — [join-the-federation.es.md](https://github.com/alexar76/aicom/blob/main/docs/join-the-federation.es.md)
- Casos — [USE-CASES.es.md](USE-CASES.es.md)
