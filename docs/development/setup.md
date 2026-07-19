# Local setup

> The repo is a uv-managed Python monorepo. Frontend is Next.js, separate
> toolchain. Tests are pytest. Postgres is docker-compose. MinIO is in
> the same compose file for parity with prod S3 (V1: not used by any
> code path, but the file is there to be honest about the V2 plan).

## Prerequisites

- **Python 3.13.** `uv` will download 3.13 even if your system has 3.14
  (this is the managed-tooling guarantee). Don't fight it.
- **uv.** The all-in-one Python package manager. Install once:
  ```bash
  pip install uv
  # or on Windows:
  py -m pip install uv
  ```
- **Docker Desktop.** For the Postgres + MinIO containers.
  <https://www.docker.com/products/docker-desktop/>
- **Node.js 22.x.** For the web app. The CI uses `setup-node@v4` with
  `node-version: "22"`.
- **psql** (Postgres client). Optional, but useful for poking at the
  database. Postgres 16 is what the container runs.

On Windows, all of the above must be on the PATH. Git Bash is not
required — use PowerShell.

## Bootstrap (one-time)

From the repo root:

```powershell
# 1. Sync the workspace. This creates a .venv and installs all 13
# workspace members + dev deps.
uv sync --all-extras

# 2. Start Postgres (port 5432) and MinIO (ports 9000/9001).
docker compose up -d

# 3. Apply ALL the SQL migrations (0001–0014 today). The docker-compose mounts
# ./db/migrations into the container's initdb path, so the first
# `docker compose up` runs them automatically. If you need to
# re-apply on a fresh DB, drop the volume:
docker compose down -v
docker compose up -d
```

Verify:

```powershell
# Postgres is up and the schema is applied.
psql -h localhost -U constat -d constat -c "SELECT COUNT(*) FROM accounts;"
# Password: constat (matches docker-compose.yml).

# Tests pass.
uv run pytest -v
```

The default test DB is `sqlite:///:memory:` (see
`tests/conftest.py`). You don't need a running Postgres for tests
unless you're specifically testing Postgres-only behavior (RLS
policies).

## Day-to-day

```powershell
# Tests
uv run pytest -v

# Lint + format check
uv run ruff check .
uv run ruff format --check .

# Type-check the core package
uv run mypy packages/core/src

# API on http://localhost:8000 (uvicorn constat_api.main:app works too)
uv run python -m constat_api

# Web on http://localhost:3000
cd apps/web
npm install
npm run dev
```

The first `uv sync` creates `.venv/`. Re-run `uv sync` after a
`pyproject.toml` change. `uv.lock` is the source of truth — commit
changes to it.

## Environment variables

V1 is env-driven. The minimal config:

```bash
# Postgres (override the default localhost DSN if you need to).
CONSTAT_DATABASE_URL=postgresql://constat:constat@localhost:5432/constat

# AWS — for live connector runs. Leave empty for local dev (uses
# the default boto3 chain or a CONSTAT_AWS_PROFILE).
CONSTAT_AWS_PROFILE=
```

The full `.env.example` lives at the repo root. Copy to `.env` and
fill in. **Never commit `.env`.**

`settings.py::Settings` reads these once at import time. There is no
hot-reload — restart the API.

## What the workspace members look like

```
packages/
  core/                  # models, namespaces, catalog (the stable contract)
  connectors/
    aws_rds/             # boto3 RDS scan
    aws_ec2/             # boto3 EC2/EBS scan (volumes, snapshots, instances)
    focus/               # FOCUS 1.0 CSV → focus_charges
  insights/
    rds_eol/             # the V1 hero rule
    mysql_eol/           # MySQL Extended Support
    aurora_eol/          # Aurora Extended Support
    ebs_gp2_to_gp3/      # gp2 → gp3 savings
    ebs_unattached/      # unattached-volume waste
    snapshot_orphan/     # orphan-snapshot waste
    ec2_stopped_with_storage/  # stopped-instance storage
    chargeback/          # the FOCUS drift rule

apps/
  api/                   # FastAPI
  web/                   # Next.js

db/migrations/           # 14 raw-SQL migrations, applied in order
tests/                   # cross-package pytest
```

Each `pyproject.toml` in a workspace member declares its own
dependencies. `uv sync` resolves them all in one lockfile.

## The two non-Python subsystems

### Next.js (apps/web)

- App Router (`apps/web/app/`). Pages: `/`, `/insights`, `/insights/[id]`,
  `/insights/inbox`, `/inconclusives`, `/chargeback`, `/status`,
  `/accounts`, `/insight-runs`, `/restitution`.
- API client: `apps/web/lib/api.ts`. Read at build time via
  `NEXT_PUBLIC_API_URL` (default `http://localhost:8000`).
- Component library: none. Inline styles for now. V2: pick a
  primitive set (Radix, shadcn) and document the choice.

### Postgres (docker-compose)

- `postgres:16-alpine`. User/db `constat`, password `constat`.
- Mounts `db/migrations/` into `/docker-entrypoint-initdb.d/`. First
  startup applies all migrations (0001–0014 today).
- Healthcheck: `pg_isready -U constat -d constat` every 5s, 5 retries.

If you need to inspect data:

```powershell
psql -h localhost -U constat -d constat
# Password: constat.

\dt                  # list tables
SELECT * FROM accounts;
SELECT rule_name, COUNT(*) FROM insights GROUP BY rule_name;
SELECT reason, COUNT(*) FROM inconclusive GROUP BY reason;
```

### MinIO (S3 parity, V1 not used)

- `minio/minio:latest`. Root user `constat`, password `constat-secret`.
- Ports 9000 (S3 API) and 9001 (web console).
- V1 does not write to S3 (observations are stored in the
  `observations` table). V2 will offload payloads.

## Lint, format, type-check

Three commands, run before every push:

```powershell
uv run ruff check .                   # lint
uv run ruff format --check .          # format check (CI fails on drift)
uv run mypy packages/core/src         # type-check core (the stable contract)
```

`ruff` is configured in `pyproject.toml` (line-length 100, Python 3.13).
`mypy` is `--strict` on `packages/core/src` only — we don't enforce it
on the rest of the codebase yet.

## CI

`.github/workflows/ci.yml` runs on every push and PR to `main`. It:

- lints with ruff
- format-checks with ruff
- type-checks `packages/core/src` with mypy
- runs the pytest suite
- builds the web app (`npm run build`)

A green CI is the bar for merging. Local `uv run pytest -v` and
`uv run ruff check .` is the cheap pre-flight.

## What this setup is NOT

- **Not a production deploy.** The docker-compose is for local dev.
  The web app talks to the API on `localhost:8000`. The Postgres
  password is `constat` and is in the repo. None of this is safe to
  expose.
- **Not a V2 setup.** No SQS, no Fargate, no Aurora, no CloudFront.
  The V1 stack is intentionally small. See
  [`../architecture.md`](../architecture.md) for what's deliberately
  out.
- **Not Windows-native in the build chain.** The repo works on
  Windows (PowerShell) for dev, but CI is Ubuntu. If you see
  CRLF/LF drift in `git status`, that's `core.autocrlf` and is
  cosmetic — it does not break the code.

## See also

- [`running-the-stack.md`](./running-the-stack.md) — the end-to-end V1
  demo path
- [`known-issues.md`](./known-issues.md) — drift between ORM and
  migrations, and other traps
- [`../architecture.md`](../architecture.md) — what the system is
