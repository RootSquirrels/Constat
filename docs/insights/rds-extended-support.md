# `rds_eol` — RDS PostgreSQL Extended Support

> **The V1 hero insight.** This is what the customer sees first, what
> the demo leads with, and what the catalog's correctness is judged on.
> The GTM promise — "3 RDS PG11 instances on db.m5.xlarge, that's
> ~$580/month you're paying AWS for the privilege of staying" — comes
> from this rule.

This document is the contract for the rule. Code:
- Resolver: `packages/insights/rds_eol/src/constat_rds_eol/resolver.py`
- Runner: `apps/api/src/constat_api/insights/runner.py::run_rds_eol`
- Catalog: `packages/core/src/constat_core/catalog/aws.py`
- Collector: `packages/connectors/aws_rds/src/constat_aws_rds/collector.py`

## What the rule proves

> *"Your RDS PostgreSQL instance is paying AWS Extended Support
> licence fees. Here is the engine, the version, the instance class,
> the vCPU count, the pricing tier, and the monthly cost in dollars."*

A customer asking "are we paying for Extended Support?" gets:

- A list of every RDS PG instance that is paying the fee (the
  **Insight**), grouped by engine major version.
- A list of every RDS PG instance we *could not* evaluate, with the
  exact fact that was missing (the **Inconclusive**).
- No `false` negatives. If the engine is a non-PostgreSQL one
  (MySQL, MariaDB, Aurora, …) we emit **nothing** — that's a
  definitive NO_MATCH, not a missing conclusion.

The rule never claims a cost it cannot defend. The catalog dates and
the vCPU map are the proof; the resolver cites both in the payload.

## The data flow

```
boto3 DescribeDBInstances (paginated per region)
        │
        ▼
constat_aws_rds.collector.db_to_facts(...)
  → facts: aws.rds.engine, aws.rds.engine_version,
            aws.rds.instance_class, aws.rds.vcpu
        │
        ▼
  facts table (current state, UNIQUE per (tenant, resource, ns, key, source))
        │
        ▼
runner.run_rds_eol()
  → for each resource:
      1. source_runs_repo.latest_successful_run(scope)
         ↳ if missing → emit Inconclusive(reason='scope_not_proven'), continue
      2. facts_repo.list_facts_for_resource(resource_id)
         ↳ if empty → emit Inconclusive(reason='<no facts>'), continue
      3. rds_eol_evaluate(resource_id, facts, today)
         ↳ see resolver below
        │
        ▼
insights table  |  inconclusive table  |  insight_runs table
```

## The resolver contract

The resolver is a pure function: `(resource_id, facts, today) →
InsightResult`. It has no DB or HTTP dependency; the runner is
responsible for fetching and persisting.

```python
def evaluate(
    resource_id: UUID,
    facts: Iterable[Fact],
    *,
    today: date | None = None,
) -> InsightResult:
    ...
```

`InsightResult` carries:

- `insights: list[Insight]` — the gaps (0 or 1 in V1)
- `inconclusive_reasons: list[str]` — the missing facts
- `is_conclusive: bool` — true iff `inconclusive_reasons` is empty
- `has_gap: bool` — true iff `insights` is non-empty

The three states:

| State | When | Table written |
|---|---|---|
| **MATCH** (gap found) | The instance is in Extended Support or within 90 days of EOL, AND all required facts are present | `insights` |
| **NO_MATCH** (definitive no gap) | The instance is a non-PostgreSQL engine, OR the engine is a known-LTS version (16+), OR the engine is more than 90 days from EOL | (nothing) |
| **INCONCLUSIVE** (cannot conclude) | A required fact is missing, UNKNOWN, ERROR, or the scope has no successful `source_run` | `inconclusive` |

The runner writes the table; the resolver returns the contract.

## The gates (3 facts, in order)

A resource only enters the catalog lookup if all 3 gates pass. The
gates are short-circuited: the first missing fact stops the
evaluation and the missing fact is added to the inconclusive list.

1. **`aws.rds.engine`** must be `KNOWN` and equal to `"postgres"`.
   - If missing or `UNKNOWN`: inconclusive with `aws.rds.engine`.
   - If `KNOWN` and != `"postgres"`: **NO_MATCH** (definitive —
     MySQL/MariaDB/Aurora etc. are out of scope for this rule).

2. **`aws.rds.engine_version`** must be `KNOWN`. The major version
   (`"14.7"` → `14`) is parsed from the string.
   - If missing or `UNKNOWN`: inconclusive.
   - If malformed: inconclusive with `aws.rds.engine_version.malformed`.

3. **`aws.rds.vcpu`** must be `KNOWN`. vCPU is computed from the
   instance class via the catalog table.
   - If missing or `UNKNOWN`: inconclusive. The most common cause
     is an instance class not in the catalog (e.g. a new Graviton
     class AWS published and we haven't mirrored yet — see
     [known issues](../development/known-issues.md) for the catalog
     review checklist).

If all 3 pass, the resolver looks up the catalog. If the version is
LTS (16+ in V1, not in the catalog), the resolver returns NO_MATCH.

