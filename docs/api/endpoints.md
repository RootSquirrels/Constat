# API endpoints (V1)

> V1 is a small, synchronous API. There is no auth yet — the deployment
> is internal, behind a VPN. The contract here is what the UI consumes
> and what a pilot customer's integration would consume. Stable unless
> an entry is marked `(V2)`.

Base URL (dev): `http://localhost:8000`
Title: "Constat API" (from `settings.py`)
Version: `0.5.0` (from `apps/api/src/constat_api/main.py`)

Interactive docs: `/docs` (Swagger UI) and `/redoc`, served by FastAPI.

## The 6 routers

| Router | Prefix | Purpose | Code |
|---|---|---|---|
| `health` | (none) | DB ping | `apps/api/src/constat_api/routers/health.py` |
| `insights` | `/insights` | List, get, create insights | `apps/api/src/constat_api/routers/insights.py` |
| `insights` (runner) | `/insights` | Run a rule (synchronous) | `apps/api/src/constat_api/routers/runner.py` |
| `inconclusive` | `/inconclusives` | List "we don't know" records | `apps/api/src/constat_api/routers/inconclusive.py` |
| `insight-runs` | `/insight-runs` | Audit history of rule executions | `apps/api/src/constat_api/routers/insight_runs.py` |
| `aws` (collect) | `/collect/aws` | Trigger an AWS scan | `apps/api/src/constat_api/routers/aws.py` |
| `focus` (collect) | `/collect/focus` | Ingest a FOCUS CSV | `apps/api/src/constat_api/routers/focus.py` |

(That's 7 router modules but 6 prefixes — `/insights` is shared by the
list endpoints and the run endpoint.)

## `GET /health`

Liveness check. Pings the database.

**Response 200**:
```json
{ "status": "ok" }
```

If the DB is unreachable, FastAPI returns 500. The web app's home page
best-effort calls this and degrades gracefully on error.

## `GET /insights`

List insights, with optional filters and pagination.

**Query parameters**:
- `rule_name: str?` — filter by rule (`rds_eol`, `chargeback`)
- `severity: 'info' | 'warning' | 'critical'?`
- `account_id: UUID?` — filter to a single prospect account
- `limit: int = 100` (1..500)
- `offset: int = 0`

**Response 200**: `Insight[]`

```json
[
  {
    "id": "3f6c…",
    "rule_name": "rds_eol",
    "resource_id": "8c12…",
    "account_id": "1a2b…",
    "severity": "critical",
    "title": "RDS PostgreSQL 11 is in Extended Support",
    "payload": {
      "engine_version": "11.22",
      "major_version": 11,
      "eol_date": "2024-02-29",
      "end_of_extended_support": "2027-03-31",
      "days_to_event": 0,
      "pricing_tier": "year_3_plus",
      "pricing_usd_per_vcpu_hour": 0.20,
      "recommendation": "Upgrade to PostgreSQL 12 LTS now to stop Extended Support fees"
    },
    "computed_at": "2026-07-18T12:34:56Z"
  }
]
```

## `GET /insights/{insight_id}`

Fetch a single insight by id.

**Response 200**: `Insight` (same shape as list)
**Response 404**: `{"detail": "insight not found"}`

## `POST /insights`

Create a single insight. Used by tests and ingestion workers; not
exposed to a public UI yet.

**Request body**: `Insight` (no id required, server assigns one)
**Response 201**: `Insight` (with id)
**Response 422**: Pydantic validation error

## `POST /insights/run`

Run a rule across the current data. Synchronous in V1 (blocks the
request). The response includes the run summary.

**Request body**:
```json
{ "rule": "rds_eol", "period_label": "all-time" }
```

- `rule`: must be one of the keys in `RUNNERS` (V1: `rds_eol`, `chargeback`).
  An unknown rule returns 400.
- `period_label`: free-form label, stored in the insight payload.
  Default `"all-time"`. Used by the `chargeback` rule.
