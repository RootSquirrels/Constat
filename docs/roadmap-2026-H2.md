# Roadmap H2-2026 — SOTA par l'exécution

> Remplace `roadmap-scoreboard-features.md` comme feuille de route active.
> Contexte : le comité d'évaluation client a produit ~25 corrections sur un
> système que trois audits statiques avaient jugé « SOTA ». Cette roadmap
> corrige les findings ET la méthode qui les a laissés passer.

## Pourquoi « SOTA » a cassé — trois causes, trois réponses

1. **Les audits étaient statiques** (lecture de code, rien d'exécuté). Tout ce
   qui ne se voit qu'à l'exécution — npm ci cassé, frontend en 401, HTTP en
   clair *appliqué*, purge jamais lancée — était noté « non vérifiable » puis
   scoré avec optimisme. → Réponse : **chantier 0, la preuve par exécution
   devient le gate**, plus jamais la lecture seule.
2. **Le verdict était périmétré** (« SOTA pour le périmètre vendu ») ; le
   comité a évalué **le produit qu'on achète** : déploiement, ROI vérifiable,
   onboarding réel. → Réponse : la definition of done change (§ méthode).
3. **Les seuils initiaux étaient auto-référencés** (« asynchrone au-delà de
   5 comptes ») alors que l'ICP écrit commence à 5 et se vend à 35.
   → Réponse : **les seuils dérivent de l'ICP**, pas du confort d'ingénierie.
   Cette roadmap ignore les anciens seuils quand l'évolution est pertinente.

**Nouvelle definition of done** (s'applique à chaque item ci-dessous) :
démontré en exécution, sur environnement déployé, avec artefact observable
(run CI, capture, ligne de journal datée). « Le code le fait » ne suffit plus.

Ce qu'on continue de ne PAS faire (over-engineering exclu) : Kubernetes,
Kafka/streaming, Neo4j, DSL de règles, data lake, microservices, mutualisation
multi-tenant forcée (l'instance dédiée par client EST notre modèle de cellule —
la mutualisation attendra un coût d'exploitation qui la justifie).

---

## Chantier 0 — La preuve par l'exécution *(semaines 1-3 · le gate de tout le reste)*

| # | Action | Effort | Critère d'acceptation |
|---|---|---|---|
| 0.1 | **Staging permanent** : `terraform apply` réel en eu-west-3, image construite, migrations appliquées | M | L'URL staging répond en HTTPS ; apply rejouable depuis zéro |
| 0.2 | **CI = arbitre unique** : pytest + `npm ci && build` + chaîne migrations + RLS Postgres + `terraform validate`/`plan` + build Docker, tout bloquant (pip-audit/npm audit inclus après triage) | S | Run vert publié sur le commit ; le badge remplace toute déclaration « tests OK » |
| 0.3 | **E2E quotidien sur compte sandbox Constat** : vrai AssumeRole → vrai scan → vraie restitution, chronométré | M | Le test qui manquait au SRE : durée, appels API, throttling mesurés chaque nuit |
| 0.4 | **Backup/restore exécuté** + game day (kill de la tâche mid-scan → reprise) | S | Ligne datée dans le runbook ; RTO/RPO mesurés, pas déclarés |
| 0.5 | Reliquats comité : frontend authentifié contre l'API réelle (staging), purge lancée en réel, alertes reçues par un humain | S | Capture de l'alerte reçue ; 0 ligne « not yet executed » restante |

## Chantier 1 — Collecte à l'échelle ICP *(semaines 3-8 · abandon assumé du seuil « 5 comptes »)*

| # | Action | Effort | Critère d'acceptation | Statut 2026-07-19 |
|---|---|---|---|---|
| 1.1 | **Collecte asynchrone** : SQS + worker ECS service ; work item = compte×région ; l'idempotence existante (index partiel SourceRun) sert de dédup ; `POST /collect` → 202 + run consultable | L | 35 comptes × 7 régions collectés sans timeout HTTP ; échec d'un item n'affecte pas les autres | **CODE LIVRÉ** : file (inline/SQS), worker, 202 + `GET /collect/aws/jobs/{id}`, migration 0015, terraform `sqs.tf`+service worker, isolation d'échec testée (551→569 tests). Exécution 35 comptes : **attend le staging (chantier 0)** |
| 1.2 | Concurrence bornée par compte (quotas AWS) + backpressure | M | Throttling observé < seuil sur l'e2e 35 comptes | **CODE LIVRÉ** : `PerAccountLimiter` (`CONSTAT_WORKER_PER_ACCOUNT`), file bornée → 503 + Retry-After, backoff adaptatif boto3. Mesure de throttling : e2e staging |
| 1.3 | **Onboarding par lot** : StackSet / intégration Organizations (`ListAccounts`) + import CSV | M | 35 comptes onboardés en < 2 h, chronométré en staging | **CODE LIVRÉ** : `collect_targets` persistées (0016, RLS, ExternalId write-only), `POST /collect/targets/import` (CSV), découverte Organizations + `cli.onboard`, `infra/customer/stackset.yaml`. Chrono < 2 h : staging |
| 1.4 | Re-scan ciblé en un appel API (région/compte) branché au runbook d'alerte | S | L'opérateur du runbook n'ouvre plus psql | **FAIT** : runbook `alerting.md` réécrit (un `POST /collect/aws` région-ciblé + `force`, suivi du job) |
| 1.5 | Bench réel publié : durée, coût AWS, appels par run à 35 comptes | S | docs/operations/benchmarks.md § « réel » — remplace le bench sqlite | **HARNAIS PRÊT** : `scripts/bench_real.py` + section « réel » PENDING EXECUTION. Aucun chiffre tant que le staging n'existe pas |

Lève la limite contractuelle de l'avenant SLA §1 une fois 1.1-1.3 démontrés.

## Chantier 2 — Un chiffre défendable devant une DAF *(semaines 3-8, parallèle)*

| # | Action | Effort | Critère d'acceptation | Statut 2026-07-19 |
|---|---|---|---|---|
| 2.1 | **Tarifs par région** au catalog (plus de « US East pour tout le monde ») + devise source affichée | M | Un insight eu-west-3 cite la grille eu-west-3, datée | **CODE LIVRÉ** : grilles EBS us-east-1/eu-west-1/eu-west-3 sourcées (page AWS + Price List API), facts `region`, `price_region_exact` dans les payloads ; découverte : l'Extended Support RDS n'est **pas** uniforme (+12-18 % eu-west) — grille régionale RDS en cours d'application aux 3 règles |
| 2.2 | **Conversion EUR** datée (taux BCE référencé), montants doubles USD/EUR | S | Restitution en € avec taux et date en pied de page | **FAIT** : `catalog/fx.py` (BCE 2026-07-17 : 1 EUR = 1,1435 USD), colonnes EUR + taux + date dans l'export CSV, double affichage $/€ dans la restitution avec pied de page BCE |
| 2.3 | **ESTIMATED → ACTUAL** : rapprochement ligne FOCUS ↔ ressource (ResourceId déjà chargé) ; l'estimation confirmée par facture change de statut | M | Part « confirmée par facture » affichée — la question n°2 de la DAF | **CODE LIVRÉ** : réconciliation post-run native_id↔ResourceId (par famille de service FOCUS), `focus_actual_monthly_usd` + bascule ACTUAL, `kind` ADR-13 invariant. Affichage « part confirmée » en restitution : à brancher (suivi) |
| 2.4 | **Historique apparu/résolu** : delta entre runs, courbe « € récupérés » | M | Le KPI de renouvellement existe ; réponse à « que reste-t-il en année 2 » | **CODE LIVRÉ** : table `insight_events` (0017, RLS), diff par empreinte dans les 2 runners, `GET /insights/history` + total `resolved_monthly_usd_total`. Courbe web : à brancher (suivi) |
| 2.5 | Inconclusifs = file de travail : owner + échéance sur l'acquittement existant, tri par impact potentiel | S | Sur 35 comptes, les inconclusifs sont triables, pas du bruit | **CODE LIVRÉ** : owner/due_date/status (0018), `PATCH /inconclusives/{id}` + audit, tri règle/âge (pas de score d'impact inventé — les inconclusifs n'ont pas de montant) |
| 2.6 | Détection FOCUS partiel/mois manquant → bandeau d'avertissement | S | Un export tronqué ne produit plus un chargeback silencieusement faux | **FAIT** : `GET /focus/coverage` (mois manquants + stale > 45 j) + bandeau ambre sur la page chargeback |

## Chantier 3 — Un SaaS qu'un RSSI signe *(semaines 6-12)*

| # | Action | Effort | Critère d'acceptation |
|---|---|---|---|
| 3.1 | **Tenant par requête** : résolution depuis l'identité (plus de tenant par défaut) + service accounts à scopes (ADR-10 du doc d'archi, enfin dû) | L | Test e2e 2 tenants **via l'API** sur staging ; l'étiquette « fondation » du schéma devient « démontré » |
| 3.2 | **SSO OIDC** pour l'UI (Google/Microsoft — ce que l'ETI a déjà) | M | Login sans clé partagée ; rôles lecture/admin |
| 3.3 | **Audit des lectures** : qui a consulté quoi (le trou RSSI : lectures API non attribuées) | M | Réponse démontrable à « qui a vu mes données » — **CODE LIVRÉ 2026-07-18** : attribution par principal (RBAC `CONSTAT_API_KEYS`), action `api.read` dans `audit_events`, réponse via `GET /compliance/audit-events` ; à dater en exécution sur staging (chantier 0) |
| 3.4 | Immutabilité du journal : trigger Postgres interdisant UPDATE/DELETE sur audit_events | S | Le mot « append-only » devient techniquement garanti — **CODE LIVRÉ 2026-07-18** : migration 0014 (triggers UPDATE/DELETE/TRUNCATE), tests Postgres en CI ; critère exécuté au premier run CI vert |
| 3.5 | Dossier sécurité : questionnaire type pré-rempli, DPA, politique de rétention signable | M | Le RSSI reçoit le dossier avant de le demander |
| 3.6 | Pentest externe | M | Avant le 3e client payant ; findings triés publiés au client sur demande |

## Chantier 4 — Le moat *(continu, dès semaine 4)*

| # | Action | Effort | Critère d'acceptation |
|---|---|---|---|
| 4.1 | **Référentiels en données** : EOL/tarifs migrés du code vers `ReferenceDatasetVersion` (le concept du doc d'archi), avec job mensuel de vérification diff contre les pages AWS citées | M | Une mise à jour de tarif = une donnée versionnée, pas un commit ; l'écart détecté alerte |
| 4.2 | Golden datasets réels par source (export FOCUS AWS anonymisé du 1er pilote) | S | La classe de bug « conforme à notre CSV maison » fermée définitivement |
| 4.3 | **Tags + inventaire filtrable** (`aws.tag.*`, `/resources`, vue) — lève l'ADR-12 dès la demande d'un pilote, préparation anticipée | L | Chargeback par tag réel ; le mot « inventaire » redevient prononçable en démo |
| 4.4 | Connecteur Compute Optimizer (gratuit, opt-in) → insights de surdimensionnement | M | 2e vague de valeur par démo, sans dépendance support plan |
| 4.5 | Cadrage « opposable » dans tout le matériel commercial (aligné SLA §5) | S | L'objection juridique du comité a une réponse écrite partout |

---

## Séquence et jalons

```
S1-S3   Chantier 0 (gate)            → Jalon A : « ça tourne, prouvé » — staging vert, e2e réel, restore daté
S3-S8   Chantiers 1 + 2 (parallèle)  → Jalon B : « 35 comptes, en euros » — bench réel publié, restitution EUR/région
S6-S12  Chantier 3                   → Jalon C : « signable » — tenant par requête démontré, SSO, dossier sécurité
S4-...  Chantier 4 (continu)         → Jalon D : « défendable durablement » — référentiels en données, golden réels
```

Le jalon A conditionne toute démo. Le jalon B lève la limite 5 comptes du SLA.
Le jalon C conditionne le 2e client. Rien d'autre n'est bloquant.

## Règle de gouvernance de cette roadmap

Chaque item se ferme par son critère d'acceptation **exécuté et daté**, jamais
par « le code est écrit ». Tout nouvel item passe le filtre : « quel persona du
comité (FinOps, SRE, RSSI, DAF, marketing) cesse d'objecter si c'est fait ? »
Un item sans persona est de l'over-engineering — il sort.
