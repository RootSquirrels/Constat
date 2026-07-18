# Constat

**Proof-grade cloud insights. Connect a read-only role; in two hours you know what your AWS fleet is silently costing you — with evidence for every number.**

In French, a *constat* is a certified statement of fact — the document a bailiff draws up that stands in court. That is the standard this product holds itself to: every value on screen carries its source, its timestamp, and an honest **"unknown"** when we cannot prove it. Never a false "compliant". Never a guessed number.

## What Constat finds

| Insight | The question it answers | Typical stakes |
|---|---|---|
| RDS PostgreSQL Extended Support | Which databases run EOL engines and pay the surcharge? | $0.10–0.20 per vCPU-hour, ~$580/month for a single `db.m5.xlarge` in year 3 |
| RDS MySQL Extended Support | Same, for the largest EOL population on the market (MySQL 5.7) | Year-3 pricing doubled in March 2026 |
| Aurora Extended Support | Same, engine-aware (Aurora MySQL has no year-3 tier — we know) | Per-vCPU surcharge, often invisible in invoices |
| FOCUS chargeback | What does each account and service really cost, billed vs. amortized? | Cost allocation your finance team can trust |

Each result is either a **quantified finding** (with the formula and facts behind it), a proven **"no issue"**, or an explicit **"inconclusive"** telling you exactly which data was missing — because a resource that silently disappears from a report is the failure mode this product exists to eliminate.

## Why it's different

- **Read-only, no agent, no prerequisites.** One cross-account IAM role with a mandatory External ID. No Business Support plan required, nothing installed on your workloads.
- **Evidence, not opinions.** Every fact links to the scan that observed it. Absence is only claimed after two consecutive complete scans — a network timeout can never "delete" your resources.
- **Freshness is enforced, not assumed.** Data older than 24 hours degrades to *inconclusive* instead of masquerading as current.
- **Your inventory is the truth.** Cloud APIs define what exists; billing data enriches it. A resource with no cost line is still a resource.

## Under the hood

Python 3.13 · FastAPI · PostgreSQL 16 (row-level security enforced and tested in CI across all 13 tenant tables) · Next.js 15 · Terraform · daily scheduled scans via EventBridge. Reference data (EOL calendars, Extended Support pricing) is versioned, source-linked and review-dated in code.

Security posture: non-owner database runtime role, append-only audit log, PII stored as hashes only, per-tenant retention policies, API-key auth with constant-time comparison. Details in [`docs/operations/`](docs/operations/) — including the runbooks a security questionnaire will ask about.

## Quick start (development)

```bash
pip install uv
uv sync --all-packages
docker compose up -d          # Postgres 16 + MinIO
uv run pytest -v
cd apps/web && npm install && npm run dev   # UI on http://localhost:3000
```

Deployment: see [`infra/`](infra/) (Terraform: ECS Fargate, RDS, Secrets Manager, scheduler) and [`docs/operations/deployment.md`](docs/operations/deployment.md).

## Repository layout

```
packages/core/           # stable contract: models, namespaces, reference catalog
packages/connectors/     # aws_rds, focus (FOCUS 1.0)
packages/insights/       # rds_eol, mysql_eol, aurora_eol, chargeback
apps/api/                # FastAPI: collectors, rule runner, REST API
apps/web/                # Next.js: insights, chargeback, POC report, run health
db/migrations/           # plain SQL, applied and verified in CI
infra/                   # Terraform (pilot deployment)
docs/                    # architecture, ADRs, runbooks, benchmarks
tests/                   # unit + Postgres-backed RLS integration tests
```

Engineering conventions and module ownership: [`AGENTS.md`](AGENTS.md). Product scope decisions: [`docs/adr/`](docs/adr/).

## Status

V1 — pilot-ready. Four insight rules, scheduled daily collection, multi-tenant isolation tested against live Postgres in CI, benchmarked at 10k resources. Roadmap and thresholds for what comes next: [`docs/`](docs/).

## License

Proprietary. All rights reserved. — Contact: romain.bailleul@protonmail.com
