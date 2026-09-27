# HESTIA

<p align="center">
  <a href="../README.md">English</a> ·
  <a href="README.ru.md">Русский</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.fr.md"><b>Français</b></a> ·
  <a href="README.zh.md">中文</a>
</p>

**HESTIA** (Ἑστία) — l’**âtre** pour les fournisseurs de capacité AIMarket.

Runtime hébergé et isolé · pas le catalogue du Hub · pas un job board · pas Factory.

**Capacités :** `hestia.host.deploy@v1` · `hestia.host.status@v1` · `hestia.hearth.list@v1` · `hestia.host.stop@v1` ·
**Port :** `9480` ·
**Landing :** [alexar76.github.io/hestia](https://alexar76.github.io/hestia/) ·
**Âtre de référence (quand le DNS est up) :** [hestia.modelmarket.dev](https://hestia.modelmarket.dev)

## Réponse directe

**Ce n’est pas un tableau où les agents Factory apparaissent tout seuls.**

Il faut **déployer un bundle signé sur cet âtre**. Tant que ce n’est pas fait, le roster est vide — et vide signifie « rien n’est hébergé ici », pas « le marché est vide ».

**Où ils s’hébergent :** sur les **machines de l’opérateur Hestia**.

| Mode | Qui exécute le processus |
|---|---|
| Âtre de référence | Flotte AICOM (`hestia.modelmarket.dev`) |
| Auto-hébergé | **Votre** serveur, portable ou compose |
| Non | Le portable de l’auteur, Hub, Factory, THEMIS ou ARGUS |

Le Hub reste le catalogue. THEMIS (optionnel) peut refuser avant le démarrage. L’annonce est un coup explicite, pas une concession de confiance.

## Couches (ne pas les fusionner)

| Nœud | Question | Couche |
|---|---|---|
| **Factory** / `create-aimarket-agent` | Scaffolder un fournisseur ? | Source sur disque. Personne n’écoute encore. |
| **THEMIS** | Entrer dans le catalogue Hub ? | Admission à la publication |
| **HESTIA** | Où tourne vraiment le processus du vendeur ? | Runtime hébergé sur les machines de l’opérateur |
| **Hub** | Que peut trouver et payer un acheteur ? | Catalogue + règlement |
| **ARGUS** | Comment le consommer ? | Acheteur / desktop |

Une URL d’âtre **n’est pas** une ligne de catalogue.

## Isolation

Par défaut, un **sous-processus loopback scellé** (`HESTIA_RUNTIME=stub` ; pas de cgroups ; l’AST n’est pas un bac à sable). **La boîte Hestia ne pilote pas l’hôte. Les locataires non plus.** Compose = un satellite (pas de CLI docker, pas de `docker.sock`, pas de privileged). Le runtime docker est un processus **sur l’hôte** (`python -m hestia` + `HESTIA_ALLOW_HOST_DOCKER=1`). Seulement des digest `sha256:` de `HESTIA_ALLOW_IMAGE_DIGESTS`. Chaque locataire reçoit son propre réseau bridge `Internal` (`hestia-tenants-{slug}`), sans IPv6, sur un /29 de `HESTIA_TENANT_SUBNET_POOL` ; aucune route d’un locataire à un autre. La passerelle de ce réseau est l’hôte du moteur : lancez `sudo scripts/tenant-host-firewall.sh apply` sur cet hôte pour qu’un locataire n’atteigne pas les services de l’hôte à l’écoute sur toutes les adresses. Un moteur TCP sans TLS est refusé, quelle que soit l’adresse (y compris `tcp://127.0.0.1`). Au démarrage, Hestia vérifie chaque locataire image et déplace sur des réseaux privés ceux encore sur l’ancien réseau partagé ; celui qu’il ne peut pas vérifier passe en `quarantined` (non servi, réessayé via `POST /v1/admin/reconcile`). Un hearth `stub` n’appelle jamais docker. `--cap-drop ALL`, `--read-only`, uid `65532`.

## Démarrage rapide

```bash
cd hestia
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
uv sync --extra dev --project .
uv run --project . pytest -q
uv run --project . python -m hestia
# console : http://127.0.0.1:9480/ui/
```

Un `HESTIA_DEPLOY_TOKEN` vide refuse toute écriture.

Guide : [user-guide.fr.md](user-guide.fr.md) · atelier : [workshop.fr.md](workshop.fr.md) · cas : [USE-CASES.fr.md](USE-CASES.fr.md) · architecture : [ARCHITECTURE.md](ARCHITECTURE.md) (EN).

Le nom `HESTIA` reste en latin. Licence MIT.
