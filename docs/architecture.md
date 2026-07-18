# Architecture (V1)

> **Scope:** this document describes the system as it is built in V1. V2/V3
> decisions (Neo4j, Step Functions, Iceberg, EKS, controls) are explicitly
> out of scope. When we move to V2, those decisions get a dedicated ADR.

## The four-box mental model

```
┌────────────────────────┐    ┌────────────────────────────────────────┐
│  Sources               │    │  Product                               │
│  ───────               │    │  ───────                               │
│  AWS APIs (RDS)        │    │  apps/api (FastAPI, 6 routers)         │
│  FOCUS 1.0 CSV         │    │  apps/web (Next.js, 5 pages)           │
│  Catalog (EOL, vCPU)   │    │                                        │
└──────────┬─────────────┘    └────────────────▲───────────────────────┘
           │                                  │
           ▼                                  │
┌─────────────────────────────────────────────┴───────────────────────┐
│  Ingestion (apps/api/collectors + apps/api/cli + insight runner)   │
│  ───────                                                            │
│  AWS collector     → cross-account AssumeRole → boto3 paginator    │
│  FOCUS CLI/HTTP    → stdlib CSV → aggregator → focus_charges       │
│  Insight runner    → reads facts, evaluates, emits 3 states        │
└──────────────────────────────┬─────────────────────────────────────┘
                               │ writes
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Core (Aurora PostgreSQL — V1 is local docker, prod is RDS)         │
│  ───────                                                             │
│  7 tables: accounts, resources, observations, facts,                │
│           focus_charges, insights, inconclusive, source_runs        │
│  + insight_runs (audit)                                             │
└─────────────────────────────────────────────────────────────────────┘
```

The **core** is the only durable surface. **Sources** are reproducible
(re-scan the AWS API). **Ingestion** is replaceable. **Product** is
disposable as long as the API contract is stable.

## Read paths

```
Browser (Next.js)
  └─→ apps/web/lib/api.ts (typed fetch)
       └─→ apps/api/routers/* (FastAPI)
            └─→ apps/api/repositories/* (SQLAlchemy)
                 └─→ Postgres tables
```

The Next.js app does **not** import from `packages/*` directly. It only
talks to the API. This is the rule that lets us swap the frontend
without touching the core.

## Write paths

Three writers exist in V1, all synchronous:

| Writer | Entry point | Output |
|---|---|---|
| AWS collector | `POST /collect/aws` or `python -m constat_api.cli.aws` | `resources` + `observations` + `facts` + `source_runs` |
| FOCUS ingestion | `POST /collect/focus` or `python -m constat_api.cli.focus` | `focus_charges` |
| Insight runner | `POST /insights/run` or `python -m constat_api.cli.run_insights` | `insights` + `inconclusive` + `insight_runs` |

V1 keeps the writers synchronous. We add a queue (SQS, Fargate worker)
when a second connector needs a different cadence. Not before.

## The inventory-first promise

The architectural rule that makes the product defensible:

> *A `false` value is only emitted when the source and the scope together
> prove the absence. Otherwise: `UNKNOWN`.*

This is encoded in three places:

1. **Scope proof.** The `source_runs` table tracks, per
   `(account, region, resource_type, source)`, whether a scan has
   *succeeded*. An insight is only evaluated for a resource whose scope
   has a successful run (see
   `apps/api/src/constat_api/insights/runner.py::_is_scope_proven`).
   Otherwise the resource emits an `Inconclusive` record with
   `reason = "scope_not_proven"`.
2. **Fact state.** Every fact has a `value_state` of `KNOWN`,
   `UNKNOWN`, `STALE`, `ERROR`. The `rds_eol` resolver never emits a
   `false` negative; if a fact is missing, it returns an
   `Inconclusive` record with the missing keys
   (`packages/insights/rds_eol/src/constat_rds_eol/resolver.py::evaluate`).
3. **Catalog versioning.** EOL dates and vCPU tables are versioned in
   `packages/core/src/constat_core/catalog/aws.py` (file-level today,
   `reference_dataset` table in V2). A stale catalog degrades dependent
   insights to `INCONCLUSIVE`, never to `false`.

## The data flow per insight

### `rds_eol` (resource-based, scope-gated)

```
boto3 DescribeDBInstances (paginated)
  └─→ db_to_resource()     → resources table
  └─→ db_to_facts()        → facts (aws.rds.engine, engine_version, vcpu, instance_class)
  └─→ db_to_observation()  → observations (immutable, full payload)
        ↑ per region, wrapped in a source_run
                ↓
runner.run_rds_eol()
  └─→ for each resource: _is_scope_proven() ? facts_repo.list_facts_for_resource() : INCONCLUSIVE
        └─→ rds_eol.evaluate(resource_id, facts, today)
              └─→ catalog.aws.postgres_eol_info(major) → tier → price_per_vcpu_hour
                    └─→ Insight(payload=...) or InsightResult(inconclusive_reasons=[...])
```

### `chargeback` (account-based, no scope-gate)

```
FOCUS 1.0 CSV (one row per FOCUS charge)
  └─→ load_focus_csv()                → FocusCharge (stdlib csv)
        └─→ aggregate_for_storage()    → AggregatedFocusCharge (1 per service/period)
              └─→ focus_charges_repo.upsert_aggregated()  → focus_charges table
                    ↑ inserted/updated, no per-region scope gate
                            ↓
runner.run_chargeback()
  └─→ for each account with FOCUS data:
        └─→ aggregate_by_period(charges)   → AggregatedCost
              └─→ build_insights(aggregated) → Insight(payload={drift_usd, ...})
```

