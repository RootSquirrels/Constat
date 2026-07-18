# Constat — Monter le scoreboard & prochaines fonctionnalités

> Point de départ : re-audit 2026-07-18, verdict GO pilote. Règle du jeu inchangée :
> chaque action a un critère d'acceptation testable, rien ne se construit avant son seuil.

---

## 1. Monter le scoreboard

| Domaine | Actuel | Cible | Actions concrètes (critère d'acceptation) |
|---|---:|---:|---|
| Sécurité | 4 | 4.5 | Rôle PG runtime **non-owner** sans BYPASSRLS (test CI : ALTER POLICY refusé au rôle API) · rotation clé API documentée · `pip-audit`/`npm audit` en CI |
| Isolation tenant | 4.5 | 5 | Tenant #2 réel de bout en bout : GUC depuis la requête (header/JWT) au lieu du défaut, test e2e 2 tenants via l'API — pas seulement via psql |
| Collecte & résilience | 4 | 4.5 | Backoff jitteré explicite sur throttling (boto3 `adaptive` mode + test) · re-scan ciblé d'une seule région échouée sans re-scanner le reste |
| Tests | 4 | 4.5 | Golden dataset FOCUS issu d'un vrai export AWS Data Exports anonymisé · un test de concurrence (2 collectes même scope) · e2e web smoke (Playwright, 1 parcours) |
| Exploitabilité | 3 | 4 | IaC minimal (Terraform : ECS+RDS+secrets) · backup/restore **exécuté** une fois et runbook daté · 3 alertes branchées : run failed, scope_stale ↑, erreur 5xx ↑ |
| Frontend & API | 3.5 | 4 | Vue « Restitution POC » imprimable/PDF (la page qu'on laisse au prospect) · états loading/error systématiques · export CSV par insight |
| Scalabilité | 3.5 | 4 | Bench documenté à 10 k ressources (durée runner, RAM) — pas d'optimisation avant la mesure |
| Adéquation produit | 4.5 | 5 | Se gagne en POC, pas en code : 3 restitutions réelles où écarts € > prix annuel |

**Ordre de bataille** (effort S/M/L) :

1. **[S]** Rôle non-owner + test CI — dernier écart vs doc §11.2, une matinée.
2. **[S]** 3 alertes + backup testé — le minimum vital avant un client payant.
3. **[M]** Golden dataset FOCUS réel — la leçon AmortizedCost ne doit pas pouvoir se reproduire.
4. **[M]** Terraform minimal — reproductibilité de l'env pilote.
5. **[M]** Vue Restitution POC — c'est un artefact commercial autant que produit.

Ce qui ne monte PAS le scoreboard : ajouter des technos (queue, S3/Parquet, multi-région).
Leurs seuils sont écrits (>5 comptes/60 s de scan, >10 M observations) ; avant, c'est du poids mort.

---

## 2. Fonctionnalités — vague 1 : insights describe-API (avant les POC)

Pattern industrialisé (catalog + règle + tests + INCONCLUSIVE). Aucune dépendance nouvelle,
mêmes describe API que le collecteur existant. **Chaque insight = une ligne en € dans la démo.**

| Insight | Source | Estimation € | Effort |
|---|---|---|---|
| `mysql.extended_support` | DescribeDBInstances (déjà collecté) + catalog MySQL 5.7/8.0 | vCPU × tarif tier (année 3 depuis 03/2026 : ×2) | ~2 j — **le plus gros gisement du marché** |
| `aurora.extended_support` | idem, engines aurora-postgresql/aurora-mysql | idem, grille Aurora | ~1 j (mutualise le précédent) |
| `ebs.unattached` | DescribeVolumes `status=available` | Go provisionnés × tarif type (gp2/gp3/io1) × région | ~1 j |
| `snapshot.orphan` | DescribeSnapshots (owned) × volumes × AMI | Go × 0,05 $/mois env. (catalog par région) | ~1,5 j |
| `ec2.stopped_with_storage` | DescribeInstances `stopped` + volumes attachés | somme des volumes + IP élastiques associées | ~1 j |
| `ebs.gp2_to_gp3` | DescribeVolumes `type=gp2` | ~20 % du coût volume | ~0,5 j — le quick win le plus facile à expliquer |

Règles transverses : chaque nouveau type collecté crée son `source_run` par scope (la preuve
d'absence reste prouvable) ; chaque tarif entre au catalog avec source URL + date de revue ;
`value_basis=ESTIMATED` tant que le rapprochement FOCUS n'a pas confirmé.

**Cible de démo : 6-7 lignes chiffrées au lieu d'une.** C'est la vague qui se code MAINTENANT.

## 3. Vague 2 : utilisation réelle (après 2-3 POC)

- **Connecteur AWS Compute Optimizer** (gratuit, opt-in client, pas de support plan) :
  namespace `aws.computeoptimizer.*` → insights `ec2.overprovisioned`, `ebs.underutilized`,
  `rds.rightsizing`. C'est le complément d'utilisation que Trusted Advisor ne donne qu'avec
  Business Support.
- **Rapprochement FOCUS→ressource activé en produit** : passer les estimations en
  `value_basis=ACTUAL` quand la ligne FOCUS confirme (le schéma le permet déjà —
  ResourceId est chargé).
- **Digest hebdo** : email/export « nouveaux écarts cette semaine, écarts résolus, € cumulés » —
  transforme le POC en usage récurrent, prépare le renouvellement.

## 4. Vague 3 : connecteurs de corroboration (V2, sur demande client)

- **Trusted Advisor** : `aws.trustedadvisor.*`, activable seulement si le tenant a Business
  Support. Rôle : corroboration (deux sources = preuve renforcée) + couverture (service limits).
  Jamais un prérequis, jamais une source d'identité. ADR d'une ligne.
- **SSM** (déjà dans la cible archi) : `aws.ssm.managed`, agent version → insights de trous de
  couverture patching. Premier pas hors FinOps, vers l'angle « assurance ».
- **Inventaire filtrable** (`/resources`, `aws.tag.*`, vue web) : gated par ADR-12 —
  se construit quand un client pilote le demande, sur le schéma existant, sans migration.

## 5. Fonctionnalités produit (hors insights)

| Feature | Pourquoi | Quand |
|---|---|---|
| Scans planifiés (scheduler quotidien) | Aujourd'hui la collecte est déclenchée à la main ; la fraîcheur 24 h devient auto-entretenue | Avant le 1er pilote payant |
| Vue Restitution POC exportable | L'artefact que le champion interne fait circuler en interne | Avant le 2e POC |
| Multi-comptes par lot (import CSV/Organizations) | Onboarder 40 comptes sans 40 formulaires ; `ListAccounts` via le rôle management | Quand un prospect >10 comptes |
| Historique des écarts (insight apparu/résolu, delta €) | La courbe « euros récupérés » = le renouvellement | 90 j |
| API publique read-only documentée (OpenAPI publiée + service accounts, ADR-10) | Exigence d'achat fréquente ; le schéma auth existe | 1er client qui le demande |

---

## Le fil rouge

Le scoreboard monte par la **preuve** (tests, CI, runbooks datés), jamais par la techno.
Les features montent la **valeur par démo** (lignes en €), jamais la surface.
Tout le reste a un seuil écrit — et tant que le seuil n'est pas atteint, la réponse est non.

---

## Annexe — Statut d'exécution (2026-07-18)

Vérification locale : **356 tests passés, 24 skippés** (Postgres RLS — CI fait foi,
pas de Docker sur la machine), `ruff check`/`format` propres, `mypy` core propre,
`npm run build` propre (route `/restitution` incluse).

### Scoreboard

| Action | Statut | Preuve / limite |
|---|---|---|
| Rôle PG runtime non-owner | **FAIT** | Migration `0012_runtime_role.sql` (`constat_app`, sans BYPASSRLS, DML only) + tests CI `TestRuntimeRole` (ALTER POLICY / CREATE TABLE refusés, RLS contraignante). Reste : faire tourner le pilote sous ce rôle (voir `docs/operations/deployment.md`). |
| 3 alertes | **FAIT** | `deploy/prometheus/alerts.yml` (source_run failed, scope_stale ↑, 5xx ↑) + `docs/operations/alerting.md`. Métriques existantes réutilisées, aucune nouvelle. |
| Backup/restore + runbook daté | **RUNBOOK FAIT, exécution EN ATTENTE** | `docs/operations/backup-restore.md`. Pas de Docker sur la machine de dev : l'exécution réelle est planifiée au déploiement pilote — le scoreboard ne doit PAS la compter avant. |
| Rotation clé API documentée | **FAIT** | `docs/operations/api-key-rotation.md` (contrainte honnête : pas de zéro-downtime avec une clé lue au démarrage). |
| pip-audit / npm audit en CI | **FAIT** | Steps advisory (`continue-on-error`) dans les deux jobs — à passer en bloquant après tri de la baseline. |
| Backoff jitteré throttling | **FAIT** | Mode `adaptive` boto3 (10 tentatives) sur RDS + STS, testé par inspection de config. |
| Re-scan ciblé d'une région | **DÉJÀ POSSIBLE, prouvé** | `TargetAccount.regions` le permettait ; test dédié ajouté (re-scan eu-west-1 sans toucher us-east-1). |
| Test de concurrence 2 collectes | **DÉJÀ COUVERT** | `test_source_runs.py` (index partiel `status='running'`) — vérifié, pas de doublon ajouté. |
| Bench 10 k ressources | **FAIT, mesuré** | `scripts/bench_runner.py` → **10k ressources en ~31-36 s, 162 MiB peak** (sqlite in-memory, Ryzen 5 3600). Linéaire ; 50 k ≈ 3 min. Conclusion documentée dans `docs/operations/benchmarks.md` : aucune optimisation justifiée, prochain point d'action = run Postgres à 50 k. |
| Golden dataset FOCUS | **HARNAIS FAIT, vraie donnée EN ATTENTE** | `tests/fixtures/focus_golden_v1_0.csv` (43 colonnes officielles vérifiées contre le spec) + 6 tests. **Le harnais a immédiatement payé** : bug réel du loader (`Region` requis, renommé `RegionId`/`RegionName` en 1.0) trouvé et **corrigé** dans la foulée. Remplacement par un export AWS réel anonymisé : attend le 1er prospect. |
| Terraform minimal | **ÉCRIT, non appliqué** | `infra/` (RDS + ECS Fargate + Secrets Manager + EventBridge quotidien) + `Dockerfile`. Aucun binaire terraform/docker/AWS sur la machine : label « unapplied/unvalidated » partout, fix-up attendu au premier `terraform validate`. |
| Scans planifiés (quotidien) | **FAIT (infra)** | EventBridge Scheduler `cron(0 5 * * ? *)` → RunTask (collect + rds_eol + chargeback). Cadence calée sur la fenêtre de fraîcheur 24 h. |
| Vue Restitution POC | **FAIT** | `/restitution` imprimable (CSS print), tableau €/mois + base ESTIMATED/ACTUAL, section « What we don't know » (INCONCLUSIVE), résumé chargeback, provenance. |
| États loading/error | **FAIT** | `loading.tsx`/`error.tsx` sur les 4 pages de données. |
| Export CSV par insight | **FAIT** | `GET /insights/export.csv` (mêmes filtres que la liste, cap 500) + bouton sur la page insights. |
| Tenant #2 e2e (GUC header) | **REFUSÉ pour l'instant** | Seuil non atteint : aucun tenant #2. AGENTS.md acte le mono-tenant V1 ; la règle du jeu (« rien avant son seuil ») s'applique à cette action comme aux autres. |
| e2e web Playwright | **FAIT (1 test, seuil global non atteint)** | `apps/web/tests/e2e/restitution.spec.ts` (commit `bffa653`) — smoke test mocké sur `/restitution` via `page.route()`, pas d'API réelle nécessaire. Ajouté sur **demande explicite** du pitch commercial, **pas** par atteinte du seuil. Le seuil initial (« surface web > 1 page ou client payant ») reste pertinent pour la suite complète : les 4 autres pages (`/status`, `/accounts`, `/insight-runs`, `/chargeback`) n'ont pas de test e2e et n'en auront pas tant que la surface n'augmente pas ou qu'un client ne le demande. |
| 3 restitutions réelles (Adéquation 5) | **HORS CODE** | Activité POC, pas du code. |

### Vague 1 (insights)

| Insight | Statut | Note |
|---|---|---|
| `mysql.extended_support` | **FAIT** | `packages/insights/mysql_eol` + catalog MySQL 5.7/8.0 (dates et tarifs sourcés AWS, revus 2026-07-18). ~2× le gisement PG selon le marché. |
| `aurora.extended_support` | **FAIT** | `packages/insights/aurora_eol` (aurora-mysql 2/3 + aurora-postgresql 11-15). Découverte catalog : pas de tier année-3 pour Aurora MySQL (contrairement à l'hypothèse du tableau) — sourcé. |
| `ebs.gp2_to_gp3` | **FAIT** | `packages/insights/ebs_gp2_to_gp3` + catalog EBS — le connecteur EC2/EBS a été construit (scopes source_run propres, preuve d'absence préservée). Montant : `savings_monthly_usd` (registre ADR-13). |
| `ebs.unattached` | **FAIT** | `packages/insights/ebs_unattached` — volumes `available` × tarif catalog (`monthly_waste_usd`). Type de volume absent du catalog ⇒ INCONCLUSIVE `catalog.volume_type_price_missing`, jamais d'insight « gratuit ». |
| `snapshot.orphan`, `ec2.stopped_with_storage` | **PROCHAIN CHANTIER** | Même connecteur EBS/EC2 désormais en place ; restent DescribeSnapshots (croisement volumes × AMI) et DescribeInstances `stopped` + IP élastiques, plus leurs entrées catalog. |

Le runner a été généralisé au passage (`RESOURCE_RULES` + `run_resource_rule`) :
ajouter le prochain insight basé ressource = un package resolver + une ligne de registre.

### Corrections de consolidation (cette passe)

- **Loader FOCUS** : accepte `RegionId` (spec 1.0) avec fallback `Region` (pré-1.0) — bug trouvé par le golden dataset.
- **Clé payload coût** : `extended_support_monthly_usd` partout (l'export CSV et le web
  lisaient une clé qui n'existait dans aucun resolver).
- `.gitignore` : tfvars/tfstate exclus (l'exemple reste tracké).
- `known-issues.md` §2 : promesse §11.2 désormais tenue (0012), reste le runtime à basculer.

### Comité d'évaluation client (2026-07-18, post-vague 1)

- **Extraction des montants** : registre unique `constat_core.monetary` (ADR-13) —
  chaque règle déclare sa clé de payload, sa base (ESTIMATED/ACTUAL) et sa nature
  (`AVOIDABLE_SAVING` vs `ACCOUNTING_DELTA`, jamais agrégées ensemble). Test de
  complétude : une règle dans RUNNERS sans décision monétaire casse la CI (la
  dérive `ebs_unattached` a été attrapée exactement ainsi). Miroir TS épinglé par test.
- **SLA pilote borné** : `docs/pilot/sla-pilote.md` (projet, relecture juridique
  requise) — 5 comptes max, 90 jours, eu-west-3 + TLS, fraîcheur 24 h contractualisée,
  réversibilité complète. Vérifié ligne à ligne contre le système réel (règles actives,
  ack, template IAM, rétention, ALB+TLS).

### Rappel des refus actifs (seuils écrits, inchangés)

Queue + workers (>5 comptes ou scan >60 s) · S3/Parquet (>10 M observations) ·
multi-tenant effectif (tenant #2) · Trusted Advisor / Compute Optimizer / SSM
(vagues 2-3, sur demande client ou après 2-3 POC) · inventaire filtrable (ADR-12,
sur demande pilote).
