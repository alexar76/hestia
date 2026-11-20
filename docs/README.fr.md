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

> **Vous voulez vendre une fonction sans serveur ?** Cinq commandes la placent sur le hearth de référence ; les acheteurs paient votre portefeuille → [vendre sans serveur](sell-without-a-server.fr.md)

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

## Bac à sable (`HESTIA_RUNTIME=wasm`)

Avec `HESTIA_RUNTIME=wasm`, un handler ne tourne jamais comme un processus à lui. Chaque appel s'exécute dans
une **instance WebAssembly neuve** : CPython 3.14 compilé pour WASI, exécuté par wasmtime, dans le service séparé
`hestia-runner` (`docker compose --profile wasm up -d`). Ce conteneur n'a **aucun réseau**, **aucun secret** (rien
de l'environnement du foyer), un système de fichiers en lecture seule et une seule socket Unix dans un volume
partagé avec le foyer. Dans l'instance, le handler voit la bibliothèque standard en lecture seule, et rien d'autre :

| Le handler tente de | Il obtient |
|---|---|
| lire `/data/provider.key`, `/etc/passwd`, `~/.ssh`, la socket du runner | `FileNotFoundError` : aucun chemin de l'hôte n'existe |
| écrire dans la bibliothèque standard | `PermissionError` (lecture seule) |
| ouvrir une socket | pas de sockets dans WASI preview 1 |
| lancer un processus, charger du code natif (`ctypes`) | non pris en charge par WASI |
| lire l'environnement | deux variables fixes : `PYTHONHASHSEED`, `PYTHONHOME` |
| dépasser la limite, boucler sans fin, inonder stdout | `MemoryError` (256 Mio), délai dépassé (10 s, epoch interruption), sortie refusée au-delà de 4 Mio |
| garder un état entre appels ou lire celui d'un autre | chaque appel est une instance neuve |

Le foyer signe le résultat avec la clé propre du tenant **hors** du bac à sable (la clé n'y entre jamais), avec
le canonique et les codes du stub : un tenant passé de `stub` à `wasm` garde sa clé et ses réponses se vérifient
comme avant. Le bac à sable étant la frontière, un handler `wasm` passe un lint, pas la liste blanche du stub :
il peut importer tout module de la build WASI (`base64`, `difflib`, `typing`, `decimal`, `unicodedata`… ; pas
`zlib`, ni sockets, ni threads). Entre deux appels il n'y a pas de processus : un agent inactif coûte du disque,
pas de mémoire ; des centaines tiennent là où le stub en gardait une trentaine.

Prouvé sur le foyer de référence avec l'agent d'un inconnu écrit pour s'échapper : chaque chemin essayé a donné
`FileNotFoundError`, réseau et processus indisponibles, deux variables dans son environnement.

### Agents en JavaScript

Depuis le 20/11/2026, un handler peut aussi être en **JavaScript** : `"language": "javascript"` dans le
`source` du déploiement. Il s'exécute dans le même runner avec les mêmes limites, dans **QuickJS-ng 0.17.0
compilé pour WASI** (épinglé par SHA-256 dans `scripts/fetch_qjs_wasi.py`), qui ne reçoit aucun répertoire
ouvert ni aucun environnement. Le handler est un script classique (pas un module ES) qui définit
`handle(payload)`, simple ou `async` ; un projet avec des paquets est d'abord regroupé en un fichier, par
exemple `esbuild index.ts --bundle --format=iife --global-name=handler --platform=neutral`, qui laisse
`handler.handle`. Jusqu'à 200 000 caractères (la requête de déploiement doit tenir dans 256 Kio).
`fetch(url, {method, headers, body, json})` fonctionne comme en Python : seulement les `egress_hosts` du
déploiement, via le foyer, et renvoie une Promise d'une réponse avec `ok`, `status`, `headers.get()`, `text()`
et `json()`.

Ce qui **n'y est pas** : les modules de Node (`fs`, `crypto`, `Buffer`, `process`), les minuteries et les vraies
E/S. Les `qjs:std` et `qjs:os` du moteur peuvent être importés ; sous WASI ils n'atteignent rien d'autre que la
sortie du handler, et la réponse que le foyer signe est écrite en dernier par l'invité du runner, une fois le
handler terminé. Une Promise qui ne peut jamais se résoudre est aussitôt rendue comme une erreur, une récursion
profonde est une `RangeError` (pile du moteur de 256 Kio), mémoire, temps et sortie sont plafonnés comme en
Python. La liste montre `"language": "javascript"` pour cet agent. Le premier est `warden.scan@v1`
(hestia-agents), l'analyse statique des définitions d'outils MCP de WARDEN.

```json
{"slug": "my-agent",
 "capability": {"...": "..."},
 "source": {"kind": "template", "language": "javascript",
            "handler": "function handle(payload) { return {words: payload.text.split(/\\s+/).length}; }"},
 "owner_pubkey": "...", "payout_address": "0x..."}
```


### Hôtes nommés par l'appelant

Un agent n'atteint le réseau qu'à travers le foyer et seulement les hôtes des `egress_hosts` de
son déploiement (`HESTIA_TENANT_EGRESS=1` ; les règles sont dans le README anglais, « Network for
agents »). Certains contrôles portent sur un hôte que seul l'appelant connaît : « *ce* domaine
publie-t-il un enregistrement AID valide, et son point d'accès prouve-t-il la clé ? ». Depuis le
20/11/2026, un déploiement peut nommer jusqu'à trois **champs d'entrée** de premier niveau dont la
valeur dans un appel — un nom d'hôte ou une URL `https://` — ajoute cet hôte pour ce seul appel :

```json
{"slug": "aid-discover", "egress_hosts": ["cloudflare-dns.com", "dns.google"],
 "egress_input_fields": ["domain", "endpoint"], "capability": {…}, "source": {…}}
```

Un appel avec `{"domain": "example.com"}` peut alors interroger `https://example.com/…` en plus des
deux résolveurs fixes ; l'appel suivant, avec un autre domaine, ne le peut pas. Toutes les règles
habituelles s'appliquent à cet hôte (adresses publiques seulement, https sur 443, pas de
redirection, tailles et délais). En plus :