- `tag_key: str?` — when set, the `chargeback` rule re-aggregates
  by the chosen FOCUS tag (e.g. `"Application"`, `"CostCenter"`).
  Charges without a tag for the key go to the `__untagged__`
  bucket. Ignored by `rds_eol`.

**Query parameters**:
- `today: date?` (ISO `YYYY-MM-DD`) — override the current date for
  deterministic EOL/pricing calculation. Used by `rds_eol` and by the
  test suite.

**Response 200**:
```json
{
  "rule_name": "rds_eol",
  "resources_scanned": 17,
  "insights_emitted": 3,
  "inconclusive_emitted": 2,
  "errors": [],
  "period_label": ""
}
```

`errors` is non-empty when at least one resource raised an exception
during evaluation. The run's status is `partial` in that case (see
`/insight-runs`).

## `GET /inconclusives`

List the "we don't know" records. This is the surface that makes the
inventory-first promise *visible* — a customer asking "why didn't you
flag these?" gets the answer here.

**Query parameters**:
- `rule_name: str?`
- `account_id: UUID?`
- `limit: int = 100` (1..500)
- `offset: int = 0`

**Response 200**: `Inconclusive[]`

```json
[
  {
    "id": "7d9e…",
    "rule_name": "rds_eol",
    "resource_id": "2c4f…",
    "account_id": "1a2b…",
    "missing_facts": ["aws.rds.vcpu"],
    "reason": "missing_facts",
    "computed_at": "2026-07-18T12:34:56Z"
  }
]
```

`reason` is one of:
- `scope_not_proven` — no successful `source_run` for the
  `(account, region, resource_type, source)` at the time of evaluation
- `missing_facts` — one or more required facts (`aws.rds.engine`,
  `aws.rds.engine_version`, `aws.rds.vcpu`) are absent or `UNKNOWN`
- `<no facts>` — the resource has no facts at all
- `aws.rds.engine_version.malformed` — the version string is not
  parseable as a major.minor.patch

## `GET /inconclusives/{inconclusive_id}`

Fetch a single inconclusive record.

**Response 200**: `Inconclusive`
**Response 404**: `{"detail": "inconclusive not found"}`

> Implementation note: V1 does a small-N scan of the table; V2 adds a
> direct `get_by_id`. With limit=500 the lookup is fine for a
> single-tenant pilot; don't rely on it for a thousand-record page.

## `POST /inconclusives`

Insert an inconclusive. Used by tests; not a public write API in V1.

## `GET /insight-runs`

Audit history of rule executions, newest first.

**Query parameters**:
- `rule_name: str?`
- `status: 'running' | 'success' | 'failed' | 'partial'?`
- `limit: int = 50` (1..500)

**Response 200**:
```json
[
  {
    "id": "…",
    "rule_name": "rds_eol",
    "status": "success",
    "started_at": "2026-07-18T12:34:56Z",
    "finished_at": "2026-07-18T12:34:57Z",
    "resources_scanned": 17,
    "insights_emitted": 3,
    "error": null
  }
]
```

This is the trace that answers "what did the rds_eol rule do at
14:00 yesterday?".

## `POST /collect/aws`

Trigger a cross-account AWS scan. Synchronous in V1: the response
arrives when the scan is done (or has failed per-region).

**Request body**:
```json
{
  "targets": [
    {
      "aws_account_id": "111111111111",
      "role_arn": "arn:aws:iam::111111111111:role/ConstatReadOnly",
      "external_id": "<shared secret>",
      "name": "prod-eu",
      "regions": ["eu-west-1", "eu-west-3"]
    }
  ],
  "dry_run": false
}
```

- `targets`: at least one. `role_arn` null = use the base session
  (single-account mode, useful in dev). `regions` null = use the
  default set in `packages/connectors/aws_rds/src/constat_aws_rds/collector.py::DEFAULT_REGIONS`.
