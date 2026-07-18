# Audit V1 — Constat (Cloud Assurance Platform)

Équipe d'audit simulée : Principal Cloud Architect AWS · Staff SWE · Staff Data Architect · Product Security Engineer · SRE.
Date : 2026-07-18. Dépôt : `C:\Users\Drgal\Documents\GitHub\Constat` (lecture seule — aucune modification effectuée).

**Limite matérielle de l'audit** : l'environnement d'exécution sandbox était indisponible
(`HYPERVISOR_VIRT_DISABLED` — virtualisation désactivée sur la machine hôte). **Aucune commande
n'a pu être exécutée** (pytest, ruff, mypy, npm build, docker). L'audit est donc **statique
intégral** : tous les findings s'appuient sur le code lu, fichier et ligne à l'appui. Les
assertions "les tests passent" reprennent la déclaration du développeur (82/82) sans vérification
indépendante.

---

## 1. VERDICT EXÉCUTIF

### **GO SOUS CONDITIONS** — pilote "insights" mono-tenant uniquement.

**NO-GO** si le pilote est vendu comme "inventaire cloud filtrable" : cette capacité n'existe pas
dans le code (aucun endpoint `/resources`, aucune collecte de tags, aucune vue inventaire web).

Cinq raisons principales :

1. **[F-01, BLOCKER]** Une exception réseau non-`ClientError` en cours de pagination marque le
   scan `success` et déclenche le retirement des ressources non vues — une panne réseau peut
   "supprimer" des ressources. C'est la violation directe de la règle d'or du produit.
2. **[F-02, HIGH]** La fraîcheur n'est pas implémentée : `STALE` n'est jamais produit, aucune
   fenêtre temporelle nulle part. Le pilier "fraîcheur" du GTM est absent du code.
3. **[F-03, HIGH]** Chaque run d'insights insère sans purge ni dédup : trois exécutions = trois
   copies de chaque insight dans l'API. Violation du critère "rejeu sans doublon".
4. **[F-05, HIGH]** L'isolation tenant repose sur des policies RLS jamais testées automatiquement
   (tests sqlite no-op, CI sans Postgres, test "documentation" `assert True`), et 4 tables
   récentes n'ont pas de RLS du tout [F-04].
5. **Le socle est réellement bon** : source_runs/preuve de complétude, INCONCLUSIVE bout-en-bout,
   chaîne de provenance fact→run, catalog versionné, qualité de code et de documentation
   au-dessus de la moyenne. Les corrections sont incrémentales, pas une réécriture.

Confiance : **haute** sur les chemins critiques (collecteur, runner, repositories, RLS, migrations
lus en intégralité), **moyenne** sur les routers secondaires/CLI/web (lus partiellement), **basse**
sur le comportement runtime (rien exécuté).

---

## 2. SCORECARD

