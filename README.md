# Constat

Cloud inventory observability — the **écart chiffré** between what your cloud account
*should* look like and what it *actually* looks like, in euros, with provenance.

> Inventory-first. Multi-source. Every value carries its source, its timestamp, and
> "unknown" when we don't know — never a false "compliant".

## Status

**V1 foundations** — this is the first commit. One demoable insight (RDS PostgreSQL
Extended Support), FOCUS chargeback, and the contract surface for 3 engineers to
work in parallel. See `AGENTS.md` for what is and isn't in V1.

## Architecture (TL;DR)

- **Inventory-first**: AWS APIs are the source of truth. Cost, security, CMDB enrich — they never create.
- **Stable core contract**: `packages/core` is owned by the lead and changes only via ADR.
- **Connectors and insights are independent modules** — one engineer per module, no cross-imports.
- **Boring tech**: Postgres 16, S3/MinIO, Fargate (later), FastAPI, Next.js.

## Quick start

See `AGENTS.md` → "Dev bootstrap". Short version:

```bash
pip install uv
uv sync
docker compose up -d
uv run pytest -v
cd apps/web && npm install && npm run dev   # web on http://localhost:3000
```

## Layout

```
packages/core/           # models, namespaces, catalog constants
packages/connectors/     # aws_rds, focus
packages/insights/       # rds_eol, chargeback
apps/api/                # FastAPI
apps/web/                # Next.js
db/migrations/           # SQL
tests/                   # pytest
```

## License

Proprietary. All rights reserved.
