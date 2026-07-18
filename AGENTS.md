# AGENTS.md — Constat (Cloud Assurance Platform)

> Read this before touching the repo. Conventions, ownership, and the things we explicitly do NOT do yet.

## What is Constat

Inventory-first cloud observability. The product is the **écart chiffré** (proven gap in €/$)
between what a cloud account *should* look like and what it *actually* looks like,
across inventory × lifecycle × cost × operational coverage.

The first V1 deliverable is one demoable insight (RDS PostgreSQL Extended Support) over
real AWS data, plus a chargeback view backed by FOCUS. The GTM promise is
"in 2h of connection, we prove what you don't know about your fleet — and what it costs."

## Repo layout (monorepo, multi-engineer friendly)

```
Constat/
├── packages/
│   ├── core/                    # STABLE CONTRACT — models, namespaces, catalog constants
│   ├── connectors/
│   │   ├── aws_rds/             # boto3 RDS
│   │   └── focus/               # FOCUS CSV/Parquet → Postgres
│   └── insights/
│       ├── rds_eol/             # rule: PG major < LTS → Extended Support → €/month
│       └── chargeback/          # rule: per account × service, amortized vs brut
├── apps/
│   ├── api/                     # FastAPI — orchestrator + REST
│   └── web/                     # Next.js — "Insights" + "Chargeback" views
├── db/migrations/               # SQL migrations (Alembic later)
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

## Value states

Every fact carries a `value_state`: `KNOWN` / `UNKNOWN` / `STALE` / `ERROR`.
The product surfaces `UNKNOWN` explicitly — that's the differentiator vs
Trusted Advisor / Cost Explorer, which silently omit.

## Explicitly NOT in V1 (backlog, not "soon")

- Step Functions, SQS, Fargate orchestration — V1 is a Fargate task + cron. We add SFN when we have >1 connector producing on different cadences.
- Multi-tenant RLS — V1 is 1 prospect, 1 tenant. Add RLS when we onboard tenant #2.
- Full `FactDefinitionRegistry` ceremony — V1 uses a `namespace.key` enum + a CHECK constraint. Registry is V2.
- Azure, Prisma, ServiceNow, EDR connectors — V2/V3.
- Streaming / Neo4j / Iceberg — only if quantitative thresholds are met (see ADR-04 in the arch doc).

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

# 4. Apply migrations
psql -h localhost -U constat -d constat -f db/migrations/0001_init.sql
# (password: constat — see docker-compose.yml)

# 5. Run tests
uv run pytest -v
```

## Useful day-to-day

```bash
uv run pytest -v                    # tests
uv run ruff check .                 # lint
uv run ruff format .                # format
uv run python -m constat_api        # API on http://localhost:8000
cd apps/web && pnpm install && pnpm dev   # web on http://localhost:3000
```

## Where things will get decided later

- Alembic vs raw SQL (we're on raw SQL until we have >1 migration).
- ORM (we use SQLAlchemy Core, not ORM, until we have complex relations).
- Auth on the API (none in V1, internal only).
- Secrets management (V1 = .env; V2 = AWS Secrets Manager).