The chargeback rule has no `source_runs` check: FOCUS is "complete by
ingestion" — the user is the source. If a month is missing, the drift
over the other months is still meaningful; the missing month is a
business question, not a technical one.

## Multi-tenant

V1 is single-tenant. Every row has a `tenant_id` column (migration
0004), with the default tenant
`00000000-0000-0000-0000-000000000001`. RLS policies and the
per-session GUC binding are in place since commit `dc1bb7e
feat(api+db): multi-tenant RLS scaffolding` (migration 0007 +
`apps/api/src/constat_api/tenant.py`). V1 still uses the default
tenant for every connection; V2 will source the tenant from a
request header / service-account context. See
[`development/known-issues.md`](./development/known-issues.md) for
the BYPASSRLS follow-up that must be resolved before V2.

## Acceptance criteria (V1)

From the V1 brief, the system must satisfy:

1. Three to twenty AWS accounts connected, no permanent access key.
2. Onboarding < 2 hours.
3. ≥ 99% inventory concordance vs independent sample.
4. No duplicates after three replays of the same run.
5. No `AccessDenied` shown as `false`.
6. Every enriched cell exposes source and timestamp.
7. FOCUS works without defining the universe of resources.
8. Unmatched costs are visible, not silently dropped.
9. A new connector adds a new fact + column without changing the
   Resource model.
10. Inventory filters respect the SLO at pilot volume.
11. Zero cross-tenant leakage in API, Postgres, S3.
12. Per-run cost (AWS + FOCUS) is instrumented.
13. ≥ 3 insights produce a quantified estimate on the pilot.
14. Anything visible in the UI is extractable via the public read-only
    API.
15. Losing a source or catalog degrades the dependent insights to
    `INCONCLUSIVE` — never silent disappearance.

The product-level exit is: the customer identifies at least one
visibility gap they could not produce reliably before — **and at least
one gap whose euro value exceeds the annual platform price on their
perimeter.**

## SLOs (V1 pilot)

| SLO | Target |
|---|---|
| API monthly availability | 99.9% |
| `GET /insights` p95 | < 500 ms |
| First page after opening | < 2 s |
| AWS standard freshness | 95% of scopes < 6 h |
| Publication after end of collection | p95 < 30 min |
| Onboarding | < 2 h |
| Cells with source + timestamp | 100% |
| Silently-incomplete runs | 0 |
| Cross-tenant leaks | 0 |

6h AWS freshness is a *cost* choice, not a limit. Event-driven
re-scans come in V2; for V1 we re-scan on a cron.

## Risks (V1)

Ranked by probability × blast radius.

1. **Catalog drift.** A wrong EOL date or a missing vCPU entry (e.g. a
   new Graviton class) silently erodes the V1 hero insight. Mitigation:
   the catalog has a `Last reviewed` header and is the
   `apps/api/src/constat_api/insights/runner.py` test fixture's most
   pinned module.
2. **Scope assumption.** A successful `source_run` is the proof of
   completeness. If we ever evaluate an insight for a resource whose
   scope is `failed` or `running`, we break criterion n°15. Mitigation:
   `_is_scope_proven` is the gate; tests assert it.
3. **FOCUS shape drift.** AWS exports FOCUS 1.0 today; tomorrow they may
   add columns or rename. The loader validates the 11 required columns
   up front (`packages/connectors/focus/src/constat_focus/loader.py::FOCUS_REQUIRED_COLUMNS`).
4. **Per-tenant FOCUS data.** V1 assumes a single FOCUS CSV per account.
   When tenants export multiple files (e.g. per-org), the ingestion
   needs a merge step. Not solved in V1; documented as a known issue.
5. **Cost underestimation.** SLO 6 says 6h freshness; if the pilot
   customer expects real-time, we fail the demo. Document this in the
   POC, not after.

## What is NOT in V1 (intentional)

This is the backlog. Adding any of these in V1 needs a one-paragraph
justification in the PR description.

- **Step Functions / SQS / Fargate workers.** V1 is a single
  synchronous API. We add a queue when a second connector needs a
  different cadence.
- **Multi-tenant RLS follow-up (BYPASSRLS discipline + per-request
  tenant context).** Policies and GUC binding are in (commit
  `dc1bb7e`); the API role is still the migration owner. Promote
  to a non-owner, non-superuser, no-BYPASSRLS role before V2.
- **Full `FactDefinitionRegistry` ceremony.** V1 uses a `namespace.key`
  string + `CHECK` constraint. The registry is V2.
- **Azure, ServiceNow, Prisma, EDR connectors.** V2+.
- **Streaming, Neo4j, Iceberg, EKS.** Seuil-triggered, not V1.
- **Remediation / `SendCommand` / write role.** V3.
- **Tag-based chargeback grouping (Application, CostCenter).** V2.

## Where to read next

- The data shape: [`data-model.md`](./data-model.md)
- The 9 concepts: [`concepts.md`](./concepts.md)
- The hero insight: [`insights/rds-extended-support.md`](./insights/rds-extended-support.md)
