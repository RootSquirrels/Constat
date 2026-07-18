# Running the stack (V1 demo path)

> This doc walks through the V1 demo from a clean checkout to insights
> on the screen. It's the script for the pilot POC: every step is
> reproducible, every step is short.

## The 5 commands

The whole V1 demo in five shell calls. Each is documented below in
detail.

```powershell
# 0. Setup (one-time)
uv sync --all-extras
docker compose up -d
uv run pytest -v                                # ~20 seconds

# 1. Seed account + ingest a FOCUS CSV (no AWS needed)
python -m constat_api.cli.focus --account 111111111111 --csv examples/focus-sample.csv

# 2. (If you have AWS access) Trigger the AWS collector
python -m constat_api.cli.aws --targets examples/targets.json --dry-run   # smoke test
python -m constat_api.cli.aws --targets examples/targets.json             # write

# 3. Run the insight rules
python -m constat_api.cli.run_insights --rule rds_eol
python -m constat_api.cli.run_insights --rule chargeback --period-label "all-time"

# 4. Browse
uv run python -m constat_api          # API on :8000
# in another shell:
cd apps/web && npm run dev            # Web on :3000
# → http://localhost:3000
```

The 5 commands map to the 4 verbs in
[`../concepts.md`](../concepts.md#the-four-flow-verbs):

| Verb | Command |
|---|---|
| `ingest.focus` | `python -m constat_api.cli.focus` |
| `collect.aws` | `python -m constat_api.cli.aws` |
| `run.rds_eol` | `python -m constat_api.cli.run_insights --rule rds_eol` |
| `run.chargeback` | `python -m constat_api.cli.run_insights --rule chargeback` |
| browse | `python -m constat_api` + `npm run dev` |

## Step-by-step

### 0. Setup (assumes clean checkout)

```powershell
uv sync --all-extras
docker compose up -d
uv run pytest -v
```

The pytest suite uses an in-memory sqlite (see
`tests/conftest.py::engine`). It does **not** require a running
Postgres.

If the docker-compose `up` re-applies the 6 migrations on an existing
volume, you can re-check with:

```powershell
psql -h localhost -U constat -d constat -c "SELECT version FROM _migrations ORDER BY version;"
# (Note: V1 has no _migrations table. The 6 SQL files in
# db/migrations/ are the truth. The 6 numbers are 0001..0006.)
```

### 1. Ingest a FOCUS CSV

If you don't have a FOCUS export handy, you can use a synthetic one.
The repo doesn't ship an example file (add one in `examples/` if you
need it; do not commit proprietary FOCUS exports).

The minimal FOCUS 1.0 file is a CSV with the 11 required columns
(see `packages/connectors/focus/src/constat_focus/loader.py::FOCUS_REQUIRED_COLUMNS`):

```csv
BillingAccountId,BillingAccountName,ServiceName,ChargePeriodStart,ChargePeriodEnd,BilledCost,EffectiveCost,PricingCategory,Region,ResourceId,SubAccountId
111111111111,prod-eu,AmazonRDS,2026-07-01T00:00:00Z,2026-08-01T00:00:00Z,1234.56,1180.00,On-Demand,eu-west-1,arn:aws:rds:eu-west-1:111111111111:db:app-prod,222222222222
```

```powershell
python -m constat_api.cli.focus --account 111111111111 --csv path/to/focus.csv
# → rows_read, rows_written, inserted, updated, duration_seconds
```

The CLI is at `apps/api/src/constat_api/cli/focus.py`. The function
`ingest_focus_csv` is the in-process entry point for tests and for
the HTTP endpoint `POST /collect/focus`.

### 2. AWS collector (optional, requires IAM)

You need a cross-account role in the prospect's AWS account:

```json
{
  "Version": "2012-10-17",
  "Statement": {
    "Effect": "Allow",
    "Principal": { "AWS": "<your-Constat-execution-role-arn>" },
    "Action": "sts:AssumeRole",
    "Condition": { "StringEquals": { "sts:ExternalId": "<shared-secret>" } }
  }
}
```