| Domaine | Note /5 | Justification (preuve) |
|---|---:|---|
| Adéquation produit V1 (périmètre doc archi) | **2** | Insights ✔, provenance ✔ ; inventaire filtrable ✘ (aucun router resources), tags ✘ (`db_to_facts` ne produit que engine/version/class/vcpu), fraîcheur ✘ [F-02] |
| Adéquation produit (périmètre AGENTS.md : 1 insight démo + chargeback) | **4** | Le périmètre redéfini est livré, avec INCONCLUSIVE et preuve de complétude |
| Architecture | **4** | Monolithe modulaire conforme, contrats de packages (AGENTS.md), séparation core/connectors/insights/api respectée dans les imports |
| Modèle de données | **3.5** | Current-state facts + chaîne source_run (0006) ✔ ; pas d'incarnations, retirement 1-scan [F-08], `external_id` UNIQUE global [F-12] |
| Sécurité | **3** | Clé API partagée constant-time ✔, set_config paramétré ✔ ; ExternalId optionnel [F-06], POST /insights forgeable [F-10], /metrics ouvert |
| Isolation multi-tenant | **3** | RLS FORCE + GUC + default-deny NULL : design correct ; jamais vérifié automatiquement [F-05], 4 tables sans RLS [F-04], rôle unique owner |
| Collecte & résilience | **2.5** | source_runs + index partiel + cleanup_stuck ✔ ; F-01 (BLOCKER), pas de backoff explicite, collecte synchrone HTTP [F-09] |
| Qualité du code | **4.5** | Typé, documenté au niveau décisionnel (chaque « pourquoi » est écrit), ruff+mypy en CI, conventions constantes |
| Tests | **3** | 31 fichiers, cas limites réels ; sqlite-only (RLS/JSONB/migrations non exercés), scénario F-01 absent, non exécutés dans cet audit |
| Exploitabilité | **2.5** | Logs structurés + request-id + métriques Prometheus + audit_events ✔ ; zéro IaC, zéro déploiement défini, pas d'alerting, backup non testé |
| Scalabilité | **3** | OK pour l'échelle pilote (≤10k ressources) ; `query.all()` + N+1 dans le runner [F-16], idempotence in-process |
| Coût opérationnel | **4** | 1 process + 1 Postgres + MinIO ; rien de surdimensionné |
| Expérience développeur | **4.5** | uv workspace, conftest factorisé, AGENTS.md exemplaire, un nouveau dev peut ajouter un insight en suivant le pattern |
| Frontend & API | **2.5** | API cohérente (pagination bornée le/500, Pydantic strict) mais surface minimale ; web = 3 fichiers (détail insight) |
| **Préparation pilote** | **GO conditionnel** | Conditions §8 ; le BLOCKER F-01 n'est pas masqué par la moyenne |

---

## 3. POINTS FORTS (démontrés dans le code)

- **Preuve de complétude opérante** : `source_runs` + index unique partiel `status='running'`
  (0005, orm.py L346-355) + `cleanup_stuck_runs` + `force` : leasing, anti-concurrence, reprise
  après crash — tous présents et testés (`test_source_runs.py`).
- **INCONCLUSIVE bout-en-bout** : `InsightResult` (resolver L36-55) → runner (L119-159, y compris
  `scope_not_proven`) → table `inconclusive` → endpoint. « L'ignorance est affichée » est
  implémenté, pas juste déclaré.
- **Provenance chaînée** : `facts.last_source_run_id` + `observations.source_run_id` (0006) —
  chaque valeur remonte au run qui l'a produite.
