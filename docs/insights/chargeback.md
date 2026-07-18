# `chargeback` — FOCUS-derived per-account cost drift

> **The second V1 insight.** A FOCUS export → an aggregated per-account
> × service × period cost row → an insight that quantifies the
> *amortized vs billed* drift. The customer sees the monthly cost
> structure per account, per service, with the dollar gap between
> what they paid (`billed_cost`) and what AWS amortized against
> them (`amortized_cost`).

This document is the contract for the rule. Code:
- Resolver: `packages/insights/chargeback/src/constat_chargeback/resolver.py`
- Runner: `apps/api/src/constat_api/insights/runner.py::run_chargeback`
- FOCUS loader: `packages/connectors/focus/src/constat_focus/loader.py`
- FOCUS aggregator: `packages/connectors/focus/src/constat_focus/aggregator.py`
- Storage repo: `apps/api/src/constat_api/repositories/focus_charges.py`
- CLI: `apps/api/src/constat_api/cli/focus.py`

> **Work-in-progress note.** The aggregation contract is being
> iterated (per-period aggregation, tag-based grouping in V2). This
> doc describes the current V1 contract. The resolver is the source
> of truth — when in doubt, read the code.

## What the rule proves

> *"Here is how much each of your accounts spent on each service
> per billing period, in both billed (cash) and amortized
> (effective) terms. The difference tells you whether Reserved
> Instances and Savings Plans are pulling their weight, or whether
> you're leaving money on the table."*

The customer's first question is "how much am I spending?". The
second is "where?". The third is "am I over- or under-amortized?".
The `chargeback` insight answers the first three at once, scoped
to whatever FOCUS data the customer has loaded.

The rule does **not** claim to be a FinOps showback (no tag-based
allocation in V1) and does **not** recommend RI/SP purchases. The
drift is a *signal*, not an *action*.

## Why FOCUS and not the AWS Cost Explorer API

FOCUS 1.0 (FinOps Open Cost & Usage Specification) is the
specification the FinOps Foundation publishes to normalize billing
data across cloud providers. In V1 we ingest FOCUS 1.0 CSV exports
from AWS Cost and Usage Reports (CUR) — the customer enables CUR
with FOCUS columns, runs a query, exports the CSV, and we ingest it.

Why CSV and not the API? Because:

1. CUR is the cheapest, most reliable, customer-controlled export.
2. FOCUS 1.0 has all the columns V1 needs (EffectiveCost,
   PricingCategory, ResourceId, SubAccountId, Region, etc.).
3. The 11 required columns are a stable contract — we validate them
   up front in
   `packages/connectors/focus/src/constat_focus/loader.py::FOCUS_REQUIRED_COLUMNS`.
4. The customer can replay an export if they think our totals are
   wrong. With an API, the answer is "trust us".

V2: same FOCUS path, but with multi-source ingestion (CUR + Athena
+ S3 raw), and a `cost_facts` table that supports incremental loads.

## FOCUS 1.0 conformance

The loader is FOCUS 1.0 conformant. Two specific points:

1. **No `AmortizedCost` column.** FOCUS 1.0 renames the
   pre-1.0 `AmortizedCost` to `EffectiveCost`. Our `amortized_cost`
   column maps to FOCUS `EffectiveCost`. (The early code had an
   `effective_cost` column too; migration 0003 dropped it. Don't
   add it back.)
2. **`ResourceId` and `SubAccountId` are required-by-spec** for
   cost-to-resource attribution. We capture them in
   `focus_charges.resource_id` and `focus_charges.sub_account_id`
   (migration 0003). The FOCUS loader's required-columns list
   fails-loud on a CSV missing them.

## The data flow

```
FOCUS 1.0 CSV (11 required columns)
        │
        ▼
constat_focus.loader.load_focus_csv(path)
  → FocusCharge (per row, raw, in memory)
        │
        ▼
constat_focus.aggregator.aggregate_for_storage(charges)
  → AggregatedFocusCharge (1 per (account, service, period))
        │
        ▼
apps/api/repositories/focus_charges.upsert_aggregated(...)
  → focus_charges table (1 row per (account, service, period))
        │
        ▼
apps/api/insights/runner.run_chargeback(session, period_label)
  → for each distinct account in focus_charges:
      charges = SELECT * FROM focus_charges WHERE account_id = ?
      aggregated = aggregate_by_period(charges)
      insights = build_insights(aggregated, period_label)
      INSERT INTO insights ...
```

