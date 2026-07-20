# Roadmap consolidation — produit interne, maintenabilité d'abord

> Focus : technique, maintenabilité, scalabilité. La vente/SaaS est hors sujet
> ici. Déclencheur : afflux de bugs + drift AWS/Azure dans la chaîne FOCUS +
> crainte fondée que le système devienne inbuvable.

## Diagnostic — d'où viennent les bugs et le drift

1. **La vélocité a dépassé la vérification.** ~15 packages, 21 migrations,
   queue+worker, FX, acks, événements — livrés en code, mais toujours **aucun
   run CI vert observé, aucun staging appliqué**. Le chantier 0 de la roadmap
   H2 glisse pendant que les features s'empilent : l'écart « CODE LIVRÉ » vs
   « FAIT » de roadmap-done.md est exactement la fabrique à bugs.
2. **FOCUS est traité comme UN format alors que c'est N dialectes.** AWS et
   Azure exportent tous deux « du FOCUS », mais divergent sur les colonnes
   effectivement remplies, les vocabulaires ServiceName, les régions, la
   devise (migration 0019 en témoigne), la sémantique SubAccount. Sans couche
   dialecte explicite ni export réel épinglé par fournisseur, chaque
   divergence se découvre en bug.
3. **La règle des trois est dépassée partout.** 4 règles EOL quasi identiques
   (rds/mysql/aurora + le pattern), 4 règles stockage cousines, 2 collectors
   qui dupliquent pagination/erreurs/région. Chaque copie est une surface de
   drift (la régression rds_eol venait déjà de là).
4. **Ce qui a marché n'a pas été généralisé.** Les trois classes de bugs
   récurrentes du projet (liste codée en dur qui dérive ; refactor qui perd un
   comportement ; dialecte de source non conforme) ont chacune un antidote
   éprouvé ici (registre+pin ; test d'émission lié au registre ; golden
   dataset). Ils existent par endroits, pas systématiquement.

Verdict : le système n'est pas inbuvable — 15 petits packages à frontières
strictes, c'est une bonne base. Il le deviendra si la duplication et l'écart
code/exécution continuent de croître. D'où : **consolidation avant toute
nouvelle feature.**

---

## Règle d'or pendant la consolidation

**Gel des features** (nouvelles règles, nouveaux connecteurs, nouvelles pages)
tant que le chantier I n'est pas fermé. Exception : correction de bug, avec
test de non-régression obligatoire.

## Chantier I — Barrière qualité *(sem. 1 · stop the bleed)*

