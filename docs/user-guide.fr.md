# Guide utilisateur

🌐 [English](user-guide.md) · [Русский](user-guide.ru.md) · [Español](user-guide.es.md) · **Français** · [中文](user-guide.zh.md)

## Ce que vous opérez

Un **âtre** : des machines sous votre contrôle qui exécutent des fournisseurs AIMarket isolés. La console `/ui/` parle à **ce** processus. Ce n’est pas la recherche Hub.

Les agents n’apparaissent au roster qu’après un `POST /v1/tenants` (ou `hestia.host.deploy@v1`) réussi **sur cet hôte**.

## En local

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
uv sync --extra dev --project .
uv run --project . python -m hestia
```

Ouvrez http://127.0.0.1:9480/ui/ — collez le jeton, déployez l’écho de démo, ouvrez `/t/demo-echo/health`.

## Invoquer un locataire

```bash
curl -sS http://127.0.0.1:9480/t/demo-echo/invoke \
  -H 'content-type: application/json' \
  -d '{"hello":"hearth"}'
```

## Handler personnalisé

Python uniquement, après admission AST. Il faut `handle(payload) -> dict`. Ensemble stdlib fermé. Pas d’`os`, pas de réseau, pas de fichiers.

## Image épinglée

```bash
export HESTIA_RUNTIME=docker
export HESTIA_ALLOW_IMAGE_DIGESTS=sha256:<64 hex>
```

Le digest doit déjà être sur le **même Docker** que vous avez déjà. Hestia ne lance pas `docker build`. C’est un processus **sur l’hôte**, pas un socket dans la boîte compose (cela serait un shell vers l’hôte). Le `docker.sock` hôte est refusé sauf `HESTIA_ALLOW_HOST_DOCKER=1`.

## Annoncer au Hub

Désactivé par défaut. Il faut `HESTIA_HUB_URL` et un `POST /v1/tenants/{slug}/announce` (ou `HESTIA_AUTO_ANNOUNCE=1`). Héberger ≠ référencer.

## Auto-hébergement vs flotte AICOM

Vous lancez Hestia — les locataires vivent sur **votre** machine. L’âtre de référence AICOM vit derrière `hestia.modelmarket.dev`. Factory ne déploie nulle part à la fin d’un pipeline.

## Flotte (âtre de référence)

Sur l’hôte qui servira `hestia.modelmarket.dev` — l’enregistrement A doit pointer **ici**. N’inventez pas la machine. Un jeton vide refuse toute écriture.

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
export HESTIA_PUBLIC_BASE=https://hestia.modelmarket.dev
sudo ./scripts/deploy_hestia.sh
```

Compose reste stub (pas de `docker.sock`). nginx est le bord TLS devant `127.0.0.1:9480`.
