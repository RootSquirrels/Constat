# Core concepts

The product is built on nine concepts. They are not new — they are the
shapes that emerge from "inventory-first" + "the absence of a value is a
first-class signal". Each one is a row in the database, a Pydantic
model in `packages/core`, and a real thing in the runner.

References: see `packages/core/src/constat_core/models.py`,
`packages/core/src/constat_core/namespaces.py`, the migrations in
`db/migrations/`, and the runner in
`apps/api/src/constat_api/insights/runner.py`.

---

## 1. Resource

> *A stable identity of a cloud resource, proven present by a complete
> scan.*

A Resource is a `(account, region, resource_type, native_id)` tuple
with a UUID we mint. The same ARN seen again across two scans resolves
to the same `resource_id`. A native_id that reappears after a confirmed
retirement gets a *new* incarnation (V2 feature; V1 keeps the same
`resource_id` and unsets `retired_at` on next sighting).

Key invariants:

- `retired_at` is **null** until retirement is *proven* by a successful
  scan that didn't see the resource. We never guess.
- The 4-tuple identity is enforced by `UNIQUE (account_id, region,
  resource_type, native_id)`.

Code:
- Pydantic: `packages/core/src/constat_core/models.py::Resource`
- Table: `resources` (`db/migrations/0001_init.sql`)
- ORM: `apps/api/src/constat_api/orm.py::ResourceORM`
- Repo: `apps/api/src/constat_api/repositories/resources.py`

---

## 2. Observation

> *An immutable, source-true data point. Replayable from S3 in V2,
> stored in the DB in V1 for replay debugging.*

An Observation is the payload as the source gave it, with the
`observed_at` we assign at scan time. In V1 we keep the JSONB in the
`observations` table directly. V2 will offload payloads to S3 and keep
only a `payload_ref` here.

The `source_run_id` link (added in migration 0006) connects every
observation to the run that produced it. Without that chain, "the scan
succeeded" is an unfalsifiable claim.

Code:
- Pydantic: `packages/core/src/constat_core/models.py::Observation`
- Table: `observations` (`db/migrations/0001_init.sql`, link in
  `0006_facts_current_state.sql`)
- Collector: `packages/connectors/aws_rds/src/constat_aws_rds/collector.py::db_to_observation`

---

## 3. Fact

> *A current, namespaced value with provenance. The atom of the
> inventory.*

A Fact is `(namespace, key, value, value_state, source, observed_at)`.
It is the only thing the insights see. A fact can be scoped to a
resource (most cases) or to an account (cost aggregates, account-level
catalog entries).

**The data contract:** every fact published to the `facts` table
must be declared in [`fact-registry.md`](./concepts/fact-registry.md)
— a YAML + CI guard that catches typos, missing entries, and
producer/consumer drift. Adding a fact means adding it to the YAML
**and** to the test's `EXPECTED_PRODUCED`/`EXPECTED_CONSUMED`
constant. V2's runtime `FactDefinitionRegistry` table will be
migrated from this YAML; the contract is the same.

Key invariants:

- A fact's value is **not authoritative** by itself. It is authoritative
  *together* with `value_state` and `observed_at`.
- `value_state = KNOWN` means "the source confirmed this value, and it
  is fresh". `UNKNOWN` means "the source was queried, the value could
  not be determined" (e.g. the instance class is one we don't have a
  vCPU entry for).
- Two sources do not share a `namespace.key` (e.g. `aws.tag.Owner` and
  `servicenow.cmdb.assignment_group` are distinct facts).
- The unique key is `(tenant_id, resource_id, namespace, key, source)`
  — *no* `observed_at`. The current-state model. (See
  [`development/known-issues.md`](./development/known-issues.md) for
  the ORM/migration drift on this constraint.)

Namespaces (V1):
- `aws.*` — direct from AWS APIs (`aws.rds.engine`, `aws.rds.vcpu`, …)
- `catalog.*` — versioned reference data (`catalog.postgres.eol_date`)
- `cost.*` — FOCUS-derived cost facts (V1: not used; we keep
  `focus_charges` as a denormalized table instead. Tag-based
  cost facts come with V2.)