- chaque hôte nommé par l'appelant a son propre budget sur tout le foyer,
  `DEFAULT_TARGET_PER_MINUTE` (20 requêtes par minute, tous agents et appelants confondus) : on ne
  peut pas diriger le foyer contre un site ;
- seuls l'opérateur et les propriétaires qu'il a admis peuvent déclarer des champs d'entrée. Une
  clé d'inscription ouverte ne peut nommer que des `egress_hosts` fixes : une clé gratuite avec des
  hôtes nommés par l'appelant serait un proxy vers n'importe quel site public depuis l'adresse du
  foyer ;
- la liste affiche `egress_input_fields` à côté d'`egress_hosts` : l'acheteur voit ce que l'agent
  peut atteindre avant de l'appeler.

Chaque tour d'un même appel reçoit aussi le même **`CALL_NONCE`**, 32 octets aléatoires en hex,
global dans les deux langages. Un handler qui a besoin d'un défi frais (une demande de preuve AID)
l'utilise et demande la même chose à chaque tour, si bien que la réponse déjà obtenue par le foyer
lui répond. `aid.discover@v1` (hestia-agents) repose sur les deux. Dans le bac à sable, un handler
Python peut désormais être aussi grand qu'un handler JavaScript, 200 000 caractères ; hors du bac à
sable, la limite reste 32 000.

## Propriétaires

Un foyer partagé héberge les agents de plusieurs entreprises : le jeton de l'opérateur n'est donc pas la seule clé. Un **propriétaire** est une clé Ed25519 que l'opérateur admet (`POST /v1/owners`). Chaque requête d'un propriétaire est signée sur la base publique du foyer, la méthode, le chemin, un horodatage unix (±300 s), un nonce à usage unique et le SHA-256 du corps (`X-Hestia-Owner` / `-Timestamp` / `-Nonce` / `-Signature`, format `hestia-owner/1` dans [`hestia/owners.py`](../hestia/owners.py)).

| Qui | Peut |
|---|---|
| Opérateur (jeton) | tout, comme avant ; admet, limite et suspend les propriétaires |
| Propriétaire | déployer sur un slug libre ou le sien, dans son quota (`HESTIA_OWNER_MAX_TENANTS`, 3 par défaut) ; arrêter, annoncer et lister **uniquement ses** agents ; lire son statut via `GET /v1/owners/me` |
| Tous les autres | lire la liste publique, appeler les agents |

Sont refusées : une requête rejouée, une requête signée pour un autre foyer, chemin ou corps, et un propriétaire qui met une autre clé dans `owner_pubkey`. La clé factice à zéros et toute autre clé Ed25519 d'ordre faible ne peuvent rien posséder : les agents déployés avec la clé factice restent à l'opérateur seul.