The role needs read access to RDS, EC2 (for tags and SSM in V2), and
STS Self-Tag (`iam:TagSession`) for traceability.

A `targets.json` looks like:

```json
[
  {
    "aws_account_id": "111111111111",
    "role_arn": "arn:aws:iam::111111111111:role/ConstatReadOnly",
    "external_id": "<shared-secret>",
    "name": "prod-eu",
    "regions": ["eu-west-1", "eu-west-3"]
  }
]
```

Run:

```powershell
# Smoke test: assume role, list regions, do not write.
python -m constat_api.cli.aws --targets examples/targets.json --dry-run

# Real scan: write resources, observations, facts, source_runs.
python -m constat_api.cli.aws --targets examples/targets.json
```

The CLI is at `apps/api/src/constat_api/cli/aws.py`. The same
function is exposed as `POST /collect/aws` for API-based triggering.

### 3. Run the insight rules

Two rules in V1, both synchronous:

```powershell
# rds_eol: per-resource, scope-gated. Reads facts, emits Insights
# and Inconclusives. The "scope not proven" branch fires for any
# resource whose (account, region, resource_type) has no successful
# source_run.
python -m constat_api.cli.run_insights --rule rds_eol

# chargeback: per-account, no scope gate. Aggregates focus_charges
# by (account, service, period), emits per-service drift.
python -m constat_api.cli.run_insights --rule chargeback --period-label "all-time"
```

`--period-label` is a free-form string stored in the insight payload.
Useful for tagging time-bound views in the UI later.

The CLI is at `apps/api/src/constat_api/cli/run_insights.py`. The
dispatcher is in `apps/api/src/constat_api/insights/runner.py::run_rule`.

### 4. Browse

Two processes:

```powershell
# Terminal 1: API on :8000
uv run python -m constat_api

# Terminal 2: Web on :3000
cd apps/web
npm install       # one time
npm run dev
```

The web app reads `NEXT_PUBLIC_API_URL` at build time (default
`http://localhost:8000`). For a different API, set it in
`apps/web/.env.local` and restart `npm run dev`.

The home page (`/`) shows three cards: **Insights**,
**Inconclusives**, **Chargeback**. Each is a server-rendered
fetch — they degrade gracefully if the API is down.

### 5. The "happy path" smoke test (no AWS)

If you don't have AWS access, you can still exercise the V1 path
end-to-end with just FOCUS + a few SQL inserts:

```powershell
# 1. Ingest a FOCUS CSV.
python -m constat_api.cli.focus --account 111111111111 --csv examples/focus-sample.csv

# 2. Run the chargeback rule. No AWS needed.
python -m constat_api.cli.run_insights --rule chargeback --period-label "all-time"

# 3. Browse → /chargeback (read-only view, by-account per-service).
# 4. Browse → /insights (the chargeback insights surface here too).
```

To exercise `rds_eol` without AWS, you need a `resources` row, an
account, facts, and a successful `source_run`. The test fixtures
in `tests/test_runner.py` show the minimum setup.

## Where things can go wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| `psql` connection refused | Docker not started | `docker compose up -d` |
| `pytest` collection error on `boto3` | `uv sync` not run | `uv sync --all-extras` |
| `rds_eol` emits only Inconclusives | No successful `source_run` for the scope | Re-run the AWS collector; check `source_runs.status` |
| `chargeback` emits no insights | No `focus_charges` rows | Re-ingest the FOCUS CSV; check `account_id` match |
| `AccessDenied` in the AWS collector response | Trust policy or ExternalId mismatch | Check the prospect's role trust + the `external_id` in the target |
| `ImportError: constat_aws_rds` | Missing `uv sync` or wrong `pythonpath` | Re-run `uv sync`; check `pyproject.toml::tool.uv.workspace` |
| Web app shows "API error" | API not running or wrong URL | Start the API; check `NEXT_PUBLIC_API_URL` |

## See also

- [`../api/endpoints.md`](../api/endpoints.md) — the API surface
- [`../concepts.md`](../concepts.md) — the 9 concepts and the 4 verbs
- [`known-issues.md`](./known-issues.md) — drift between ORM and SQL