- `derived.*` — computed by insights (V2)

Code:
- Pydantic: `packages/core/src/constat_core/models.py::Fact`
- Table: `facts` (`db/migrations/0001_init.sql` + `0006_facts_current_state.sql`)
- Namespaces: `packages/core/src/constat_core/namespaces.py`

---

## 4. SourceRun

> *The proof of completeness for a (account, region, type) scan.*

A SourceRun is the answer to "did we actually look?". When the runner
asks "is this resource's scope proven?", it asks the `source_runs`
table: was there a `status = 'success'` run for
`(account, region, resource_type, source)` recently enough?

The partial unique index `(account_id, region, resource_type, source)
WHERE status = 'running'` ensures only one scan per scope is active.
Multiple completed runs coexist.

A run is `success` when the AWS API call completed without error for
the entire region. Per-resource errors are not possible (the API
returns the whole paginated result); per-region errors are recorded
and the scan continues with the next region. `AccessDenied` is a run
error, not a resource value — the absence is *not* provable.

Code:
- Table: `source_runs` (`db/migrations/0005_source_runs.sql`)
- ORM: `apps/api/src/constat_api/orm.py::SourceRunORM`
- Repo: `apps/api/src/constat_api/repositories/source_runs.py`
- Producer: `apps/api/src/constat_api/collectors/aws.py::collect_target`

---

## 5. Insight

> *A computed gap. The "yes, here is a problem" output.*

An Insight is what the user pays for. It is produced by a rule
(`rule_name`) against a target (resource or account) and carries a
`payload` with enough evidence to be proven wrong by the user. In V1:

- `rds_eol` — resource-scoped, RDS in PostgreSQL Extended Support
- `chargeback` — account-scoped, FOCUS-derived amortized-vs-billed drift

An Insight is *only* emitted when the rule *proves* a gap. The runner
checks the scope proof first; without it, no insight is emitted and an
`Inconclusive` is produced instead.

The Insight is the product. The Inconclusive is the differentiator.

Code:
- Pydantic: `packages/core/src/constat_core/models.py::Insight`
- Table: `insights` (`db/migrations/0001_init.sql`)
- Runners: `apps/api/src/constat_api/insights/runner.py::run_rds_eol`,
  `…::run_chargeback`
- Resolvers: `packages/insights/rds_eol/src/constat_rds_eol/resolver.py`,
  `packages/insights/chargeback/src/constat_chargeback/resolver.py`

---

## 6. Inconclusive

> *A "we don't know" record. The visible absence.*

This is the rule output when the evaluation could *not* complete. The
reasons are: a fact was missing, the scope was not proven, the
resource is in a state the rule does not handle. An Inconclusive is
*never* silent. It carries the `missing_facts` list, the `reason`, and
a timestamp.

The GTM hook: when a customer sees 5 Insights and 12 Inconclusives
explaining "we'd know but your RDS instance has no `vcpu` because the
class is a new Graviton we haven't catalogued yet", the customer
understands the product's epistemic discipline. The next step is
"let's add the catalog entry" — not "your tool says 5 things are fine
when actually 12 are broken".

V1 emits Inconclusive only for `rds_eol` (resource-based, scope-gated).
The `chargeback` rule treats FOCUS data as user-provided truth, so it
has no Inconclusive branch in V1.

Code:
- Pydantic: `packages/core/src/constat_core/models.py::Inconclusive`
- Table: `inconclusive` (`db/migrations/0002_inconclusive.sql`)
- Resolver: `packages/insights/rds_eol/src/constat_rds_eol/resolver.py::evaluate`
- UI: `apps/web/app/inconclusives/page.tsx`

---

## 7. Value state

> *Whether the fact is known, unknown, stale, or in error.*

Four states, one per fact:

| State | Meaning |
|---|---|
| `KNOWN` | The source confirmed a value, and the value is fresh |
| `UNKNOWN` | The source was queried, the value is genuinely not determinable (e.g. no vCPU entry for the instance class) |
| `STALE` | A previous value exists but is past its freshness threshold |
| `ERROR` | The source was queried but the call failed |