| # | Action | AC |
|---|---|---|
| I.1 | **CI verte observée, bloquante** — la dette n°1, encore et toujours : pytest + npm ci/build + migrations + RLS PG + terraform validate, tout rouge = rien ne merge | Badge vert sur main ; plus aucun « ça passe chez moi » |
| I.2 | **Registre des classes de bugs** : chaque bug corrigé est classé (liste-dérivante / refactor-perte / dialecte-source / migration-drift / autre) + test de non-régression | docs/development/bug-classes.md ; escape rate hebdo mesuré |
| I.3 | Pins manquants connus : miroir TS du taux FX (noté dans la roadmap H2), + grep systématique des tables codées en dur sans test-pin | 0 registre non épinglé |
| I.4 | **import-linter en CI** : les frontières d'AGENTS.md (core n'importe rien, packages n'importent pas apps, connecteurs entre eux) appliquées par l'outil, plus seulement par test ad hoc | Le lint échoue sur un import interdit |
| I.5 | mypy strict étendu à `apps/api` (aujourd'hui core seul) | CI type-check l'orchestrateur |

## Chantier II — Dialectes FOCUS par fournisseur *(sem. 2-3 · le fix du drift)*

| # | Action | AC |
|---|---|---|
| II.1 | **Un module dialecte par provider** (`focus/dialects/aws.py`, `azure.py`) : mapping colonnes, services, régions, devise, SubAccount → un seul `FocusCharge` canonique. Plus un seul `if azure` en aval | Les règles et l'agrégateur ne connaissent plus le provider |
| II.2 | **Golden exports réels par provider**, anonymisés et versionnés (`tests/golden/focus_aws.csv`, `focus_azure.csv`) + harnais de conformité identique paramétré par dialecte | La classe « conforme à notre CSV maison » fermée pour les DEUX providers |
| II.3 | Table de correspondance service-name cross-provider **en données versionnées** (pas en code) | Ajouter un service = une ligne de données |
| II.4 | **Grep-pin CI** : `aws`/`azure` interdits dans `packages/insights/*` (les règles ne voient que le canonique) | Test rouge si un provider fuit dans une règle |

## Chantier III — Factorisation par la règle des trois *(sem. 3-5 · APRÈS les golden)*

| # | Action | AC |
|---|---|---|
| III.1 | **Règle EOL générique** : une implémentation paramétrée (matcher moteur, catalog, clés payload) ; rds/mysql/aurora deviennent ~20 lignes de config chacune. Une fonction partagée, PAS un DSL | 1 seul endroit où vit l'arithmétique vCPU×tarif×730 ; les 3 suites de tests existantes passent inchangées |
| III.2 | Helpers communs des règles stockage (pricing volume par région, fenêtres d'âge) | Plus de copie du calcul Go×tarif |
| III.3 | **Lib collector commune** sous le Protocol ADR-14 : pagination, `_region`, classification d'erreurs, breaker — aws_rds et aws_ec2 la consomment | Un 3e connecteur inventaire = collect+factories uniquement |
| III.4 | KPI de sortie : **ajouter une règle = ≤ 3 fichiers touchés** (config, catalog, test) ; un connecteur = 1 package, 0 modif du cœur | Mesuré sur la prochaine règle réelle |

Gate : III ne démarre qu'avec II.2 fait (on ne refactore jamais sans golden).

## Chantier IV — Invariants property-based *(sem. 3-4, parallèle)*

| # | Action | AC |
|---|---|---|
| IV.1 | Hypothesis sur les invariants du cœur : identité ressource (jamais 2 actives même clé naturelle), retirement (jamais sans 2 runs complets), extraction monétaire (∀ payload arbitraire : jamais d'exception, bool jamais monétisé), idempotence worker (rejouer un job = état identique) | 4 suites de propriétés en CI |
| IV.2 | Test de chaîne migrations : baseline squashée appliquée sur PG vierge ≡ ORM (diff vide), + upgrade depuis un dump pré-squash | Le squash ne peut pas avoir perdu une colonne |

## Chantier V — Scalabilité par la mesure *(continu)*

- Le bench e2e quotidien devient une **série temporelle** (durée, RAM, coût par
  run) avec alerte de régression >20 % — c'est lui qui déclenchera
  observations→S3/Parquet, read model UI, partitionnement. Pas avant.
- Rétention/purge exécutées en réel sur staging (pas seulement testées).

---

## Gouvernance anti-inbuvable (permanente)

1. **Règle des trois** : la 3e copie d'un pattern déclenche la factorisation
   avant toute 4e occurrence. C'est un critère de review, pas un vœu.
2. **Tout registre naît avec son pin** : une table de correspondance sans test
   de complétude/miroir ne passe pas la review.
3. **FAIT ≠ CODE LIVRÉ** partout (généralisation de roadmap-done.md) : un item
   se ferme exécuté et daté.
4. **KPIs maintenabilité suivis mensuellement** : fichiers touchés par ajout de
   règle (≤3) ; bug escape rate ; part des bugs dans une classe connue (si une
   classe re-frappe 2×, son antidote systémique manque) ; durée de la CI (<10 min).
5. **Budget bugs** : si l'escape rate hebdo dépasse le seuil convenu 2 semaines
   de suite, gel des features automatique — la règle s'applique sans débat.

## Séquence

```
Sem 1     Chantier I (gel features)     → la CI arbitre, les pins complets
Sem 2-3   Chantier II                   → drift AWS/Azure structurellement clos
Sem 3-5   Chantier III (gate: golden)   → duplication résorbée, KPI ≤3 fichiers
Sem 3-4   Chantier IV (parallèle)       → invariants sous Hypothesis
Continu   Chantier V                    → scalabilité pilotée par le bench
```

À la sortie : ajouter le 3e cloud (Azure Resource Graph en inventaire) coûtera
un package de dialecte + un package connecteur — et rien d'autre. C'est le
test final de cette consolidation.
