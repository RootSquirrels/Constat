# AGENTS.md — Constat (Cloud Assurance Platform)

> Read this before touching the repo. Conventions, ownership, and the things we explicitly do NOT do yet.

## What is Constat

Inventory-first cloud observability. The product is the **écart chiffré** (proven gap in €/$)
between what a cloud account *should* look like and what it *actually* looks like,
across inventory × lifecycle × cost × operational coverage.

The V1 deliverable is **insights-first** (ADR-12 in `docs/adr/`): 8 rules in
`RUNNERS` — `rds_eol`, `mysql_eol`, `aurora_eol`, `ebs_gp2_to_gp3`,
`ebs_unattached`, `snapshot_orphan`, `ec2_stopped_with_storage`, `chargeback` —
over real AWS data, plus a chargeback view backed by FOCUS. The GTM promise is
"in 2h of connection, we prove what you don't know about your fleet — and what it costs."

## Repo layout (monorepo, multi-engineer friendly)

```
Constat/
├── packages/
│   ├── core/                    # STABLE CONTRACT — models, namespaces, catalog constants
│   ├── connectors/
│   │   ├── aws_rds/             # boto3 RDS
│   │   ├── aws_ec2/             # boto3 EC2/EBS (volumes, states, types)
│   │   └── focus/               # FOCUS CSV/Parquet → Postgres
│   └── insights/
│       ├── rds_eol/             # rule: PG major < LTS → Extended Support → €/month
│       ├── mysql_eol/           # rule: MySQL 5.7/8.0 Extended Support → €/month
│       ├── aurora_eol/          # rule: Aurora MySQL/PG Extended Support → €/month
│       ├── ebs_gp2_to_gp3/      # rule: gp2 volume → gp3 savings → €/month
│       ├── ebs_unattached/      # rule: available volume → monthly waste
│       ├── snapshot_orphan/     # rule: snapshot whose volume is gone → €/month
│       ├── ec2_stopped_with_storage/  # rule: stopped instance storage → €/month
│       └── chargeback/          # rule: per account × service, amortized vs brut
├── apps/
│   ├── api/                     # FastAPI — orchestrator + REST
│   └── web/                     # Next.js — "Insights" + "Chargeback" + "Restitution" views
├── db/migrations/               # SQL migrations (Alembic later)
├── docs/pilot/                  # SLA pilote borné (projet, relecture juridique)
├── infra/                       # Terraform pilote (ECS+RDS+secrets) — not yet applied
├── deploy/prometheus/           # alerting rules
├── scripts/                     # bench_runner.py etc.
├── tests/                       # cross-package integration tests
└── .github/workflows/ci.yml
```

## Ownership & import rules (the contract)

- `packages/core` is a **stable contract**. Touching it requires an ADR (see `docs/adr/`).
  Other packages import core. Core imports nothing.
- `connectors/*` import core only. They do not import other connectors.
- `insights/*` import core only. They do not import other insights.
- `apps/api` is the orchestrator. It imports connectors + insights and wires them.
- `apps/web` consumes the API. It does not import from `packages/*` directly.

This is what lets 3 engineers work in parallel without merge conflicts.

## Language & tooling

- Python 3.13 (managed by `uv`; `uv` will download 3.13 even if system has 3.14).
- Type hints everywhere. `from __future__ import annotations` at the top of every module.
- Lint: `ruff check` + `ruff format`. Type-check: `mypy --strict` on `packages/core` only for now.
- Tests: `pytest`. New module = at least one happy-path test.
- Frontend: Next.js 15 App Router + TypeScript.

## Commit convention

`<scope>: <imperative>` — e.g. `core: add Fact model`, `aws_rds: paginate DescribeDBInstances`.
Scopes mirror the package names. No emoji. No "WIP" commits on main; branch if you must.

## Namespaces (V1)

`Fact` values are namespaced strings: `<namespace>.<key>`. Reserved namespaces in V1:

| Namespace | Source | Example keys |
|---|---|---|
| `aws.*` | Direct from AWS APIs | `aws.rds.engine`, `aws.ec2.instance_type` |
| `catalog.*` | Versioned reference data | `catalog.postgres.eol_date` |
| `cost.*` | FOCUS-derived | `cost.amortized_monthly`, `cost.unblended_monthly`, `cost.ri_coverage_pct` |
| `derived.*` | Computed by insights | `derived.days_to_eol` |

If you want to add a new namespace, open an issue first. We don't do EAV.

## Invariants you must not regress (audit-hardened)

- A source_run is `success` only when the scan loop **completed** without error —
  never by default. Retirement of resources requires **two consecutive** successful
  scans that both missed the resource (`CONSECUTIVE_SCANS_FOR_RETIREMENT`).
- Insight runs are delete-and-replace per rule: re-running a rule must not
  duplicate insights. Scope freshness: a successful run older than 24 h makes the
  scope `scope_stale` (INCONCLUSIVE), not "proven".
- **New tenant-scoped table ⇒ RLS policy in the same migration** (ENABLE + FORCE +
  `app.current_tenant_id` GUC, copy 0007/0011). The CI Postgres job fails otherwise.
