# Data model (V1)

> **Source of truth for the schema is the SQL migrations**, not the ORM
> (see [`development/known-issues.md`](./development/known-issues.md) for
> the drift). This doc is the human-readable view of the migrations.
> Migrations are append-only and re-runnable on a fresh DB; the ORM is
> test-only.

## The 13 tables at a glance

| Table | Purpose | FK in | FK out | Migration |
|---|---|---|---|---|
| `accounts` | One row per prospect AWS account (or FOCUS BillingAccountId) | — | — | `0001_init.sql` + `0011` |
| `resources` | Stable identity of a cloud resource, proven by a scan | `account_id` | — | `0001_init.sql` |
| `observations` | Immutable source payload, replayable | `resource_id`, `source_run_id?` | — | `0001_init.sql` + `0006` |
| `facts` | Current value, namespaced, with `value_state` | `resource_id?`, `account_id?`, `last_source_run_id?` | — | `0001_init.sql` + `0006` |
| `focus_charges` | FOCUS 1.0 cost rows, one per (account, service, period) | `account_id` | — | `0001_init.sql` + `0003` + `0008` |
| `focus_charge_tags` | Per-input-row FOCUS tags (proportional attribution) | `focus_charge_id` | — | `0009_focus_charge_tags_table.sql` |
| `insights` | Computed gaps (MATCH) | `resource_id?`, `account_id?` | — | `0001_init.sql` + `0013` |
| `inconclusive` | "We don't know" records (missing facts) | `resource_id?`, `account_id?` | — | `0002_inconclusive.sql` |
| `source_runs` | Proof of scan completeness per (account, region, type) | `account_id` | — | `0005_source_runs.sql` |
| `insight_runs` | Audit row per rule execution | — | — | `0001_init.sql` |
| `audit_events` | Append-only "who did what when" log | — | — | `0010_audit_retention_pii.sql` |
| `retention_policies` | Declarative per-table retention config | — | — | `0010_audit_retention_pii.sql` |
| `pii_classifications` | Per-field sensitivity labels + value hash | — | — | `0010_audit_retention_pii.sql` |

Every tenant-scoped table also has a `tenant_id UUID NOT NULL` column
and an index on it (added in `0004_tenant_id.sql`).

## The FK chains (the integrity of the system)

```
                            ┌──────────────┐
                            │   accounts   │  UNIQUE(tenant_id,
                            │              │  external_id)
                            └──────┬───────┘
                                   │
              ┌────────────────────┼────────────────────────┐
              │                    │                        │
              ▼                    ▼                        ▼
       ┌──────────────┐     ┌──────────────┐         ┌───────────────┐
       │  resources   │     │ focus_charges│         │ source_runs   │
       │ UNIQUE(      │     │ (FOCUS       │         │ UNIQUE(       │
       │   account_id,│     │  1.0 per     │         │   account_id, │
       │   region,    │     │  service +   │         │   region,     │
       │   resource_  │     │  period)     │         │   resource_   │
       │   type,      │     └──────────────┘         │   type,       │
       │   native_id) │                                │   source)     │
       └──────┬───────┘                                │ WHERE status= │
              │                                        │   'running'   │
              │                                        └──────┬────────┘
              │                                               │
              │ FK                                            │ FK
              │                                               │
       ┌──────┴───────┐         ┌─────────────────┐           │
       │ observations │◄────────│ source_run_id   │◄──────────┘
       │ (immutable   │  FK     │  on observations│
       │  payload)    │  optional                │
       └──────┬───────┘                            │
              │                                    │
              │                                    │
       ┌──────┴───────┐                            │
       │    facts     │◄───────────────────────────┘
       │ UNIQUE(      │     FK last_source_run_id
       │   tenant_id, │         (optional)
       │   resource_id,
       │   namespace,
       │   key,
       │   source)
       └──────┬───────┘
              │
              │ read by the insight runner
              │
       ┌──────┴───────┐    ┌──────────────────┐
       │   insights   │    │   inconclusive   │
       │  (MATCH)     │    │  ("we don't know")│
       └──────────────┘    └──────────────────┘
```

The runner-side loop:

```
runner.run_rds_eol()
  └─→ for each resource:
        ├─→ source_runs_repo.latest_successful_run(scope)  → bool
        │     └─→ if False: emit Inconclusive(reason="scope_not_proven")
        └─→ facts_repo.list_facts_for_resource(resource_id)
              └─→ rds_eol.evaluate(resource_id, facts, today)
                    └─→ emits Insight OR Inconclusive (with missing_facts)
```

## Table-by-table