Two aggregations happen, at different levels:

| Layer | Grouping | When |
|---|---|---|
| Loader-side (`aggregate_for_storage`) | `(service, period_start, period_end)` for one account | On CSV ingest, in-memory, before writing to DB |
| Resolver-side (`aggregate_by_period`) | `(account_id, service, period_start, period_end)` across all of an account's focus_charges | On rule run, in-memory, before writing to `insights` |

The loader-side aggregation collapses the FOCUS row stream into one
row per (service, period) per account — handling the "many FOCUS
rows per service per period" reality (one per resource, one per
pricing dimension, etc.). The resolver-side re-aggregation is a
no-op given the loader's output, but it makes the resolver testable
in isolation (the resolver can be fed `FocusCharge` instances
without the DB).

## The resolver contract

The resolver is pure: `(charges) → list[Insight]`. It depends on
`FocusCharge` (from the FOCUS loader) and `Insight` (from core
models). It has no DB or HTTP dependency.

```python
def aggregate_by_period(charges) -> list[AggregatedCost]: ...
def build_insights(aggregated, *, period_label: str = "") -> list[Insight]: ...
```

The runner fetches `FocusCharge` rows from the DB (converting ORM →
Pydantic), calls the resolver, persists the insights, and writes
the `insight_runs` audit row.

### `AggregatedCost`

```python
@dataclass(frozen=True)
class AggregatedCost:
    account_id: str
    service: str
    billed_cost: Decimal
    amortized_cost: Decimal
    charge_count: int
    period_start: date | None = None
    period_end: date | None = None
    tags: dict[str, str] = field(default_factory=dict)  # V2
```

The `tags` field is empty in V1. V2 will populate it from
`focus_charges.resource_id` joined with `resources` (if the resource
has tags).

### `drift_amortized_minus_billed`

```python
@property
def drift_amortized_minus_billed(self) -> Decimal:
    return self.amortized_cost - self.billed_cost
```

A positive drift means the customer is being **amortized up** (AWS
is allocating more of an RI/SP's cost to them than the cash outlay
in this period). A negative drift means **amortized down** (the
opposite — refunds, credits, one-time fee amortized to zero).

Neither direction is "good" or "bad" on its own. It's a signal to
interpret in context.

## Severity thresholds

The resolver classifies drift into severity:

| Absolute drift | Severity |
|---:|---|
| ≥ $1,000 | `CRITICAL` |
| ≥ $100 | `WARNING` |
| < $100 | `INFO` |

Defined as constants in
`packages/insights/chargeback/src/constat_chargeback/resolver.py`:

```python
SEVERITY_WARNING_USD = Decimal("100")
SEVERITY_CRITICAL_USD = Decimal("1000")
```

These are **tunable** and will be calibrated against the first G0
pilot data. If the customer has 10 services × 5 accounts × 12
months of FOCUS data and most rows are below $100, the
thresholds are too noisy. If everything trips CRITICAL, they're
too lax. The first pilot run calibrates them.

The thresholds are in code, not config. We don't have a config
file in V1. When the calibration stabilizes, we move them to a
`settings.py::CHARGEBACK_THRESHOLDS` (still no config file, still
in repo).

## What the resolver emits

For each `AggregatedCost` row, one `Insight` with this payload:

```json
{
  "service": "AmazonRDS",
  "account_id": "111111111111",
  "period_label": "2026-07-01 → 2026-07-31",
  "period_start": "2026-07-01",
  "period_end": "2026-07-31",
  "billed_cost_usd": 1234.56,
  "amortized_cost_usd": 1180.00,
  "drift_amortized_minus_billed_usd": -54.56,
  "charge_count": 12,
  "tags": {}
}
```

The title is human-readable:

```
AmazonRDS on 111111111111 (2026-07-01 → 2026-07-31): amortized down by $54.56
```

The direction is `up` / `down` / `flat`. The dollar amount is the
absolute drift.

## What the resolver does NOT do (V1)

