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
