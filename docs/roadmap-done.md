# Réalisé — archive de la roadmap H2-2026

> Les items terminés sortent de `roadmap-2026-H2.md` (règle : la roadmap
> active ne liste que ce qui reste à faire). Deux niveaux d'honnêteté :
> **FAIT** = critère d'acceptation exécuté et daté · **CODE LIVRÉ** =
> implémenté et testé, exécution datée en attente du staging (chantier 0).

## Chantier 1 — Collecte à l'échelle ICP

| # | Item | Statut | Preuve |
|---|---|---|---|
| 1.1 | Collecte asynchrone (SQS + worker, 202 + job consultable) | CODE LIVRÉ 2026-07-19 | commits `f5da554`, `df215d6` ; exécution 35 comptes : staging |
| 1.2 | Concurrence bornée par compte + backpressure | CODE LIVRÉ 2026-07-19 | `PerAccountLimiter`, 503 + Retry-After ; throttling mesuré : staging |
| 1.3 | Onboarding par lot (CSV, Organizations, StackSet) | CODE LIVRÉ 2026-07-19 | commit `f49a5c1` ; chrono < 2 h : staging |
| 1.4 | Re-scan ciblé en un appel API (runbook) | FAIT 2026-07-19 | `docs/operations/alerting.md` — plus de psql |

## Chantier 2 — Un chiffre défendable devant une DAF

| # | Item | Statut | Preuve |
|---|---|---|---|
| 2.1 | Tarifs par région au catalog | FAIT 2026-07-19 | commits `4c63c1d`, `ff4042b` ; grilles EBS + ES RDS sourcées (Price List API 2026-07-17), `price_region_exact` partout |
| 2.2 | Conversion EUR datée (BCE) | FAIT 2026-07-19 | `catalog/fx.py`, doubles montants CSV + restitution, pied de page taux+date |
| 2.3 | ESTIMATED → ACTUAL (rapprochement FOCUS) | CODE LIVRÉ 2026-07-19 | commit `966ecf9` ; affichage restitution de la part confirmée : en suivi |
| 2.4 | Historique apparu/résolu | CODE LIVRÉ 2026-07-19 | `insight_events` (0017), `GET /insights/history` ; courbe web : en suivi |
| 2.5 | Inconclusifs = file de travail | CODE LIVRÉ 2026-07-19 | owner/due_date/status (0018), PATCH + audit |
| 2.6 | Détection FOCUS partiel + bandeau | FAIT 2026-07-19 | commit `841f478` : `GET /focus/coverage` + bandeau chargeback |

## Chantier 3 — Un SaaS qu'un RSSI signe

| # | Item | Statut | Preuve |
|---|---|---|---|
| 3.3 | Audit des lectures (attribution) | CODE LIVRÉ 2026-07-18 | commit `648e239` : principal RBAC, `api.read`, `GET /compliance/audit-events` |
| 3.4 | Immutabilité du journal (trigger) | CODE LIVRÉ 2026-07-18 | migration 0014 ; tests Postgres désormais exécutés en CI (`-m postgres` sur toute la suite, commit `b39dd93`) — le premier run CI vert date le critère |

## Pré-roadmap H2 (audit V1 + vague 1, juillet 2026)

- Audit V1 : 17 findings (F-01→F-17) corrigés — annexe §11 de `docs/audit-constat-v1.md`.
- Vague 1 : 6 règles monétaires + chargeback (rds_eol, mysql_eol, aurora_eol,
  ebs_gp2_to_gp3, ebs_unattached, snapshot_orphan, ec2_stopped_with_storage).
- Registre monétaire ADR-13 + SLA pilote borné (`docs/pilot/sla-pilote.md`).
- RBAC reader/operator, rôle PG non-owner `constat_app` (§11.2), golden
  dataset FOCUS 1.0, bench runner 10k, Terraform pilote + ALB/TLS.
