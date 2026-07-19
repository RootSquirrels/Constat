# Registre des classes de bugs (chantier I.2)

> Chaque bug qui a mordu ce projet appartient à une **classe**. Une classe
> n'est fermée que par un **test de non-régression** (ou un garde CI) qui la
> rend structurellement impossible — pas par la correction du cas particulier.
> Ce registre est la liste de contrôle de toute revue : un nouveau bug qui
> n'entre dans aucune classe existante crée une nouvelle section.

## Classe 1 — Liste dérivante (hardcoded list drift)

Une liste codée en dur diverge de la source de vérité (registre, schéma,
settings). Le pire défaut du projet : au moins 6 occurrences.

| Instance | Conséquence | Garde en place |
|---|---|---|
| ecs.tf codait 2 règles en dur → 4/6 jamais exécutées | 4 règles muettes en planification | `test_run_insights_cli.py` (épingle ecs.tf + `--all`) |
| `ebs_unattached` absent du registre MONETARY | montants perdus en restitution | `test_monetary_extraction.py` (complétude RUNNERS↔MONETARY) |
| 4 tables sans policy RLS (F-04) | fuite cross-tenant latente | `test_rls.py` (Postgres, `RLS_TABLES` exact-set) |
| Registre de facts limité à `aws.rds.*` | garantie « CI fails on drift » creuse | `test_fact_definitions.py` (producer/consumer cross-check) |
| `.env.example` ≠ vars lues (F-14, puis 7 vars collect du chantier 1) | opérateur configure dans le vide | `test_contract_pins.py::test_env_example_matches_env_vars_read_by_api` |
| Chargeback : devise absente des clés d'agrégation | EUR+USD sommés, étiquetés `_usd` | `test_azure_focus.py` (jamais de somme mixte) |

**Règle : toute nouvelle liste codée en dur naît avec son test-pin, ou elle
n'est pas mergée.**

## Classe 2 — Refactor-perte (refactor drops behavior)

Un refactor préserve la forme mais perd le comportement. Invisible en
lecture, seul un test reliant l'entrée à la sortie le voit.

| Instance | Conséquence | Garde en place |
|---|---|---|
| Refactor des tiers : vCPU gaté mais jamais consommé — le montant mensuel disparaît de `rds_eol` | l'insight phare sans chiffre | `test_monetary_extraction.py` (émission ↔ extraction liées) |
| F-01 : succès « par défaut » quand une exception non-ClientError traverse | run success → retirement de ressources vivantes | `test_collector_aws_audit_fixes.py` (succès prouvé, jamais par défaut) |
| Delete-and-replace qui efface l'ack opérateur (empreinte hachant le titre, variable chaque jour) | décisions perdues chaque nuit | ADR-16 + `test_ack_carryover.py` (identité d'écart stable) |

**Règle : un refactor qui touche un chemin de valeur (argent, suppression,
décision opérateur) exige un test entrée→sortie avant/après identiques.**

## Classe 3 — Dialecte-source (source format drift)

Le format de la source externe bouge (spec, API, SDK) et notre parseur reste
au vieux dialecte. Indétectable avec des fixtures maison.

| Instance | Conséquence | Garde en place |
|---|---|---|
| FOCUS 0.5→1.0 : `AmortizedCost`→`EffectiveCost` (attrapé), `Region`→`RegionId`/`RegionName` (raté) | export conforme rejeté | `tests/golden/` : datasets spec-shaped AWS **et** Azure, suite `test_focus_golden.py` / `test_azure_focus.py` |
| psycopg 3 : `cursor(factory=)` n'existe pas | 31 tests Postgres en erreur au setup | exécuté en CI à chaque push (job postgres-rls) |
| boto3 : `BotoCoreError` n'hérite pas de `ClientError` | cf. F-01 ci-dessus | classification d'erreurs + test `ReadTimeoutError` |

**Règle : chaque source externe a un golden dataset façonné sur la spec
officielle, pas sur notre mémoire.**

## Classe 4 — Migration-drift (schema/ORM/chain drift)

Le schéma réel, l'ORM et la chaîne de migrations divergent.

| Instance | Conséquence | Garde en place |
|---|---|---|
| 0004 omet `insight_runs.tenant_id` ; 0007 le référence | chaîne cassée sur base vierge | job CI « Apply migrations » (Alembic) à chaque push |
| `accounts.external_id` UNIQUE global (F-12) | cas MSP impossible | `test_rls.py` (forme de la contrainte épinglée) |
| Adoption Alembic casse les fixtures (glob `db/migrations/*.sql` vide) | 31 tests Postgres en erreur | fixtures passent par la chaîne Alembic elle-même |
| ORM ≠ migrations (contraintes, colonnes) | sqlite valide ce que Postgres refuse | ADR-17 : Alembic + `compare_type=True` ; known-issues tient la liste résiduelle |

**Règle : le schéma se change par migration, jamais à la main ; la chaîne
complète s'applique en CI sur base vierge à chaque push.**

## Classe 5 — Frontière non gardée (boundary leak)

Un invariant architectural repose sur la convention, pas sur un mécanisme.

| Instance | Conséquence | Garde en place |
|---|---|---|
| Race outbox : envoi SQS avant le commit du job | work items orphelins | commit-d'abord + `enqueue_error` + drop d'orphelins (worker) |
| `POST /insights` sans commit (flush seul) | écriture perdue sous Postgres, invisible sous sqlite | test e2e tenant write-leg (CI Postgres) |
| Tenant libre côté client | cross-tenant par header | `X-Tenant-ID` → 400 + e2e 2-tenants CI |
| « Append-only » par convention | UPDATE/DELETE possibles sur le journal | trigger 0014 (immutabilité technique) |

**Règle : un invariant de sécurité/données qui n'est pas appliqué par un
mécanisme (contrainte, trigger, middleware, test) n'existe pas.**

## Escape rate — mesure

Définition : un bug « échappé » est découvert **après** le merge qui l'a
introduit (en CI post-push, en revue externe, ou en usage), par opposition
aux bugs attrapés avant merge (test local, pin au moment de l'écriture).

Méthode (hebdo, à partir du 2026-07-19) :
1. Compter les corrections de la semaine dont le commit message contient
   `fix`/`SRE`/`review` ou qui ajoutent un test de non-régression listé
   ci-dessus → ce sont les échappés.
2. Escape rate = échappés / bugs totaux de la semaine. Cible : tendance
   décroissante ; toute semaine > 50 % déclenche une revue des gardes.

Historique de référence : avant le chantier I, quasiment tous les bugs du
projet étaient des échappés (audit statique, comité client, première
exécution CI — 14/14 runs rouges avant I.1).

## Ajouter une entrée

Bug découvert → (1) reproduire par un test qui échoue, (2) corriger,
(3) classer ici avec sa garde, (4) si nouvelle classe, la nommer et écrire
sa règle. Sans le (3), le bug n'est pas fermé — il est reporté.