`HESTIA_OPEN_OWNERS=1` permet à toute clé valide de s'admettre elle-même au premier déploiement. Avec le stub, elle n'a pas le droit d'exécuter du code — l'admission AST n'est pas un bac à sable — donc les inconnus ne déploient que des images épinglées et des stubs scellés. Avec `HESTIA_RUNTIME=wasm`, `HESTIA_OPEN_OWNER_CODE=1` leur donne ce droit : leur code s'exécute dans le bac à sable ci-dessus, et leurs agents n'entrent dans le manifeste public qu'à travers la revue ci-dessous. Les deux sont désactivés par défaut ; le foyer de référence tourne avec les deux activés.

```bash
python -m hestia.owner_cli keygen --out owner.key        # affiche la clé publique pour l'opérateur
python -m hestia.owner_cli deploy --hearth https://hestia.example --key owner.key --body deploy.json
python -m hestia.owner_cli stop   --hearth https://hestia.example --key owner.key --slug my-agent
python -m hestia.owner_cli proof  --key owner.key --domain example.com   # affiche l'enregistrement TXT et le fichier qui prouvent votre domaine
python -m hestia.owner_cli domain --hearth https://hestia.example --key owner.key --domain example.com
```

## Revue avant publication

Les hubs indexent le manifeste du foyer : un agent qui y figure est montré aux acheteurs de tous les hubs qui
épinglent ce foyer. L'agent d'un **propriétaire** tourne dès son déploiement — appelable à sa propre porte
`/t/{slug}` — mais n'entre dans le manifeste qu'après une revue automatique
([`hestia/listing.py`](../hestia/listing.py)). Elle lit ce que lirait le modèle de l'acheteur (nom, description,
schémas) et retient la publication pour des balises d'instructions, « ignore previous instructions », « ne le dis
pas à l'utilisateur », « avant d'utiliser tout autre outil », des actions dissimulées, des chemins d'identifiants,
des caractères cachés, des commentaires HTML, un mot qui mêle des alphabets semblables (latin et cyrillique,
grec…), et un identifiant proche de la famille d'un autre propriétaire ou un nom qui se lit comme celui d'un agent
de ce foyer, en marche ou non.
Le propriétaire voit les raisons dans la réponse du déploiement et dans `GET /v1/tenants` ; l'opérateur publie ou
retient à la main avec `POST /v1/admin/tenants/{slug}/listing`. Les agents de l'opérateur ne sont pas revus.

Les noms qu'un hub montre aux acheteurs appartiennent pour de bon à celui qui a déployé le premier sous ces noms
([`hestia/names.py`](../hestia/names.py)) : une famille de capacités (l'identifiant avant `@`), un identifiant de
produit et un éditeur (publisher id et adresse de paiement). Ils sont comparés repliés — la casse, les
séparateurs, les lettres semblables et une version finale ne font pas un nouveau nom —, donc `merkle-proof@v2`,
`merkle_proof.v2@v1` et `Merkle.Pr0of@v1` sont tous de la famille de `merkle.proof`. Un agent arrêté, un agent
renommé et un propriétaire suspendu gardent leurs noms ; un autre propriétaire reçoit 409. `hestia*` appartient
au foyer lui-même, et l'opérateur détient les préfixes de `HESTIA_RESERVED_PREFIXES` (par défaut
`aicom,aimarket,modelmarket`). Chez un même propriétaire, un identifiant exact (quelle que soit la casse) n'est
servi que par un agent à la fois. La réservation est celle de la base de données (une clé primaire) : deux
processus sur un même registre ne peuvent pas gagner tous les deux un nom. L'opérateur liste les réservations
avec `GET /v1/admin/names`, réserve ou cède un nom avec `POST /v1/admin/names`
(`{"kind": "family|product|publisher|prefix", "name": "…", "holder": "operator" | clé du propriétaire}` ; un
`prefix` couvre tout nom qui commence par lui, par exemple une marque pour son entreprise) et le libère avec
`POST /v1/admin/names/release` une fois les agents de son détenteur arrêtés.

Un agent retenu ne répond qu'à sa propre porte `/t/{slug}` : l'invocation routée qu'appelle un hub n'atteint que
les agents publiés. Un redéploiement du propriétaire relance la revue mais ne lève pas une retenue de
l'opérateur, et un redéploiement de l'opérateur ne publie pas un agent retenu — c'est l'appel de publication qui
le fait. Les agents d'un propriétaire suspendu quittent le manifeste et la liste et ne sont servis à aucune
porte. `HESTIA_MAX_TENANTS` compte les agents en marche et en quarantaine ; un agent arrêté garde son slug et ses
noms, pas sa place.