`false` is **never** a value. It is a *deduction* that requires a
successful scan and a value that means "absent". The product never
emits `false` for an inventory boolean; if the source says "no tag",
that is `aws.tag.X = null` with state `KNOWN`. If we don't know
whether the source was queried, we emit `UNKNOWN`.

Code:
- Enum: `packages/core/src/constat_core/namespaces.py::ValueState`

---

## 8. Catalog (reference data)

> *The dated, versioned knowledge the rules consume. The moat.*

The catalog is what makes the *écart chiffré* possible. The
`rds_eol` rule, given a `(version, instance_class)`, looks up
`catalog.postgres.eol_date` and `aws.rds.vcpu`, multiplies the
Extended Support tier rate by the vCPU count, and emits the monthly
cost. The catalog is the difference between "PostgreSQL 11 is end of
life" (a fact AWS publishes once and we mirror) and "your PostgreSQL
11 instance on db.m5.xlarge costs $580/month in Extended Support" (a
quantified estimate on the customer's fleet).

In V1 the catalog is a Python module: `packages/core/src/constat_core/catalog/aws.py`.
It is module-level `frozen=True` data, reviewed monthly. In V2 it
moves to a `reference_datasets` table with `effective_from` /
`effective_to`, and facts reference the version that produced them.

A stale catalog degrades dependent insights to `INCONCLUSIVE`. A wrong
catalog is a worse failure than a stale one — see
[`development/known-issues.md`](./development/known-issues.md) for the
checklist.

Code:
- Catalog: `packages/core/src/constat_core/catalog/aws.py`
- Resolver consumer: `packages/insights/rds_eol/src/constat_rds_eol/resolver.py`

---

## 9. InsightRun

> *The audit record of one rule execution.*

Every call to the runner produces one `insight_runs` row: the rule
name, when it started, when it finished, the count of resources
scanned, the count of insights emitted, the count of inconclusive
emitted, the status, and any error text. This is the trace that lets
us answer "what did the rds_eol rule do at 14:00 yesterday?".

The `insight_runs` endpoint is the audit surface for the customer
("how often does the rule run? what did the last run produce?") and
for us ("did the deployment of the new rule break anything?").

Code:
- Table: `insight_runs` (`db/migrations/0001_init.sql`)
- ORM: `apps/api/src/constat_api/orm.py::InsightRunORM`
- Endpoint: `apps/api/src/constat_api/routers/insight_runs.py`
- Producer: `apps/api/src/constat_api/insights/runner.py::run_rds_eol`,
  `…::run_chargeback`

---

## The four flow verbs

Once you have the 9 concepts, the system has four verbs. Every
deployment is one of them:

| Verb | What it does | Source | Target table(s) |
|---|---|---|---|
| `collect.aws` | Scan a target account, write resources + observations + facts + source_run | boto3 | `resources`, `observations`, `facts`, `source_runs` |
| `ingest.focus` | Load a FOCUS CSV, aggregate, write `focus_charges` | CSV | `focus_charges`, `accounts` |
| `run.rds_eol` | Read facts per resource, evaluate, emit insights + inconclusive | facts | `insights`, `inconclusive`, `insight_runs` |
| `run.chargeback` | Read `focus_charges`, aggregate per period, emit insights | focus_charges | `insights`, `insight_runs` |

Plus three read verbs (the API):

| Verb | Endpoint | What it serves |
|---|---|---|
| `get.insights` | `GET /insights` | The gaps, with payload |
| `get.inconclusives` | `GET /inconclusives` | The "we don't know" records, with `missing_facts` |
| `get.health` | `GET /health` | The DB ping |

The verbiage is consistent in the code, the CLI, the API, and the UI.

## See also

- [`data-model.md`](./data-model.md) — the 7 tables and the FK chains
- [`api/endpoints.md`](./api/endpoints.md) — the routers
- [`insights/rds-extended-support.md`](./insights/rds-extended-support.md) — the
  full spec of the hero insight
- [`architecture.md`](./architecture.md) — the four-box view
