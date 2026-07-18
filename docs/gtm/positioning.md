# Go-to-Market — Cloud Assurance Platform

> The customer-facing positioning. This is the document you copy into
> a deck, a one-pager, or a discovery call. It is intentionally
> non-technical at the top. The technical depth lives in the
> per-feature specs (`docs/insights/`, `docs/architecture.md`).

## Message central

> **« En deux heures de connexion, on vous prouve ce que vous ne savez pas sur votre parc cloud — et ce que ça vous coûte. »**

On ne vend pas un inventaire. On vend l'écart chiffré que produit le
croisement de sources qu'aucun outil ne croise : inventaire × cycle
de vie × coût × couverture opérationnelle.

**Exemple phare :** « Vous avez 3 RDS PostgreSQL 11 en année 3
d'Extended Support. Vous payez 0,20 $/vCPU-heure de surcoût de
licence — environ 580 $/mois par db.m5.xlarge — que vos Reserved
Instances ne couvrent pas. Un upgrade de version arrête cette
facture. » Trusted Advisor signale les volumes orphelins ; personne
ne dit ça.

## ICP (client idéal)

| Critère | Cible |
|---|---|
| Taille | ETI / mid-market européen, 200–5 000 salariés |
| Parc | 5 à 150 comptes AWS (puis Azure), 1 k–100 k ressources |
| Équipe | 2–10 personnes cloud/infra, **pas de FinOps ni d'asset manager dédié** |
| Douleur | Facture qui monte, audits (ISO 27001, DORA, cyber-assurance), CMDB jamais à jour |
| Acheteur | Responsable infra/cloud ou DSI ; sponsor éventuel : DAF (via l'écart chiffré) |

**Anti-ICP assumé :** CAC 40 et grands comptes à équipes dédiées —
ils achètent Axonius/Wiz/ServiceNow et ont les 5 ETP pour les faire
tourner. Nous, non requis : c'est l'argument.

## Promesse produit

Un outil **solide et léger** : onboarding < 2 h, rôle read-only sans
access key, zéro agent, zéro personne dédiée à son administration. La
donnée est opposable : chaque valeur affiche sa source, son heure, et
« inconnu » quand on ne sait pas — jamais un faux « conforme ».

## Différenciation

| Face à | Leur limite | Notre angle |
|---|---|---|
| **Trusted Advisor / outils AWS natifs** | Par compte, périmètre AWS-only, pas de croisement coût × cycle de vie × couverture, restitution brute | Multi-comptes, multi-sources, écarts chiffrés en €, exportable et opposable |
| **Axonius, JupiterOne (CAASM)** | Pricing et complexité grands comptes, orientés sécurité pure, déploiement projet | Time-to-value 2 h, angle coût + opérations + conformité, prix mid-market |
| **Wiz / Orca (CNAPP)** | L'inventaire est un sous-produit de la vulnérabilité ; aucun angle coût/licence/EOL chiffré | On ne concurrence pas leur CNAPP : on est le référentiel d'écarts que leur donnée peut enrichir (connecteur) |
| **Cloudaware / ServiceNow (CMDB)** | Lourds, projet d'intégration, ETP dédiés, coût | « La CMDB qui se remplit toute seule et dit la vérité », sans équipe dédiée |
| **CloudQuery / Steampipe (OSS)** | Donnée brute : à vous de construire requêtes, règles, référentiels EOL, tarifs | Les insights maintenus (référentiels EOL + grilles tarifaires versionnés) sont le produit, pas le SQL |

**Le fossé défendable :** la maintenance des référentiels croisés
(dates EOL par moteur, paliers Extended Support, grilles tarifaires,
règles de rapprochement FOCUS) + la discipline de provenance
(chaque écart prouvé, sourcé, daté — jamais déduit d'un scan
incomplet). C'est ingrat, continu, et personne ne le packagera pour
le mid-market.

## Motion commerciale

1. **POC gratuit 2 h** : connexion read-only → restitution live de la
   vue Insights sur le parc réel du prospect.
2. **Règle de closing :** on ne signe que si le POC révèle des écarts
   annuels > prix annuel de la plateforme. Sinon on part — et ça se
   sait.
3. **Land :** périmètre AWS + FOCUS. **Expand :** connecteurs (EDR,
   ServiceNow, Azure) puis contrôles V2.

**Pricing (principe) :** par compte cloud connecté, dégressif. Pas
de per-seat, pas de % du spend (on n'est pas un outil FinOps), pas
de coût caché d'intégration. Ordre de grandeur cible à valider par
le benchmark coût/run.

## Objections attendues

- *« Trusted Advisor / Cost Explorer me le dit déjà. »* — Compte par
  compte, sans croisement, sans historique opposable, et pas
  l'Extended Support corrélé aux RI. Demandez la liste consolidée à
  votre équipe : chronométrez.
- *« Wiz nous couvre. »* — Sur la vulnérabilité, oui. Montrez-moi
  dans Wiz vos moteurs EOL avec le surcoût licence mensuel en euros.
- *« On peut le faire nous-mêmes avec CloudQuery. »* — Oui, plus
  les référentiels EOL/tarifs à maintenir, les règles de
  rapprochement, l'historisation. C'est 0,5–1 ETP permanent : notre
  prix est en dessous.
- *« Encore un outil… »* — Zéro agent, zéro admin dédié, read-only.
  S'il ne prouve pas sa valeur au POC, il ne s'installe pas.

## Ce qu'on ne prétend pas être (V1)

Pas un CNAPP, pas un outil FinOps de showback, pas une CMDB ITSM,
pas de remédiation automatique. Un référentiel d'écarts prouvés. Le
reste vient (V2/V3) quand la fondation a gagné sa place.

## Risque GTM n°1

Vendre l'inventaire au lieu de l'écart. Toute démo, tout deck,
toute page web commence par la vue Insights et un montant en euros
— jamais par la table filtrable.