- `dry_run`: if `true`, scan and log but skip the writes (useful to
  validate IAM + region coverage).

**Response 200**:
```json
{
  "results": [
    {
      "aws_account_id": "111111111111",
      "regions_scanned": ["eu-west-1", "eu-west-3"],
      "resources_written": 17,
      "observations_written": 17,
      "facts_written": 68,
      "errors": []
    }
  ]
}
```

`errors` is per-region `ClientError.code` plus message. A
non-empty `errors` list does *not* abort the scan: the next region is
attempted. The `source_runs` row for the failed region is marked
`status = 'failed'`.

`AccessDenied` is a run error, not a per-resource absence. The
absence is *not* provable for that scope — the corresponding insight
evaluation emits an `Inconclusive(reason='scope_not_proven')` on the
runner pass.

## `POST /collect/focus`

Ingest a FOCUS 1.0 CSV.

**Request body**:
```json
{
  "account_external_id": "111111111111",
  "csv_path": "/srv/cur/focus-2026-07.csv",
  "account_name": "prod-eu"
}
```

- `csv_path` is a server-side path. The server process must have read
  access. File upload (multipart) is V2.
- `account_name` is optional, stored on `accounts.name`.

**Response 200**:
```json
{
  "account_id": "1a2b…",
  "rows_read": 12804,
  "rows_written": 87,
  "inserted": 87,
  "updated": 0,
  "duration_seconds": 2.34
}
```

`rows_read` is the raw FOCUS row count; `rows_written` is the
count after the in-memory `(service, period)` aggregation
(`packages/connectors/focus/src/constat_focus/aggregator.py::aggregate_for_storage`).

**Errors**:
- 404 if `csv_path` does not exist
- 500 if the CSV is missing required FOCUS 1.0 columns (see
  `packages/connectors/focus/src/constat_focus/loader.py::FOCUS_REQUIRED_COLUMNS`)

## Error semantics (all endpoints)

- **400 Bad Request** — unknown rule name in `/insights/run`, or
  Pydantic body validation. Body: `{"detail": ...}`.
- **404 Not Found** — `insight_id` or `inconclusive_id` does not
  exist; or `csv_path` missing.
- **422 Unprocessable Entity** — Pydantic body validation when the
  model is parsed at the request boundary. Same `{"detail": ...}` shape
  with a per-field list.
- **500 Internal Server Error** — uncaught exception in a router.
  FastAPI's default. The response body is `{"detail": "Internal Server Error"}`
  and the traceback is logged.

The web app (`apps/web/lib/api.ts::ApiError`) catches these and
surfaces them as a banner; it does not crash the page.

## What's missing in V1 (and why)

These are deliberate V2 decisions, not gaps. Don't add them in V1
without a one-paragraph justification in the PR.

- **Auth.** No token check, no rate limit. Internal deployment only.
  V2: OIDC for users, OAuth2 client credentials for service
  accounts (per the GTM doc, ADR-10 of the strategic brief).
- **Async `/insights/run`.** V1 blocks. A scan over 50 accounts is
  5-10 minutes. V2: a `runs` resource with `POST /insights/run`
  returning 202 + a run id, then `GET /insights/runs/{id}` to poll.
- **Cursor-based pagination.** V1 uses `limit` + `offset`. Fine for
  the pilot volume; switch to opaque cursors in V2.
- **OpenAPI export to a file.** The spec is generated at runtime by
  FastAPI; we don't ship a checked-in `openapi.json` yet.
- **Webhooks.** Run finished, new insight — V2.
- **Bulk export.** Async export to S3 with a presigned URL — V2.

## See also

- [`../architecture.md`](../architecture.md) — the four-box view
- [`../concepts.md`](../concepts.md) — the 9 concepts
- [`../insights/rds-extended-support.md`](../insights/rds-extended-support.md) —
  the payload shape of the V1 hero insight
