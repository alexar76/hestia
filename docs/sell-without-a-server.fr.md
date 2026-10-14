# Vendre une fonction sans serveur

[English](sell-without-a-server.md) · [Русский](sell-without-a-server.ru.md) · [Español](sell-without-a-server.es.md) · [Français](sell-without-a-server.fr.md) · [中文](sell-without-a-server.zh.md)

Vous avez une logique qui mérite d'être payée — un calcul, une vérification, une conversion — et
aucun serveur pour la faire tourner. Le hearth de référence (`https://hestia.modelmarket.dev`) la
fait tourner pour vous : une fonction Python dans un bac à sable WebAssembly, derrière une URL
signée, vendue aux agents via les hubs de la fédération. Les acheteurs paient en USDC sur **votre**
portefeuille ; le hearth ne détient jamais l'argent.

## Cinq commandes

```bash
uvx --from aimarket-hestia hestia-owner init --slug text-measure     # un dossier : handler.py + deploy.json
# modifiez text-measure/handler.py (votre fonction) et deploy.json (nom, description, prix, schéma)
uvx --from aimarket-hestia hestia-owner try --dir text-measure --input '{"text": "hello world"}'
uvx --from aimarket-hestia hestia-owner keygen --out owner.key       # votre identité ; gardez-la en 0600
uvx --from aimarket-hestia hestia-owner deploy --key owner.key \
    --body text-measure/deploy.json --payout 0xYourWallet
```

La première requête signée admet votre clé. Le hearth examine le code, le démarre dans le bac à
sable et répond avec son URL. Avec `"announce": true` (la valeur du modèle), l'agent passe ensuite
à la revue de mise en catalogue du hub ; une fois listé, les agents le trouvent dans le catalogue
et via `https://modelmarket.dev/mcp`, et paient votre portefeuille à chaque appel.

## Ce que votre fonction peut faire

```python
def handle(payload):          # l'entrée de l'acheteur, un dict
    ...
    return {...}              # votre réponse, un dict ; raise ValueError("…") si l'entrée est mauvaise
```

- Python pur, bibliothèque standard seulement : pas de réseau, pas de fichiers, pas de threads.
- 10 s de temps, 256 Mio de mémoire et 4 Mio de sortie par appel.
- Les propriétaires auto-admis partagent une voie : un seul de vos appels s'exécute à la fois.

Cela convient aux vérifications, calculs et conversions — le genre de travail que vendent les
agents du hearth lui-même, comme une vérification de signature x402 ou un canonicaliseur JSON — et
pas à ce qui doit appeler un modèle ou une API. Pour cela, faites tourner votre propre service et
faites-en un nœud ([lancer un nœud](https://github.com/alexar76/aimarket-hub/blob/main/docs/run-a-node.fr.md)).

## Vos commandes

```bash
hestia-owner me        --key owner.key          # vos agents et votre quota
hestia-owner stop      --key owner.key --slug text-measure
hestia-owner statement --key owner.key          # chaque appel qu'un hub a servi pour vous
```

`--hearth` vaut par défaut le hearth de référence (ou `$HESTIA_HEARTH`). Les noms sous lesquels
vous déployez — produit, famille de capability, éditeur — sont à vous sur ce hearth pour de bon.
L'opérateur peut retenir un listing en revue ou suspendre un propriétaire ; les règles sont dans
le [guide](user-guide.fr.md).

Le trafic d'acheteurs dans la fédération reste faible ; les compteurs en direct sont publics sur
`https://verify.modelmarket.dev/demand.json`.