### `accounts`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | default `gen_random_uuid()` |
| `tenant_id` | UUID NOT NULL | default V1 tenant (see `settings.py::DEFAULT_TENANT_ID`) |
| `external_id` | TEXT NOT NULL | AWS account ID (12 digits) or FOCUS `BillingAccountId`. UNIQUE `(tenant_id, external_id)` — tenant-scoped since `0011` (the MSP case: one AWS account, several customers) |
| `name` | TEXT | friendly label, optional |
| `created_at` | TIMESTAMPTZ NOT NULL | default `NOW()` |

One row per connected AWS account or per FOCUS `BillingAccountId`.
Created lazily by `accounts_repo.get_or_create` on first encounter from
either path.

### `resources`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | our internal stable id |
| `tenant_id` | UUID NOT NULL | |
| `account_id` | UUID NOT NULL FK | → `accounts.id` ON DELETE CASCADE |
| `region` | TEXT NOT NULL | AWS region, e.g. `eu-west-1` |
| `resource_type` | TEXT NOT NULL | `AWS::RDS::DBInstance` (CloudFormation type) |
| `native_id` | TEXT NOT NULL | ARN in V1 |
| `first_seen_at` | TIMESTAMPTZ NOT NULL | default `NOW()` |
| `last_seen_at` | TIMESTAMPTZ NOT NULL | bumped on each scan that sees the resource |
| `retired_at` | TIMESTAMPTZ | null = active. Set only after a successful scan proves the absence |

UNIQUE: `(account_id, region, resource_type, native_id)`. The
identity is the 4-tuple, not the UUID; the UUID is a surrogate for
joins and external references.

Index: `idx_resources_account_type` on `(account_id, resource_type)`.
Partial index: `idx_resources_active` on `(account_id) WHERE
retired_at IS NULL` for the "active only" inventory view.

### `observations`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL | |
| `resource_id` | UUID NOT NULL FK | → `resources.id` CASCADE |
| `source` | TEXT NOT NULL | e.g. `aws_rds` |
| `observed_at` | TIMESTAMPTZ NOT NULL | scan time |
| `payload` | JSONB NOT NULL | full source payload (allowlisted in V1) |
| `ingested_at` | TIMESTAMPTZ NOT NULL | default `NOW()` |
| `source_run_id` | UUID FK? | → `source_runs.id` SET NULL (added in 0006) |

V1 keeps the payload in the DB. V2 offloads to S3; the column will
become a `payload_ref`.

Index: `(resource_id, observed_at DESC)`, `(source, observed_at DESC)`,
and `(source_run_id) WHERE source_run_id IS NOT NULL`.

### `facts`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL | |
| `resource_id` | UUID? FK | → `resources.id` CASCADE. null for account-scoped facts. |
| `account_id` | UUID? FK | → `accounts.id` CASCADE. null for resource-scoped facts. |
| `namespace` | TEXT NOT NULL | `aws.rds`, `catalog.postgres`, … |
| `key` | TEXT NOT NULL | within the namespace, e.g. `engine` |
| `value` | JSONB | the actual value (string, number, boolean, null) |
| `value_state` | TEXT NOT NULL | CHECK in `('KNOWN','UNKNOWN','STALE','ERROR')` |
| `source` | TEXT NOT NULL | the source system (`aws_rds`, `focus`, …) |
| `observed_at` | TIMESTAMPTZ NOT NULL | when the source was queried |
| `computed_at` | TIMESTAMPTZ NOT NULL | when the fact row was last written |
| `last_source_run_id` | UUID? FK | → `source_runs.id` SET NULL (added in 0006) |

CHECK: `resource_id IS NOT NULL OR account_id IS NOT NULL` — a fact
must have at least one scope.

UNIQUE: `(tenant_id, resource_id, namespace, key, source)` — current
state. *No* `observed_at` in the unique key. (The previous
`uq_fact_snapshot` with `observed_at` was an append-log mistake;
migration 0006 corrected it, and the ORM matches the migration.)

Index: `(resource_id, namespace, key) WHERE resource_id IS NOT NULL`,
`(account_id, namespace, key) WHERE account_id IS NOT NULL`,
`(observed_at DESC)`.

### `focus_charges`

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | BigSerial on Postgres, Integer on sqlite |
| `tenant_id` | UUID NOT NULL | |
| `account_id` | UUID NOT NULL FK | → `accounts.id` CASCADE |
| `period_start` | DATE NOT NULL | FOCUS `ChargePeriodStart` |
| `period_end` | DATE NOT NULL | FOCUS `ChargePeriodEnd` |
| `service` | TEXT NOT NULL | FOCUS `ServiceName` (e.g. `AmazonRDS`) |
| `region` | TEXT? | FOCUS `Region` |
| `pricing_category` | TEXT? | FOCUS `PricingCategory` (`On-Demand`, `Reserved`, `Savings Plan`, …) |
| `billed_cost` | NUMERIC(18, 6) NOT NULL | FOCUS `BilledCost` |
| `amortized_cost` | NUMERIC(18, 6) NOT NULL | FOCUS `EffectiveCost` (FOCUS 1.0: this is the amortized metric) |
| `charge_count` | INT NOT NULL | number of raw FOCUS rows aggregated |
| `resource_id` | TEXT? | FOCUS `ResourceId` (added 0003) |
| `sub_account_id` | TEXT? | FOCUS `SubAccountId` (added 0003) |
| `ingested_at` | TIMESTAMPTZ NOT NULL | default `NOW()` |