- **No `INCONCLUSIVE` branch.** FOCUS data is "complete by
  ingestion" — the customer provided the file, so the rule
  assumes completeness. The `insight_runs.inconclusive_emitted`
  count is always 0 for the `chargeback` rule. V2 will add an
  INCONCLUSIVE branch for missing periods (e.g. "you loaded July
  but not August — here's what we can't say").
- **No `false` deductions.** Drift is computed on whatever FOCUS
  rows are present. If a service has no rows, no insight is
  emitted.
- **No scope proof check.** Unlike `rds_eol`, the runner does not
  look at `source_runs`. FOCUS is the user's data; the runner
  trusts the user's ingest.
- **No allocation of shared costs (NAT, data transfer, support).**
  FOCUS exports them at the account level, not the resource level.
  V1 surfaces them at the account level. The customer filters
  `service` to see them.
- **Showback across teams is supported via `tag_key`.** V1 emits
  per (account, service, period, tag_value) when `tag_key` is set
  (e.g. `tag_key="Application"`). Charges without a tag for the
  key go to `__untagged__`.
- **No Reserved Instance / Savings Plan recommendations.** The
  drift is a signal; we don't act on it.

## The runner

```python
def run_chargeback(session: Session, *, period_label: str = "all-time") -> RunResult:
    run = InsightRunORM(tenant_id=DEFAULT_TENANT_ID, rule_name="chargeback", status="running")
    session.add(run); session.commit()

    account_ids = {row[0] for row in session.query(FocusChargeORM.account_id).distinct().all()}
    insights_emitted = 0
    errors: list[str] = []

    for account_id in account_ids:
        try:
            orm_charges = session.query(FocusChargeORM).filter(...).all()
            charges = [_focus_charge_to_pydantic(c) for c in orm_charges]
            aggregated = aggregate_by_period(charges)
            insights = build_insights(aggregated, period_label=period_label)
            for insight in insights:
                insights_repo.insert_insight(session, insight)
                insights_emitted += 1
        except Exception as exc:
            errors.append(f"account {account_id}: {exc}")

    run.finished_at = datetime.now(tz=UTC)
    run.status = "success" if not errors else "partial"
    run.resources_scanned = len(account_ids)
    run.insights_emitted = insights_emitted
    session.commit()
    ...
```

The `period_label` is stored verbatim in the insight payload. It
is the customer's way of saying "this run is the July view".

## CLI

```bash
# One account, one period (the typical V1 ingest)
python -m constat_api.cli.focus --account 111111111111 --file focus-july.csv  # CSV
python -m constat_api.cli.focus --account 111111111111 --file focus-july.parquet  # Parquet

# All accounts with FOCUS data, default period_label "all-time"
python -m constat_api.cli.run_insights --rule chargeback

# Tagged with the period the customer ran the rule for
python -m constat_api.cli.run_insights --rule chargeback --period-label "2026-07"

# Per-tag breakdown (the typical DAF question: "how much per Application?")
python -m constat_api.cli.run_insights --rule chargeback --tag-key Application
```

## HTTP

```bash
curl -X POST 'http://localhost:8000/insights/run' \
  -H 'Content-Type: application/json' \
  -d '{"rule": "chargeback", "period_label": "2026-07"}'

# Tag-based
curl -X POST 'http://localhost:8000/insights/run' \
  -H 'Content-Type: application/json' \
  -d '{"rule": "chargeback", "tag_key": "Application"}'
```

The response is the same `RunResult` shape as `rds_eol`:

```json
{
  "rule_name": "chargeback",
  "resources_scanned": 5,
  "insights_emitted": 23,
  "inconclusive_emitted": 0,
  "errors": [],
  "period_label": "2026-07"
}
```

`resources_scanned` is the count of distinct accounts in
`focus_charges`. `insights_emitted` is the count of (account,
service, period) groups that produced a non-zero insight.

When `tag_key` is set, `insights_emitted` is the count of (account,
service, period, tag_value) groups instead, and `period_label` is
augmented to `"{label} tag_key={key}"` for traceability.

## Tag-based aggregation (V1)

The `chargeback` runner accepts an optional `tag_key` body field
(CLI flag `--tag-key`). When set, the rule re-aggregates the
FOCUS data by (account, service, period, tag_value), where
`tag_value` is the value of the requested tag on each input row.

**Storage:** `focus_charges.tags` is a JSONB column that stores the
list of unique tag dicts seen for each (account, service, period)
row. The list preserves per-row heterogeneity — when a (service,
period) bucket has 3 rows with `Application=web` and 1 row with
`Application=api`, the stored list is `[{web}, {api}]`, not
collapsed to a mode.

**Splitting:** when the runner re-aggregates by `tag_key="Application"`
and a stored row has 2 unique tag values, the row's cost is split
evenly (1/N) across the values. This is a V1 approximation — the
exact per-row tag data is lost in storage. V2 will move to per-row
tag storage and lift the split.

**Untagged bucket:** charges with no tag for the requested key
go to `__untagged__` with their full cost (no split).

## Tests that pin the contract

| File | What it pins |
|---|---|
| `tests/test_chargeback.py` | `aggregate_by_period` groups correctly; `build_insights` produces one Insight per `AggregatedCost`; severity thresholds trigger correctly; direction is correct |
| `tests/test_chargeback_runner.py` | The runner emits one insight per (account, service, period), `period_label` is in the payload, `insight_runs` is updated |
| `tests/test_focus_loader.py` | The loader validates the 11 required columns, parses dates, handles malformed rows gracefully |
| `tests/test_focus_aggregator.py` | `aggregate_for_storage` collapses per-(account, service, period); resource_id/sub_account_id collapsed via mode |
| `tests/test_focus_ingest.py` | The end-to-end ingest path (loader → aggregator → DB) |

## FOCUS 1.0 → Constat column mapping

| FOCUS 1.0 | Constat | Notes |
|---|---|---|
| `BillingAccountId` | `accounts.external_id` | AWS Organizations account ID |
| `BillingAccountName` | `accounts.name` | Optional friendly name |
| `ServiceName` | `focus_charges.service` | e.g. `AmazonRDS`, `AmazonEC2` |
| `ChargePeriodStart` | `focus_charges.period_start` | DATE |
| `ChargePeriodEnd` | `focus_charges.period_end` | DATE |
| `BilledCost` | `focus_charges.billed_cost` | NUMERIC(18, 6) |
| `EffectiveCost` | `focus_charges.amortized_cost` | The "amortized" cost in FOCUS 1.0 |
| `PricingCategory` | `focus_charges.pricing_category` | `On-Demand`, `Reserved`, `Savings Plan`, … |
| `Region` | `focus_charges.region` | nullable |
| `ResourceId` | `focus_charges.resource_id` | nullable; for cost-to-resource attribution |
| `SubAccountId` | `focus_charges.sub_account_id` | nullable; for multi-account customers |

## Catalog review checklist (when FOCUS or AWS changes)

1. **FOCUS spec update.** FinOps Foundation publishes v1.x
   changes. Validate the loader's required-columns list is still
   correct. Most updates are additive (new columns); the loader
   ignores unknown columns. Don't add new required columns
   without a parser for them.
2. **AWS adds a new pricing category.** Update the
   `PricingCategory` documentation in this file. No code change
   unless we want to filter on it.
3. **FOCUS renames a column.** Major version bump. Open an issue,
   do not silently rename.

## What's missing in V1 (deliberate)

- **Tag-based grouping.** V2. The `tags` field is in
  `AggregatedCost` already; the runner doesn't populate it yet.
- **INCONCLUSIVE branch for missing periods.** V2. Today, "I loaded
  July but not August" is invisible.
- **Multi-source cost facts (CUR + Athena).** V2. The FOCUS CSV
  path is the only source in V1.
- **Allocation of shared costs.** V2+. Documented as a hard
  problem; out of V1.
- **Recommendations.** "You should buy a $X RI on service Y". V3
  at the earliest. Today: signal only.

## See also

- [`rds-extended-support.md`](./rds-extended-support.md) — the V1
  hero insight
- [`../concepts.md`](../concepts.md) — the 9 concepts
- [`../data-model.md`](../data-model.md) — the `focus_charges` and
  `insights` tables
- [`../api/endpoints.md`](../api/endpoints.md) — the HTTP surface
  for the FOCUS ingestion and the runner