- Cross-account AssumeRole always requires an `ExternalId` (API returns 422,
  collector raises `ValueError` otherwise). Persisted collect targets store it
  **write-only** — no GET ever returns it (masked as `external_id_set: true`).
- Collection is **async**: `POST /collect/aws` enqueues account×region WorkItems
  and returns 202 + `job_id` (poll `GET /collect/aws/jobs/{id}`). A worker drains
  the queue — inline thread locally (`CONSTAT_COLLECT_MODE=inline`, default),
  ECS service on SQS in the pilot. Per-account concurrency is bounded
  (`CONSTAT_WORKER_PER_ACCOUNT` — AWS quotas are per-account); a full queue
  answers 503 + Retry-After. Never reintroduce a synchronous collect path.
- Onboarding is batch: `POST /collect/targets/import` (CSV) or Organizations
  discovery (`python -m constat_api.cli.onboard`). `POST /collect/aws` with no
  explicit targets collects all persisted `collect_targets`.
- New resource-based insight = new package in `packages/insights/` (clone the
  `rds_eol` shape) + one entry in `RESOURCE_RULES` in
  `apps/api/src/constat_api/insights/runner.py`. Catalog pricing needs a source
  URL + review date; estimates carry `value_basis=ESTIMATED` until FOCUS confirms.
- Any rule that emits money MUST be registered in `constat_core.monetary.MONETARY`
  (payload key, value basis, kind) and mirrored in `apps/web/lib/api.ts`
  (`RULE_MONETARY`) — the completeness/pin tests in
  `tests/test_monetary_extraction.py` fail CI otherwise (ADR-13). Never sum
  `ACCOUNTING_DELTA` amounts into a savings total.
- Product position: V1 is sold **insights-first** (ADR-12 in `docs/adr/`). Do not
  promise a filterable inventory until it exists.

## Value states

Every fact carries a `value_state`: `KNOWN` / `UNKNOWN` / `STALE` / `ERROR`.
The product surfaces `UNKNOWN` explicitly — that's the differentiator vs
Trusted Advisor / Cost Explorer, which silently omit.

## Explicitly NOT in V1 (backlog, not "soon")

- Step Functions orchestration — collection is async via a plain SQS queue +
  worker service (roadmap H2 chantier 1, shipped 2026-07-19: `collect_queue.py`,
  `worker.py`, mode `inline` locally / `sqs` in the pilot). SFN only if we get
  >1 connector producing on different cadences.
- Multi-tenant RLS beyond the pilot shape — RLS **is** shipped and CI-enforced on all tenant tables (see the invariants above); V1 remains 1 prospect, 1 tenant operationally. Revisit when we onboard tenant #2.
- Full `FactDefinitionRegistry` ceremony — V1 has a YAML registry (`packages/core/src/constat_core/catalog/fact_definitions.yaml`) guarded by a pytest cross-check against producers/consumers. The full registry (DB table, runtime validation, backfill tooling) is V2.
- Azure, Prisma, ServiceNow, EDR connectors — V2/V3.
- Streaming / Neo4j / Iceberg — only if quantitative thresholds are met (see `docs/adr/`: ADR-07 streaming, ADR-08 Neo4j, ADR-01 Iceberg).

If you find yourself adding any of these in V1, stop and write a one-paragraph justification
in the PR description.

## Dev bootstrap (one-time)

```bash
# 1. Install uv (skips pip entirely, downloads Python 3.13)
pip install uv
# or on Windows: py -m pip install uv

# 2. Install Docker Desktop
# https://www.docker.com/products/docker-desktop/

# 3. Sync workspace + start infra
uv sync
docker compose up -d

# 4. Apply migrations (all of them, in order)
for f in db/migrations/*.sql; do
  psql -h localhost -U constat -d constat -v ON_ERROR_STOP=1 -f "$f"
done
# (password: constat — see docker-compose.yml)

# 5. Run tests
uv run pytest -v
```

Notes:
- `uv.lock` is committed; use `uv sync --frozen` in automation (CI does).
- The RLS tests (Postgres-marked) skip locally unless
  `CONSTAT_TEST_DATABASE_URL` points at a live Postgres; CI runs them
  against a service container.

## Useful day-to-day

```bash
uv run pytest -v                    # tests
uv run ruff check .                 # lint
uv run ruff format .                # format
uv run python -m constat_api        # API on http://localhost:8000
cd apps/web && npm install && npm run dev   # web on http://localhost:3000
```

## Where things will get decided later

- Alembic vs raw SQL (13 migrations in, still raw SQL — switch when Alembic is justified, not before).
- ORM (we use SQLAlchemy Core, not ORM, until we have complex relations).
- Auth on the API (API-key auth shipped: `X-API-Key` with reader/operator roles via `CONSTAT_API_KEYS`; OIDC/OAuth is V2).
- Secrets management (AWS Secrets Manager shipped in `infra/secrets.tf`; `.env` remains the local-dev path).