**FOCUS 1.0 conformance note** (FOCUS 0.5 → 1.0): the column that
*used to be* `amortized_cost` in 0.5 is named `EffectiveCost` in 1.0.
We map FOCUS `EffectiveCost` → our `amortized_cost` column. There is
no separate `effective_cost` column.

Index: `(account_id, period_start)`, `(service, period_start)`,
`(resource_id) WHERE resource_id IS NOT NULL`,
`(sub_account_id) WHERE sub_account_id IS NOT NULL`.

### `focus_charge_tags`

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | |
| `tenant_id` | UUID NOT NULL | |
| `focus_charge_id` | BIGINT NOT NULL FK | → `focus_charges.id` CASCADE |
| `key` | TEXT NOT NULL | tag key, e.g. `Application` |
| `value` | TEXT NOT NULL | tag value, e.g. `web` |

One row per (focus_charge, key, value), **once per contributing FOCUS
input row** — deliberately no UNIQUE constraint: the row count for a
(key, value) IS the weight that drives the chargeback rule's
proportional attribution (replaces the V1 even-split approximation).
Index: `(focus_charge_id)`, `(key, value)`.

### `insights`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL | |
| `rule_name` | TEXT NOT NULL | `rds_eol`, `chargeback`, … |
| `resource_id` | UUID? FK | → `resources.id` CASCADE. null for account-scoped. |
| `account_id` | UUID? FK | → `accounts.id` CASCADE. null for resource-scoped. |
| `severity` | TEXT NOT NULL | CHECK in `('info', 'warning', 'critical')` |
| `title` | TEXT NOT NULL | one-line summary for the UI |
| `payload` | JSONB NOT NULL | the evidence (see per-insight specs) |
| `computed_at` | TIMESTAMPTZ NOT NULL | default `NOW()` |
| `ack_status` | TEXT? | operator triage state (added `0013`): NULL = open, or `acknowledged` / `in_progress` / `resolved` / `dismissed` |
| `ack_at` | TIMESTAMPTZ? | when ack_status was last set (server-set on PATCH) |
| `ack_by` | TEXT? | free-form operator identifier (email, team, bot) |

CHECK: `resource_id IS NOT NULL OR account_id IS NOT NULL`.

Index: `(rule_name, computed_at DESC)`, `(severity, computed_at DESC)`,
`(account_id, computed_at DESC) WHERE account_id IS NOT NULL`, and a
partial `(severity, computed_at DESC) WHERE ack_status IS NULL` for the
inbox query ("open critical insights").

### `inconclusive`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL | |
| `rule_name` | TEXT NOT NULL | |
| `resource_id` | UUID? FK | null for account-scoped (none in V1) |
| `account_id` | UUID? FK | null for resource-scoped |
| `missing_facts` | JSONB NOT NULL | `list[str]`, e.g. `["aws.rds.vcpu"]` |
| `reason` | TEXT | free text, e.g. `scope_not_proven`, `missing_facts`, `<no facts>` |
| `computed_at` | TIMESTAMPTZ NOT NULL | default `NOW()` |

Same scope CHECK as `insights`.

### `source_runs`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL | |
| `account_id` | UUID NOT NULL FK | → `accounts.id` CASCADE |
| `region` | TEXT NOT NULL | |
| `resource_type` | TEXT NOT NULL | e.g. `AWS::RDS::DBInstance` |
| `source` | TEXT NOT NULL | e.g. `aws_rds` |
| `started_at` | TIMESTAMPTZ NOT NULL | default `NOW()` |
| `finished_at` | TIMESTAMPTZ? | null while running |
| `status` | TEXT NOT NULL | CHECK in `('running', 'success', 'failed', 'partial')` |
| `resources_found` | INT? | count of resources seen in this run |
| `error` | TEXT? | error message if status != success |

Partial unique index `uq_source_run_active` on
`(account_id, region, resource_type, source) WHERE status = 'running'`:
only one active run per scope. Multiple completed runs coexist.

### `insight_runs`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL | |
| `rule_name` | TEXT NOT NULL | |
| `started_at` | TIMESTAMPTZ NOT NULL | default `NOW()` |
| `finished_at` | TIMESTAMPTZ? | |
| `status` | TEXT NOT NULL | CHECK in `('running', 'success', 'failed', 'partial')` |
| `resources_scanned` | INT? | |
| `insights_emitted` | INT? | |
| `error` | TEXT? | |