## The catalog (the moat)

`packages/core/src/constat_core/catalog/aws.py` is the dated
reference data the resolver consumes. It is a `frozen=True` data
structure, versioned by date in the module docstring.

### EOL dates and pricing

Per the AWS RDS PostgreSQL release calendar (verified 2026-07-18):

| Major | RDS end of standard support | Year 1-2 rate ($/vCPU-h) | Year 3+ rate ($/vCPU-h) | AWS force-upgrade (end of extended support) |
|---:|---|---:|---:|---|
| 11 | 2024-02-29 | 0.10 | 0.20 | 2027-03-31 |
| 12 | 2025-02-28 | 0.10 | 0.20 | 2028-02-29 |
| 13 | 2026-02-28 | 0.10 | 0.20 | 2029-02-28 |
| 14 | 2027-02-28 | 0.10 | 0.20 | 2030-02-28 |
| 15 | 2028-02-29 | 0.10 | 0.20 | 2031-02-28 |
| 16+ | LTS as of 2026-07 (no entry in catalog) | — | — | — |

**Two-tier pricing.** Years 1-2 past EOL = $0.10/vCPU-h. Year 3+ =
$0.20/vCPU-h. The tier transition is computed via
`extended_support_tier(eol_date, today)`:

```python
days_since = (today - eol_date).days
if days_since < 730:    # ~2 * 365
    return "year_1_2"
return "year_3_plus"
```

A flat 0.20/vCPU-h rate is a **2× overestimate** for PG 12 and PG 13
in year 1-2. Don't do that. The catalog encodes the tier.

### vCPU per instance class

`RDS_INSTANCE_VCPU` maps `db.<class>` to vCPU count. Includes
Graviton (`db.t4g`, `db.m6g`, `db.m7g`, `db.r6g`, `db.r7g`) — the
Graviton families that dominate recent fleets. Without these, the
insight silently disappears for Graviton customers.

Approximate coverage (see the file for the full table):

| Family | Classes |
|---|---|
| T burstable | `t3.{micro,small,medium,large,xlarge,2xlarge}`, `t4g.{micro,small,medium,large,xlarge,2xlarge}` |
| M general purpose (Intel) | `m5.{large..24xlarge}`, `m6i.{large..24xlarge}` |
| M general purpose (Graviton) | `m6g.{large..24xlarge}`, `m7g.{large..24xlarge}` |
| R memory-optimized (Intel) | `r5.{large..24xlarge}`, `r6i.{large..24xlarge}` |
| R memory-optimized (Graviton) | `r6g.{large..24xlarge}`, `r7g.{large..24xlarge}` |

A new instance class AWS publishes (e.g. `db.m8g.large`) is the
single most likely way this insight silently breaks. **Catalog review
is the operational responsibility** of the package owner. See the
[checklist at the end of this doc](#catalog-review-checklist).

## What the resolver emits

When a MATCH happens, the insight payload is:

```json
{
  "engine_version": "11.22",
  "major_version": 11,
  "eol_date": "2024-02-29",
  "end_of_extended_support": "2027-03-31",
  "days_to_event": 0,
  "pricing_tier": "year_3_plus",
  "pricing_usd_per_vcpu_hour": 0.20,
  "pricing_tier_label": "year_3_plus",
  "recommendation": "Upgrade to PostgreSQL 12 LTS now to stop Extended Support fees"
}
```

The monthly cost is **not** in the payload. The customer computes it:

```
monthly_cost_usd = pricing_usd_per_vcpu_hour * vcpu * 730
```

For `db.m5.xlarge` (4 vCPU) on PG 11 in year 3+: `0.20 * 4 * 730 = $584`.

For `db.m5.xlarge` (4 vCPU) on PG 12 in year 1-2: `0.10 * 4 * 730 = $292`.

The customer-visible cost is in the **title** rendered by the web
app's `InsightCard` (or in a future detail page that joins
`payload` + `aws.rds.vcpu`). V1 surfaces the inputs; the customer
does the multiplication. When we build the cost column in V2, this
becomes a derived fact.

## Severity matrix

The resolver maps the timeline to severity:

| Condition | Severity | Title pattern | Recommendation |
|---|---|---|---|
| `today > end_of_extended_support` | `CRITICAL` | "RDS PostgreSQL {N} will be force-upgraded in {D} days" | "AWS will force-upgrade to {N+1} on {date}. Upgrade manually now to control timing." |
| `0 < days_to_eol ≤ 90` | `WARNING` | "RDS PostgreSQL {N} reaches EOL in {D} days" | "Plan upgrade to PostgreSQL {N+1} LTS before {date}" |
| `days_to_eol ≤ 0` (in Extended Support) | `CRITICAL` | "RDS PostgreSQL {N} is in Extended Support" | "Upgrade to PostgreSQL {N+1} LTS now to stop Extended Support fees" |
| `days_to_eol > 90` | (no insight) | — | Roadmap item, not an écart |

