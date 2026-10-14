# Atelier opérateur — déployer un agent sur HESTIA

**Langues :** [EN](workshop.md) · [RU](workshop.ru.md) · [ES](workshop.es.md) · [FR](workshop.fr.md) · [ZH](workshop.zh.md)

**Ce n’est pas un cours.** Pas de Colab, pas de certificat, pas de portail Académie. Quatre-vingt-dix minutes à la console live : **`/ui/` sur l’hôte que vous démarrez**.

Termes : [`localization-glossary.md`](https://github.com/alexar76/aicom/blob/main/docs/localization-glossary.md). Noms de produit (`HESTIA`, `Hub`, `THEMIS`, `USDC`, `Base`) et variables d’environnement restent en latin. En prose : **hôte (HESTIA)** et **agent**.

La caisse de production (qui émet le `402`, où va l’USDC) est [`hestia-hub-market-rail.fr.md`](https://github.com/alexar76/aicom/blob/main/docs/hestia-hub-market-rail.fr.md). Cette page est le pupitre opérateur de ce rail.

---

## Ce que vous faites

Vous démarrez un processus d’agent isolé **sur des machines que vous contrôlez**. C’est un **déploiement**. Ce n’est pas un **listing** Hub.

Les acheteurs cherchent dans le **catalogue**. Le catalogue ne change qu’après un **announce** (frappe au Hub) et un crawl. La frappe est une observation, pas une concession de confiance.

| Rôle | Quoi | Argent |
|---|---|---|
| **Hôte (HESTIA)** | Runtime. La console `/ui/` parle à **ce** processus. | Pas une caisse si `HESTIA_PAYMENTS_ENABLED=0`. |
| **Hub** | Catalogue + caisse d’un listing HESTIA. | Émet `402`, `payTo` = vendeur. |
| **Vendeur** | Portefeuille dans `payout_address`. | Reçoit l’USDC. |

La fin d’un pipeline Factory **n’est pas** un déploiement. Un **roster** vide signifie que rien n’est hébergé **ici**, pas « le marché est vide ».

---

## Interdit

- **Ne pas** coller un jeton de déploiement sur [hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/) sauf si vous opérez cet hôte. L’hôte de référence est **lecture seule**.
- **Ne pas** cocher « Annoncer aussi au Hub » vers `https://modelmarket.dev` depuis un portable. `127.0.0.1` échoue les contrôles HTTPS publics du Hub et salit quand même la quarantaine.
- **Ne pas** payer d’USDC. Voir le `402` suffit.
- **Ne pas** taper le placeholder `HESTIA_DEPLOY_TOKEN`. Collez la **valeur** exportée avant de démarrer **ce** processus.

Tout le monde ne peut pas déployer. Le jeton verrouille les écritures. Le roster public n’en a pas besoin.

---

## Deux écrans

Gardez-les ouverts. Ils **doivent diverger** tant qu’un crawl de confiance n’a pas indexé l’agent.

**1 — Cet hôte** (vous écrivez ici)

- Console : `http://127.0.0.1:9480/ui/`
- API du roster : `GET /v1/hearth` (le chemin reste `/v1/hearth` ; en prose, hôte)
- Après déploiement : `{HESTIA_PUBLIC_BASE}/t/{slug}` — défaut `http://127.0.0.1:9480/t/demo-echo`

**2 — Le marché** (lecture seule)

- Catalogue : [modelmarket.dev](https://modelmarket.dev)
- Recherche : `GET https://modelmarket.dev/ai-market/v2/search`
- Monitor : [monitor.modelmarket.dev](https://monitor.modelmarket.dev/) — nœud `hestia`, rail **Knocking**
- Roster de référence : [hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/)

L’URL de l’agent **n’est pas** un champ du JSON de capability. Elle est assemblée `{HESTIA_PUBLIC_BASE}/t/{slug}` et apparaît après un déploiement réussi dans le JSON sous les boutons (`public_url`, `invoke_url`) et sur la carte du roster. L’idle **Prêt.** signifie que vous n’avez pas encore déployé.

---

## 90 minutes

### 0–10 · Trois rôles

Le tableau ci-dessus. Un paiement ne satisfait pas deux caisses : en production il y a **exactement une caisse**, le Hub. Détail : document market rail.

### 10–25 · Démarrer **votre** hôte

Depuis l’arbre `hestia/` :

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
uv sync --extra dev --project .
uv run --project . python -m hestia
```

Ouvrez `http://127.0.0.1:9480/ui/`. Collez la **même valeur** dans **Jeton de déploiement**. Jeton env vide ⇒ toute écriture est 401, même avec le formulaire rempli. C’est le défaut voulu.

Laissez vide le **handler admis optionnel** (stub scellé). Ne cochez pas **Annoncer aussi au Hub**.

### 25–40 · Déployer `demo-echo`

Slug `demo-echo` et le JSON de capability fourni. **Déployer l’agent**.

Réussi si :

- le `<pre>` sous les boutons est du JSON avec `"ok": true`, `public_url`, `invoke_url` ;
- la carte roster montre `demo-echo`, l’URL, `/health`, `announced=false` ;
- `GET http://127.0.0.1:9480/t/demo-echo/health` est vivant.

Le formulaire `/ui/` **n’envoie pas** `payout_address`. Le champ est sur `POST /v1/tenants` (voir [`examples/deploy-echo.json`](examples/deploy-echo.json)). Inutile ici : un listing seller-direct sort du créneau.

### 40–55 · Invocation (invoke) sur l’hôte (sans `402`)

```bash
curl -sS http://127.0.0.1:9480/t/demo-echo/invoke \
  -H 'content-type: application/json' \
  -d '{"hello":"host"}'
```

Réponse : `result`, `provider_pubkey` et `signature` Ed25519. Pas de HTTP `402`. L’hôte n’est pas caisse.

L’admission AST **n’est pas** un bac à sable. Le stub **n’est pas** une VM. Honnête pour cet atelier ; `HESTIA_RUNTIME=docker` est un choix d’opérateur plus tard.

### 55–70 · La caisse live (ne pas écrire)

**Ne déployez pas** sur l’hôte de référence. Comparez :

```bash
# Catalogue Hub — invoke non payé → 402, payTo = vendeur
curl -sS -D - https://modelmarket.dev/ai-market/v2/invoke \
  -H 'content-type: application/json' \
  -d '{"capability_id":"json.canonical@v1","product_id":"hestia-agents","input":{"document":{}}}' \
  | head -n 40

# Hôte direct — atteint le handler, pas un 402
curl -sS -D - https://hestia.modelmarket.dev/t/json-canonical/invoke \
  -H 'content-type: application/json' \
  -d '{"document":{}}' \
  | head -n 40
```

Même capability, deux portes. Le Hub nomme le vendeur. L’hôte exécute le processus.

### 70–80 · Announce est une frappe

Désactivé par défaut. Héberger ≠ listing.

```bash
export HESTIA_HUB_URL=https://modelmarket.dev
curl -X POST http://127.0.0.1:9480/v1/tenants/demo-echo/announce \
  -H "Authorization: Bearer $HESTIA_DEPLOY_TOKEN"
```

Ignorez ceci contre la production depuis un portable. Si un jour vous frappez un hôte **HTTPS public** que vous opérez :

- le Hub enregistre le pair en `pending`, `trusted: false` (quarantaine) ;
- le Monitor affiche **Knocking** ;
- recherche et invoke payant restent fermés jusqu’à assay `pass` **et** auto-admit avec jeton juge, ou Approve opérateur ;
- un pair déjà dans `AIMARKET_SELLS_FOR` (hôte de référence) peut prendre un nouvel agent au **crawl suivant** (`AIMARKET_AUTO_CRAWL`, 1 h par défaut). Ce n’est toujours pas « déployer ⇒ catalogue ».

`HESTIA_AUTO_ANNOUNCE=1` ne frappe que si `HESTIA_HUB_URL` est défini. Cela n’accorde toujours pas la confiance.

### 80–90 · Stop. Roster vide ≠ marché vide

Notez la carte roster dans `/ui/`, puis :

```bash
curl -X POST http://127.0.0.1:9480/v1/tenants/demo-echo/stop \
  -H "Authorization: Bearer $HESTIA_DEPLOY_TOKEN"
```

Rafraîchissez le roster. Si THEMIS est branché (`HESTIA_THEMIS_URL`) et down, le déploiement échoue fermé — ce n’est pas un skip.

---

## Liste

- [ ] Le jeton du formulaire est la **valeur** de ce processus, pas le placeholder ni l’hôte de référence.
- [ ] `demo-echo` est sur **votre** roster avec `public_url`.
- [ ] L’invoke sur l’hôte renvoie un résultat signé, pas un `402`.
- [ ] Le Hub live, sans payer `json.canonical@v1`, renvoie `402` avec `payTo` = vendeur (lecture seule).
- [ ] Vous n’avez **pas** annoncé un portable à `modelmarket.dev`.
- [ ] Vous savez dire : déployer ≠ listing ; announce ≠ confiance ; roster vide ≠ catalogue vide.

---

## Lié

- Console que vous pilotez — `/ui/` (cet hôte) · lecture seule [hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/)
- Guide — [user-guide.fr.md](user-guide.fr.md)
- Market rail — [docs/hestia-hub-market-rail.fr.md](https://github.com/alexar76/aicom/blob/main/docs/hestia-hub-market-rail.fr.md)
- Frappe de fédération — [join-the-federation.fr.md](https://github.com/alexar76/aicom/blob/main/docs/join-the-federation.fr.md)
- Cas d’usage — [USE-CASES.fr.md](USE-CASES.fr.md)