`insight_runs` and `source_runs` are siblings — one audits the rule
execution, the other audits the data collection.

### `audit_events`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL | |
| `occurred_at` | TIMESTAMPTZ NOT NULL | default `NOW()` |
| `actor` | TEXT NOT NULL | `api_key:<id_hash>` or `system:<job_name>` — never the raw key |
| `action` | TEXT NOT NULL | e.g. `aws_scan_completed` |
| `target_type` | TEXT? | |
| `target_id` | TEXT? | |
| `metadata` | JSONB NOT NULL | counts, durations, rule names — never PII |

Append-only by convention (no UPDATE/DELETE in application code);
`0014` adds a trigger enforcing immutability. The "who did what when"
log for the DORA / ISO 27001 questionnaire. Index: `(tenant_id,
occurred_at DESC)`, `(actor)`, `(action)`.

### `retention_policies`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL | |
| `table_name` | TEXT NOT NULL | free string; validated against an allow-list by the runner |
| `retention_days` | INT NOT NULL | CHECK `>= 0` |
| `enabled` | BOOLEAN NOT NULL | default `TRUE` |
| `last_applied_at` | TIMESTAMPTZ? | |
| `last_deleted_count` | INT? | |
| `updated_at` | TIMESTAMPTZ NOT NULL | default `NOW()` |

UNIQUE `(tenant_id, table_name)`. Declarative "delete N days after
creation" per table — the GDPR / SOC2 proof that deletion happens
automatically, on a schedule.

### `pii_classifications`

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | |
| `tenant_id` | UUID NOT NULL | |
| `resource_type` | TEXT NOT NULL | `account`, `resource`, `focus_charge`, `tag` |
| `resource_id` | TEXT NOT NULL | |
| `field_name` | TEXT NOT NULL | `account_id`, `arn`, `tag:Application`, … |
| `sensitivity` | TEXT NOT NULL | CHECK in `('public', 'internal', 'confidential', 'restricted')` |
| `value_hash` | TEXT NOT NULL | SHA-256 hex — duplicates detectable without storing the PII |
| `classified_at` | TIMESTAMPTZ NOT NULL | default `NOW()` |

Written by the AWS collector at ingest time. Answers the privacy
questionnaire's "where does customer PII live and how is it
classified?". Index: `(tenant_id, resource_type, resource_id)`,
`(tenant_id, sensitivity)`.

## Invariants you must respect

When you write a new code path that touches a table, respect the
following. The tests pin them; the migrations enforce them at the
schema level where possible.

1. **Every tenant-scoped table has `tenant_id NOT NULL`.** No new
   table without it.
2. **`UNIQUE` on `facts` does not include `observed_at`.** Current
   state, not append log.
3. **A fact must have a scope.** `resource_id IS NOT NULL OR
   account_id IS NOT NULL`. The DB rejects a fact without one.
4. **An insight must have a scope.** Same CHECK. An "orphan" insight
   with no resource and no account is a bug.
5. **A `source_runs` row is created *before* writing facts for the
   same scope.** The collector opens the run, writes facts, then
   closes the run. Closing before opening → race. (The
   `_is_scope_proven` check in the runner is the contract test.)
6. **`retired_at` is set by the collector, only after two consecutive
   successful scans both missed the resource**
   (`CONSECUTIVE_SCANS_FOR_RETIREMENT`). One missed scan proves nothing
   (the resource may have been briefly invisible); two consecutive
   successful scans of the same scope prove the absence. Never set
   `retired_at` from a failed or partial scan.
7. **`observations.payload` is the source-of-truth payload.** Don't
   filter fields in the collector; allowlist at read time if needed.
   The historical record must be reproducible.

## Adding a new table (V1: don't; V2: here's the shape)

If you need a new table, the V1 hygiene is:

- `tenant_id UUID NOT NULL` with an index
- UUID PK (or `BIGSERIAL` if it's a high-volume fact table)
- FK to `accounts.id` (or to the right parent) with `ON DELETE CASCADE`
  where deletion should propagate
- For each CHECK constraint, a regression test in `tests/`
- For each UNIQUE constraint, a doc note on the "current-state vs
  append-log" choice (default: current-state)

Add the SQL migration in `db/migrations/NNNN_<scope>.sql`. Then add
the ORM class in `apps/api/src/constat_api/orm.py`. Then add a
repository in `apps/api/src/constat_api/repositories/`.

## See also

- [`concepts.md`](./concepts.md) — the 9 concepts
- [`development/known-issues.md`](./development/known-issues.md) — the
  ORM/SQL drift
