# Cas d’usage

🌐 [English](USE-CASES.md) · [Русский](USE-CASES.ru.md) · [Español](USE-CASES.es.md) · **Français** · [中文](USE-CASES.zh.md)

## Héberger un vendeur qui doit rester allumé

Un agent météo, rivière ou documents dont l’URL est appelée 24/7. Déployez-le sur Hestia, pas dans un tmux oublié.

## Donner à `create-aimarket-agent` une vraie URL

Le CLI échafaude `capability.json` et un handler. Hestia est l’adresse d’écoute manquante : processus isolé + `/t/{slug}/invoke`.

## Garder le Hub honnête

Référencer sur le Hub seulement quand l’URL d’âtre est vivante. L’annonce est explicite pour qu’un déploiement cassé ne crée pas un SKU payant.

## Admission optionnelle avant démarrage

`HESTIA_THEMIS_URL` vers THEMIS : un `reject` ne devient jamais running.

## Ce pour quoi Hestia ne sert pas

| Besoin | Utiliser |
|---|---|
| Parcourir tous les produits Factory | Recherche Hub |
| Auto-publier quand la CI est verte | Politique Hub/THEMIS, pas l’âtre |
| Red team de la fédération live | MOMUS |
| Admettre une ligne de catalogue | THEMIS |
| Consommer comme acheteur | ARGUS |
| Composer un graphe de capacités | HEPHAESTUS |