- **RLS bien conçue** (à défaut d'être vérifiée) : FORCE sur owner, `set_config` paramétré,
  `current_setting(..., true)` → default-deny sur GUC absent, WITH CHECK anti-insertion
  cross-tenant (0007 ; tenant.py L61-91).
- **Catalog versionné avec tiers réels** : `PostgresEOLInfo` avec `year_1_2`/`year_3_plus` et
  `end_of_extended_support` + sources URL en docstring (catalog/aws.py L17-69).
- **Idempotency-Key Stripe-style** avec namespacing par endpoint, thread-safe, documenté jusqu'à
  ses limites (in-process, perdu au restart) — les limites écrites sont exactes.
- **Primitives conformité** inhabituelles à ce stade : audit_events append-only sans PII,
  retention_policies avec allow-list, pii_classifications hash-only (0010).
- **Honnêteté du code** : les dettes sont écrites là où elles vivent (auth.py, idempotency.py,
  routers/aws.py « V2: queue + background worker »). Aucune promesse exactly-once nulle part.

---

## 4. FINDINGS

| ID | Sévérité | Confiance | Domaine | Finding | Preuve | Impact | Correction | Vérification |
|---|---|---|---|---|---|---|---|---|
| F-01 | **BLOCKER** | Haute | Collecte | Exception non-`ClientError` mid-scan ⇒ run `success` ⇒ retirement de ressources vivantes | collectors/aws.py L184-263 : `except ClientError` seul ; `finally` calcule `status` depuis `region_error` (None si BotoCoreError) puis lance `retire_stale_resources` | Fausse suppression = corruption de la promesse produit | Voir détail ci-dessous | Test : `scan_fn` qui yield 1 puis raise `ReadTimeoutError` → run `failed`, 0 retirement |
| F-02 | HIGH | Haute | Modèle | Fraîcheur non implémentée : `STALE` jamais assigné (grep : 0 setter), `_is_scope_proven` sans fenêtre d'âge | runner.py L70-84 ; grep STALE = enum/CHECK/health only | Un scope scanné il y a 30 j reste « prouvé » ; le pilier GTM fraîcheur est absent | `max_age` sur `latest_successful_run` (défaut 24 h) ; état STALE au read | Test : run vieux de 25 h → INCONCLUSIVE `scope_stale` |
| F-03 | HIGH | Haute | Produit/Données | Insights dupliqués à chaque run (insert sans purge/dédup) | runner.py L242-251, L326-329 ; insights repo : insert only | 3 runs = 3× chaque insight dans GET /insights ; « rejeu sans doublon » violé | Delete-and-replace par rule dans la transaction du run ; durable : `inputs_digest` + upsert | Test : 3 runs consécutifs → count constant |
| F-04 | HIGH | Haute | Sécurité | 4 tables sans RLS : focus_charge_tags, audit_events, retention_policies, pii_classifications | grep RLS/POLICY : uniquement 0007 ; tables créées en 0009/0010 | Fuite cross-tenant latente dès le tenant #2 ; drift exactement prévu par le doc | Migration 0011 : ENABLE+FORCE+policy sur les 4 ; checklist « nouvelle table ⇒ RLS » | Test CI Postgres (F-05) étendu aux 4 tables |
| F-05 | HIGH | Haute | Tests/Sécurité | Aucune vérification RLS automatisée : tests sqlite no-op, `test_rls_policies_documented` = `assert True`, CI sans service Postgres | tests/test_rls.py L142-178 ; ci.yml (pas de services:) | L'isolation tenant — critère zéro-défaut du doc — n'est protégée par aucun test exécuté | Job CI avec `services: postgres:16`, apply 0001→0010, scénario 2-tenants en pytest | Le scénario du docstring L149-173, automatisé |
| F-06 | HIGH | Haute | Sécurité | ExternalId optionnel sur AssumeRole cross-account (confused deputy) | collectors/aws.py L97-98 (kwargs conditionnels) ; TargetIn.external_id nullable | Si un client configure une trust policy sans ExternalId, rien ne l'exige côté SaaS | Valider : `role_arn` fourni ⇒ `external_id` obligatoire (Pydantic + collector) | Test : TargetIn avec role_arn sans external_id → 422 |
| F-07 | MEDIUM | Haute | Produit | Inventaire absent : pas d'endpoint /resources, pas de facts `aws.tag.*`, pas de vue web inventaire | routers/ (11 fichiers, aucun resources) ; collector.py db_to_facts L88-105 | Le périmètre V1 du doc d'architecture n'est pas couvert ; AGENTS.md l'a redéfini (insight démo + chargeback) sans trancher par écrit | Décision produit : assumer le pivot « insights-first » par ADR, ou construire l'inventaire avant de le vendre | ADR signé, ou endpoint + tags livrés |
| F-08 | MEDIUM | Haute | Modèle | Retirement après UN scan réussi (le doc exige 2 scans consécutifs ou NotFound corroboré) | resources.py L85-139 | Amplifie F-01 ; un glitch d'API AWS à cohérence éventuelle retire immédiatement | Exiger 2 runs success consécutifs sans la ressource, ou état `MISSING_SUSPECTED` | Test : 1 scan sans la ressource → active ; 2 scans → retired |
| F-09 | MEDIUM | Haute | Résilience | /collect/aws synchrone dans la requête HTTP ; idempotence in-process 5 min perdue au restart | routers/aws.py L78-131 ; idempotency.py L17-19 | Timeout LB sur parc >quelques comptes ; double-scan mitigé par l'index partiel uniquement | Acceptable pilote (documenté V2) ; seuil : >5 comptes ou >60 s de scan ⇒ queue+worker | Mesure de durée réelle au premier onboarding |
| F-10 | MEDIUM | Haute | Sécurité | POST /insights ouvert (« used by tests ») : tout porteur de clé forge un « écart prouvé » sans provenance | routers/insights.py L49-52 | Intégrité de la donnée vendue comme opposable | Retirer du build prod, ou gate admin + `source=manual` visible | Test : POST /insights → 403/absent en prod |
| F-11 | MEDIUM | Haute | Supply chain | `uv.lock` absent (vérifié : fichier inexistant) alors que CI fait `uv sync` | ci.yml L23 ; Read uv.lock → not found | Builds non reproductibles ; résolution différente à chaque CI | Committer uv.lock ; `uv sync --frozen` en CI | CI échoue si lock absent/désynchronisé |
| F-12 | MEDIUM | Haute | Modèle | `accounts.external_id` UNIQUE global (sans tenant_id) ; unique resources sans tenant_id | orm.py L86, L93-98 | Le tenant #2 partageant un AWS account id (cas MSP) est impossible ; migration douloureuse plus tard | UNIQUE(tenant_id, external_id) avant tout 2e tenant | Test contrainte sur 2 tenants même external_id |
| F-13 | MEDIUM | Moyenne | Produit | Chargeback : titres avec UUID interne (`str(orm.account_id)`), account_name vide ; sévérité sur drift amortized−billed (mécanique RI normale) | runner.py L182-183 ; chargeback resolver | Illisible en démo ; sévérités trompeuses (débat déjà acté : vue, pas insight) | Joindre accounts pour le nom ; retirer l'escalade de sévérité | Revue visuelle démo |
| F-14 | LOW | Haute | Config | `.env.example` documente `DATABASE_URL`/`S3_*` mais settings lit `CONSTAT_DATABASE_URL` ; `CONSTAT_API_KEY` absent de l'exemple | .env.example vs settings.py L21-32 | Opérateur suit l'exemple ⇒ mauvaise DB silencieuse + auth ouverte | Aligner les noms, ajouter CONSTAT_API_KEY avec avertissement | Relecture croisée |
| F-15 | LOW | Haute | Sécurité | /metrics sans auth (documenté) ; CORS origins hardcodé ; auth ouverte par défaut en dev | main.py L77-84 ; settings.py L26 | Faible en mono-tenant réseau privé ; à durcir avant exposition | CONSTAT_METRICS_KEY ; cors_origins env-driven | — |
| F-16 | LOW | Haute | Scalabilité | Runner : `query(ResourceORM).all()` + N+1 facts par ressource | runner.py L237, L132 | OK ≤10k ressources ; dégradation ensuite | Seuil : >50k ressources ⇒ jointure + pagination par curseur | Bench au premier gros compte |
| F-17 | INFO | Haute | Divers | `pii.record` classe la région comme PII ; MinIO au compose mais inutilisé (payloads en JSONB — dette actée) ; `docs/development/known-issues.md` référencé (orm.py L146) mais absent ; 0008/0009 même nom de fichier de base | collectors/aws.py L287-293 ; docker-compose ; glob docs/ vide | Bruit, dette documentaire | Nettoyage opportuniste | — |

### F-01 — détail exigé (BLOCKER)

**Scénario concret** : scan eu-west-1, 40 instances RDS, `DescribeDBInstances` page 1 OK (20
ressources upsertées, `last_seen_at` bumpé), page 2 → `ReadTimeoutError` (botocore,
**hérite de BotoCoreError, pas de ClientError**). L'exception traverse le `except ClientError`
(L225), le `finally` (L231-263) s'exécute avec `region_error = None` ⇒ `status = "success"`,
`finish_run(success)` ⇒ `retire_stale_resources` retire **les 20 instances de la page 2**
(leur `last_seen_at` < `run.started_at`). L'exception remonte ensuite dans `collect_targets`
qui ne catch que `ClientError` ⇒ 500, mais le mal est commis et sera commité par le prochain
commit de session.

**Cause racine** : la classification d'erreur repose sur un seul type d'exception, et le statut
de succès est calculé par défaut (absence d'erreur enregistrée) au lieu d'être prouvé (flag
positionné après complétion de la boucle).

**Correction minimale** (3 lignes) : flag `scan_completed = False` avant la boucle de scan,
`scan_completed = True` après ; dans le `finally`, `status = "success" if (scan_completed and
region_error is None) else "failed"`.

**Correction durable** : `except ClientError` + `except BotoCoreError` distincts avec
classification (AccessDenied / Throttling / Timeout / Unknown), statut `partial` pour les scans
interrompus, et le retirement conditionné à `status == "success"` **et** à un second run
consécutif (F-08).

**Test de preuve** : générateur `scan_fn` qui yield N ressources puis raise
`botocore.exceptions.ReadTimeoutError` → assert `run.status == "failed"`, assert 0 retirement,
assert les N ressources upsertées ont bien `last_seen_at` bumpé.

---

## 5. ANALYSE DES TESTS

**Non exécutés** (sandbox indisponible — voir en-tête). Analyse statique de 31 fichiers.

**Réellement bons** : cas limites métier (EOL ±90 j, versions malformées, UNKNOWN par gate),
`test_source_runs` (concurrence par index partiel, force, cleanup), `test_facts_upsert`
(current-state), consolidation `make_rds_db_dict` en conftest, `today` injectable partout
(déterminisme).

**Faux sentiment de sécurité** :
1. `test_rls_policies_documented` passe toujours (`assert True`) — un test vert qui ne teste rien.
2. Toute la suite tourne sur **sqlite** : RLS, JSONB, index partiels Postgres, `set_config`,
   les 10 migrations SQL — jamais exercés. `create_all()` ORM ≠ migrations : le drift
   schéma-ORM n'est détecté par rien.
3. Les tests FOCUS valident contre un CSV synthétique maison, pas un golden dataset issu d'un
   export AWS Data Exports réel (la non-conformité AmortizedCost/Region a déjà été ratée une
   fois par ce même pattern).
4. Aucun test du chemin F-01 (exception non-ClientError).

**Matrice minimale avant pilote** :

| Priorité | Test | Prouve |
|---|---|---|
| P0 | Exception BotoCoreError mid-scan → failed + 0 retirement | F-01 |
| P0 | CI Postgres : apply 0001→0010 + scénario RLS 2 tenants (SELECT/INSERT cross-tenant refusés) sur les 13 tables | F-04, F-05 |
| P0 | 3 runs insights consécutifs → count stable | F-03 |
| P0 | Run success vieux de >24 h → INCONCLUSIVE scope_stale | F-02 |
| P1 | role_arn sans external_id → 422 | F-06 |
| P1 | Golden dataset FOCUS 1.0 réel (AWS Data Exports anonymisé) → loader OK | conformité FOCUS |
| P1 | Drift ORM/migrations : create_all vs schéma migré → diff vide | intégrité schéma |
| P2 | Deux collectes concurrentes même scope → une seule passe | leasing |
| P2 | Charge : 10k ressources → runner < N s | F-16 |

---

## 6. THREAT MODEL (concis)

**Actifs critiques** : credentials STS temporaires (mémoire process), external_ids clients
(en base + payloads requêtes), inventaire client (PG), clé API partagée (env), audit log.

**Frontières de confiance** : Internet→API (clé partagée unique) ; API→PG (rôle unique,
owner des tables, FORCE RLS) ; SaaS→comptes clients AWS (AssumeRole read-only).

**Scénarios principaux** : (1) vol de la clé API ⇒ accès total tenant, pas de rotation/scopes
— accepté V1, à durcir (ADR-10 du doc non implémenté) ; (2) confused deputy via ExternalId
optionnel [F-06] ; (3) forge d'insights via POST /insights [F-10] ; (4) opérateur malveillant
DB ⇒ FORCE RLS aide mais le rôle applicatif peut ALTER les policies (owner) — le doc §11.2
exigeait un rôle runtime non-owner : non implémenté.

**Contrôles existants** : constant-time compare, set_config paramétré, SQLAlchemy paramétré
(0 SQL brut concaténé observé), pas de fetch d'URL fournie par le client (SSRF non applicable
V1), audit append-only, PII hash-only.

**Risques résiduels acceptables V1** : clé unique, /metrics ouvert, CORS localhost —
à condition d'un déploiement réseau privé documenté.

---

## 7. ÉVALUATION « SOTA »

**Au niveau d'un SaaS B2B moderne** : discipline INCONCLUSIVE (rare, différenciant), preuve de
complétude par scope, provenance chaînée, audit/retention/PII primitives, qualité documentaire
du code, uv workspace + ruff + CI.

**Acceptable pour une V1 (dette actée et écrite)** : collecte synchrone, idempotence in-process,
clé API unique, observations en JSONB au lieu de S3/Parquet, mono-tenant effectif.

**Obsolète** : rien. La stack (Python 3.13, FastAPI, SQLAlchemy 2, Pydantic 2, Next.js 15,
PG16) est actuelle sans être bleeding-edge.

**Inutilement complexe** : pii_classifications à ce stade est en avance sur le besoin (INFO) —
mais c'est de la complexité vendeuse (questionnaires sécurité), défendable.

**Doit évoluer avant 10 tenants** : F-04/F-05/F-12 (RLS complète + testée + uniques par tenant),
rôle DB non-owner, auth par service accounts (ADR-10). **Avant 100** : queue + workers,
observations sur S3/Parquet, InventoryRowCurrent. **Avant 1 000** : cellules, partitionnement
facts (seuil ~500 M lignes, cf. ADR-02 du doc).

**Trajectoire graphe** : aucune décision V1 ne bloque une projection Neo4j future (resource_id
UUID stables, source_runs rejouables). Aucun besoin graphe démontré — conforme à l'ADR-08.

---

## 8. PLAN DE REMÉDIATION

**Avant toute démo client** (2-3 j) :
1. F-01 fix minimal + test [S, aucun dépendance, AC : test P0 vert]
2. F-03 delete-and-replace par run [S, AC : 3 runs → count stable]
3. F-13 noms de comptes lisibles [S, AC : démo sans UUID]
4. F-14 .env.example aligné [S]

**Avant pilote payant** (1-2 sem) :
5. F-05 CI Postgres + scénario RLS automatisé [M, AC : job vert sur les 13 tables]
6. F-04 migration 0011 RLS sur les 4 tables [S, dépend de 5 pour la preuve]
7. F-02 fenêtre de fraîcheur 24 h + INCONCLUSIVE scope_stale [M, AC : test P0]
8. F-06 external_id obligatoire si role_arn [S]
9. F-10 POST /insights retiré/gated [S]
10. F-11 uv.lock commité + --frozen [S]
11. F-01 correction durable (classification erreurs + partial) [M, dépend de 1]

**Dans les 30 jours** :
12. F-08 retirement 2-scans ou MISSING_SUSPECTED [M]
13. F-07 tranché par ADR : pivot insights-first assumé OU début inventaire (tags + /resources) [décision produit, L si construit]
14. Rôle PG runtime non-owner (doc §11.2) [M]
15. Runbook opérateur : déploiement, backup/restore testé, rotation clé [M]

**Dans les 90 jours** :
16. Queue + worker pour la collecte (seuil : >5 comptes) [L]
17. Golden datasets FOCUS réels + harnais connecteur (doc §5.5) [M]
18. Observations → S3/Parquet (seuil : >10 M observations ou payloads >100 Go) [L]

**Au-delà, sur seuils** : partitionnement facts (>500 M lignes), cellules SaaS (>20 % charge
un tenant), Iceberg/Neo4j/streaming selon ADR existants — aucun n'est proche.

---

## 9. QUESTIONS OUVERTES

1. **Le pilote est-il vendu "insights" ou "inventaire" ?** — change le verdict (F-07).
2. Résultat réel de `pytest` / `ruff` / `mypy` sur la machine du développeur (non vérifiable ici).
3. Existence d'un `package-lock.json` pour apps/web (node_modules présent, lockfile non vérifié).
4. Cible de déploiement réelle (réseau privé ? exposition publique ?) — conditionne F-15.
5. Les 82 tests annoncés vs 31 fichiers lus : cohérent en ordre de grandeur, non compté précisément.
6. Historique git complet (commits annoncés vs contenu : non recoupé commit par commit).

---

## 10. ANNEXE DE PREUVES

**Commandes exécutées** : aucune (sandbox indisponible : `HYPERVISOR_VIRT_DISABLED`). Toutes les
vérifications dynamiques (tests, lint, build, docker) sont **non exécutables** pour cette raison.

**Fichiers inspectés en intégralité** : main.py, auth.py, tenant.py, db.py, settings.py, orm.py,
idempotency.py, collectors/aws.py, insights/runner.py, repositories/{facts,resources,source_runs}.py,
routers/{aws,insights,admin}.py, packages/core (models.py, namespaces.py, catalog/aws.py L1-80),
packages/connectors/{aws_rds/collector.py, focus/loader.py (version antérieure + grep version
courante)}, packages/insights/rds_eol/resolver.py (L1-90 version courante), migrations 0001,
0004-0010 (0010 partiel L1-60), ci.yml, conftest.py (L1-80), tests/test_rls.py, test_rds_eol.py,
test_focus_loader.py, pyproject.toml, README.md (L1-40), AGENTS.md (L1-60), docker-compose.yml,
.env.example.

**Inspections par grep** : setters STALE (résultat : aucun), RLS dans migrations (résultat :
0007 uniquement), colonnes FOCUS du loader courant.

**Non inspectés** (déclaré) : routers/{focus,runner,status,accounts,compliance,health,
inconclusive,insight_runs}.py, repositories/{accounts,observations,insights,inconclusive,
focus_charges}.py, cli/*, middleware.py, metrics.py, logging.py, pii.py, audit.py, retention.py,
migrations 0002-0003, apps/web (hors listing), 27 des 31 fichiers de test, chargeback/resolver.py
version courante, focus/aggregator.py.

**Note sur l'outillage** : le listing récursif du dépôt était tronqué (>10 000 fichiers avec
.git et node_modules) ; toutes les existences de fichiers contestables ont été confirmées par
lecture directe. `uv.lock` : lecture directe → fichier inexistant (confirmé).

**État final du dépôt** : aucune modification — l'audit n'a utilisé que des opérations de
lecture (Read/Grep/Glob) ; le présent rapport est écrit hors du dépôt.

---

## 11. STATUT DE REMÉDIATION (2026-07-18, post-audit)

Tous les findings ont été traités. Vérification locale : **296 tests passés, 19 skippés**
(les 19 sont les tests Postgres RLS, exécutés en CI — pas de Docker sur la machine de
développement), `ruff check` + `ruff format --check` propres, `mypy` sur `packages/core`
propre. Les corrections ont d'abord été **vérifiées contre le code** : les 17 findings
étaient exacts.

| ID | Statut | Correction appliquée |
|---|---|---|
| F-01 | **CORRIGÉ** | Flag `scan_completed` (succès prouvé, plus par défaut) + `except BotoCoreError` distinct avec classification AccessDenied/Throttling/Timeout/Unknown + retirement conditionné à `success && scan_completed`. Test de preuve : `ReadTimeoutError` mid-scan → run `failed`, 0 retirement (`tests/test_collector_aws_audit_fixes.py`). |
| F-02 | **CORRIGÉ** | `latest_successful_run(max_age=...)` ; fenêtre par défaut 24 h (`DEFAULT_SCOPE_MAX_AGE`) ; run trop vieux → INCONCLUSIVE raison `scope_stale` (distincte de `scope_not_proven`). Test : run de 25 h → `scope_stale`. |
| F-03 | **CORRIGÉ** | Delete-and-replace par règle dans la transaction du run (`delete_insights_for_rule` / `delete_inconclusive_for_rule`). Test : 3 runs consécutifs → count constant. |
| F-04 | **CORRIGÉ** | Migration `0011_rls_remaining_tables.sql` : ENABLE+FORCE+policy GUC sur `focus_charge_tags`, `audit_events`, `retention_policies`, `pii_classifications`. Le test CI couvre les 13 tables. |
| F-05 | **CORRIGÉ** | CI : service `postgres:16-alpine`, application des migrations 0001→0011, scénario pytest 2-tenants réel (`@pytest.mark.postgres`, skip local sans `CONSTAT_TEST_DATABASE_URL`). Le placeholder `assert True` est supprimé. **Non vérifiable localement** (pas de Docker) — la CI fait foi. |
| F-06 | **CORRIGÉ** | `role_arn` ⇒ `external_id` obligatoire : 422 côté API (validateur Pydantic), `ValueError` côté collecteur avant tout appel STS. |
| F-07 | **TRANCHÉ** | ADR-12 (`docs/adr/ADR-12-insights-first-pivot.md`) : pivot insights-first confirmé ; l'inventaire filtrable n'est pas vendu avant d'exister. |
| F-08 | **CORRIGÉ** | Retirement après **2 scans success consécutifs** sans la ressource (`CONSECUTIVE_SCANS_FOR_RETIREMENT = 2`) ; un seul scan ne retire rien. |
| F-09 | **DOCUMENTÉ** | Contrainte V1 acceptée (known-issues §9) ; seuil de bascule queue+worker : >5 comptes. |
| F-10 | **CORRIGÉ** | `POST /insights` gated par `CONSTAT_ENABLE_MANUAL_INSIGHTS` (défaut OFF → 403) ; quand activé, `source="manual"` est estampillé dans le payload. |
| F-11 | **CORRIGÉ** | `uv.lock` généré et commité (retiré de `.gitignore`), `[tool.uv.sources]` ajouté, CI en `uv sync --frozen --all-packages`. Dépendances manquantes ajoutées au passage : `psycopg2-binary` (import de `db.py`), `httpx2` (TestClient), `types-PyYAML` (mypy CI). |
| F-12 | **CORRIGÉ** | `UNIQUE(tenant_id, external_id)` sur `accounts` (migration 0011 + ORM) ; `get_by_external_id` scopé par tenant. |
| F-13 | **CORRIGÉ** | Titres chargeback avec le nom du compte (jointure `accounts`, fallback UUID) ; escalade de sévérité sur drift supprimée (tout à `INFO`, magnitude conservée dans le payload). Doc `docs/insights/chargeback.md` alignée. |
| F-14 | **CORRIGÉ** | `.env.example` réécrit sur les variables réellement lues (`CONSTAT_*`), `CONSTAT_API_KEY` ajouté avec avertissement auth ouverte. |
| F-15 | **CORRIGÉ** | `CONSTAT_METRICS_KEY` (header `X-Metrics-Key`, comparaison constant-time ; ouvert + warning si non défini) ; CORS via `CONSTAT_CORS_ORIGINS`. |
| F-16 | **CORRIGÉ** | N+1 supprimé : requête bulk `list_facts_for_resources`. `query.all()` conservé (échelle pilote ≤10k, seuil documenté). |
| F-17 | **CORRIGÉ** | `0009` renommé (`0009_focus_charge_tags_table.sql`) ; `known-issues.md` mis à jour (MinIO §8, région-PII §10, F-12 §11). |

**Corrections annexes découvertes pendant la remédiation** :
- La chaîne de migrations était **cassée sur base vierge** : 0007 référençait
  `insight_runs.tenant_id`, colonne jamais ajoutée (0004 l'avait oubliée). Corrigé dans
  0004 — aucun environnement n'avait pu appliquer l'ancienne chaîne au-delà de 0007.
- `tests/__init__.py` ajouté : la suite était inutilisable via `uv run pytest`
  (imports `tests.conftest` cassés hors `python -m pytest`).

**Reste à faire (reporté, non bloquant pilote)** : rôle PG runtime non-owner (doc §11.2),
golden dataset FOCUS réel, runbook opérateur, queue+worker au-delà de 5 comptes — voir §8.