`EOL_ALERT_WINDOW_DAYS = 90` in
`packages/insights/rds_eol/src/constat_rds_eol/resolver.py`. A
version 6 months from EOL is a roadmap item, not a *gap*.

## Scope proof — the inventory-first promise

`_is_scope_proven` in the runner gates every evaluation:

```python
def _is_scope_proven(session, resource):
    run = source_runs_repo.latest_successful_run(
        session,
        account_id=resource.account_id,
        region=resource.region,
        resource_type=resource.resource_type,
        source=DEFAULT_SOURCE,  # "aws_rds"
    )
    return run is not None
```

If no successful `source_run` exists for the resource's scope, the
runner emits an **Inconclusive** with `reason='scope_not_proven'`
*before* even reading facts. We do not claim MATCH or NO_MATCH for a
resource whose scope we haven't scanned — that would be a silent
false (criterion n°15).

The partial unique index `uq_source_run_active` on
`source_runs(..., source) WHERE status='running'` ensures a scope
has at most one running scan at a time.

## Determinism and the `today` knob

The resolver takes a `today: date | None` parameter. Default is
`date.today()`. Override via:

```python
runner.run_rds_eol(session, today=date(2026, 7, 18))
```

or via the HTTP/CLI:

```bash
python -m constat_api.cli.run_insights --rule rds_eol --today 2026-07-18
```

or:

```bash
curl -X POST 'http://localhost:8000/insights/run?today=2026-07-18' \
  -H 'Content-Type: application/json' \
  -d '{"rule": "rds_eol"}'
```

Determinism is required for the test suite. See
`tests/test_rds_eol.py` — every test pins `today`.

## Tests that pin the contract

| File | What it pins |
|---|---|
| `tests/test_rds_eol.py` | The 3 gates, NO_MATCH for non-postgres, NO_MATCH for LTS, MATCH for EOL+EXT, the pricing tier transition, the severity matrix |
| `tests/test_aws_collector.py` | The collector produces `aws.rds.*` facts from `DescribeDBInstances` |
| `tests/test_runner.py` | The runner emits `Inconclusive(reason='scope_not_proven')` when no successful run, and `Inconclusive(reason='missing_facts')` when facts are absent |
| `tests/test_repositories.py` | The `facts_repo.upsert_facts` is current-state (one row per identity, regardless of `observed_at`) — *but the ORM constraint is wrong* (see [known issues](../development/known-issues.md)) |

## Catalog review checklist

When AWS publishes changes (monthly cadence is fine for V1, but
follow the AWS RSS for RDS announcements):

1. **New EOL date.** Add a `PostgresEOLInfo` entry in
   `POSTGRES_EOL` with the date, the tiered rates, and the
   force-upgrade date. The tier rates haven't changed; copy the
   structure from the existing entries.
2. **New instance class.** Add the vCPU count to `RDS_INSTANCE_VCPU`.
   Cross-check on the AWS instance-types page; Graviton is most
   likely to add new classes (`db.t4g`, `db.m7g`, `db.r7g`).
3. **Re-run the test suite.** `uv run pytest -v`. If a test breaks
   on a date assumption, pin the test with `--today` and update the
   comment to reference the AWS announcement URL.
4. **Bump the `Last reviewed:` line in the catalog file's
   docstring.** This is the audit trail; the customer-facing
   "freshness" of the catalog.
5. **PR description** should reference the AWS announcement. One
   sentence on what changed and why.

The catalog is a one-line review when nothing changes, and a 30-line
review when AWS publishes a new EOL or a new instance family. Doing
it monthly is the operational bar.

## What's missing in V1 (deliberate)

- **No `false`-positive guard for already-upgraded-to-LTS versions.**
  We assume AWS doesn't downgrade. The catalog has no
  "downgraded to N" entry, so a `14.7` instance is always evaluated
  as 14. If a customer snapshots an old major version and restores
  it onto a new account, we still evaluate correctly.
- **No Multi-AZ / read-replica cost multiplier.** A Multi-AZ
  standby doubles the licence cost in real life. V1 reports the
  per-instance cost; the customer can multiply by the deployment
  shape. V2: add `aws.rds.multi_az` and `aws.rds.read_replica_count`
  to the catalog lookup.
- **No Aurora PostgreSQL handling.** Aurora's EOL and pricing model
  is different. V2: separate rule.
- **No `db.r8g` (next-gen Graviton) handling.** Will appear in the
  catalog when AWS publishes the vCPU table.
- **No "before 11" handling.** PG 10 and earlier are out of
  standard support entirely; AWS may already have force-upgraded
  them. We do not emit insights for them in V1 (they would emit
  `Inconclusive(reason='aws.rds.engine_version.malformed')` because
  the major version is not in `POSTGRES_EOL`). V2: handle explicitly.

## See also

- [`chargeback.md`](./chargeback.md) — the second V1 insight
- [`../concepts.md`](../concepts.md) — the 9 concepts
- [`../data-model.md`](../data-model.md) — the `facts` and
  `source_runs` tables
- [`../development/known-issues.md`](../development/known-issues.md) —
  the ORM/migration drift, and other traps