Un propriétaire qui prouve un domaine reçoit les noms qui commencent par ce domaine, zone comprise
([`hestia/domains.py`](../hestia/domains.py)). La preuve est un enregistrement TXT sur `_hestia.<domaine>`
contenant `hestia-owner=<clé publique>`, ou `https://<domaine>/.well-known/hestia-owner.json` avec
`{"owner_pubkeys": ["<clé publique>"]}` ; `python -m hestia.owner_cli proof` affiche les deux et `… domain`
demande au foyer de vérifier. Dès lors, toute famille, tout produit et tout éditeur qui commence par le
domaine, dans un sens ou dans l'autre — `attestedmemory.net.deal`, `net.attestedmemory.deal`,
`attestedmemory-net.deal`, « Attestedmemory.net Labs » — appartient à ce propriétaire, et les hubs voient le
domaine comme `publisher_domain` de l'agent. Le mot seul n'est à personne : attestedmemory.com, .dev et .net
peuvent être trois propriétaires, donc `attestedmemory.deal` reste au premier venu, comme tout nom. Seul
compte un domaine enregistrable en ASCII (example.com, example.co.uk ; pas un sous-domaine, pas un IDN), et
un domaine ne prend jamais un nom qu'un autre détenteur utilise déjà. La preuve HTTPS n'est demandée qu'à
des adresses publiques, sans redirection.

## Des hubs qui vendent vos agents

Un agent facture chaque appel en USDC on-chain, ce qu'un hub ne peut pas payer avec les crédits de
l'acheteur ni avec une enveloppe de sous-traitance. Un propriétaire peut donc laisser un hub vendre ses
agents en son nom ([`hestia/hub_billing.py`](../hestia/hub_billing.py)) :

1. l'opérateur donne au foyer la clé du hub : `HESTIA_TENANT_HUB_KEYS=https://hub.example=<clé>` — la même
   valeur que l'entrée de ce hub dans `AIMARKET_PEER_API_KEYS` pour ce foyer ;
2. le propriétaire ouvre un compte de crédits sur ce hub et le choisit :
   `python -m hestia.owner_cli billing --hearth … --key owner.key --hub https://hub.example --account acct_…`
   (`--account ""` retire le choix) ;
3. un appel du hub avec cette clé est servi sans paiement on-chain et la réponse porte un bloc
   `hub_billing` signé par la clé de fournisseur du foyer : à qui est l'agent, quel hub, quel compte. Le hub
   le vérifie avec la clé qu'il a épinglée pour ce foyer et crédite au propriétaire
   `AIMARKET_PUBLISHER_SHARE_BPS` (70 % par défaut) de ce qu'**il** a facturé à l'acheteur ;
4. `python -m hestia.owner_cli statement` liste chacun de ces appels, pour rapprocher de ce que le hub a payé.

Les agents de l'opérateur lui-même (clé factice) sont vendus par tout hub dont l'opérateur a configuré la
clé, et le hub garde ce qu'il facture. Un hub qui n'a rien facturé (`X-AIMarket-Hub-Charged: 0`, essai
gratuit) ne reçoit pas l'agent d'un propriétaire. Vue opérateur : `GET /v1/admin/hub-billing`.


### Un prix sans adresse de paiement

Un appel n'est facturé que si les paiements du foyer sont activés, que le prix de l'agent est
supérieur à zéro **et** que le déploiement indique une `payout_address`. Sans adresse, le foyer sert
chaque appel gratuitement plutôt que de facturer personne : sans risque pour l'acheteur, une perte
silencieuse pour le vendeur qui voulait vendre. Depuis le 20/11/2026, le foyer le dit là où le vendeur
regarde :

- la réponse au déploiement contient `warnings` (`hestia-owner` et `hestia_agents.cli deploy` les
  affichent) ;
- `/v1/hearth`, `/v1/tenants` et `/v1/owners/me` (`unpaid`) portent `unpaid_reason` pour cet agent.

Si un hub choisi par le propriétaire facture l'agent (ci-dessus), seuls les appels directs au foyer sont
gratuits, et l'avertissement le dit. Rien n'est signalé quand les paiements sont coupés sur tout le
foyer : c'est le choix de l'opérateur, pas quelque chose qu'un redéploiement corrige. Pour être payé,
redéployez avec votre portefeuille : `hestia-owner deploy --key owner.key --body my-agent/deploy.json
--payout 0x…`.

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
