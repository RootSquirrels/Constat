# Known issues (V1)

> This document tracks schema/code drift and other traps that the tests
> don't catch. The schema source of truth is the ORM in
> `apps/api/src/constat_api/orm.py`; Alembic (`db/alembic/`, see
> ADR-17) autogenerates revisions from it. The 21 historical SQL
> files under `db/migrations/_archived/` are the pre-Alembic record
> and must not be re-applied to a fresh DB.
>
> The reverse-direction drift is the live concern: RLS policies, the
> runtime role grant (`0012`), and a few indexes still live in the
> archived SQL because the ORM doesn't model them. A follow-up
> Alembic revision (or per-table `op.execute` calls inside future
> revisions) is the way to bring them into the ORM, but until that
> happens `tests/test_rls.py` (Postgres CI) is the proof that the
> production schema is consistent end-to-end.

## 1. Drift: `facts` UNIQUE constraint (ORM vs migration 0006)

**Status:** FIXED in commit `9c3e8b4` (post-V1-commit-series). ORM now
matches migration 0006.

**Original report:** the ORM declared
`UniqueConstraint("tenant_id", "resource_id", "namespace", "key",
"source", "observed_at", name="uq_fact_snapshot")` while migration
0006 had replaced it with `uq_fact_current` on the same columns
*without* `observed_at`. SQLite-staging would have created the wrong
constraint.

**Fix applied:** the ORM's `FactORM.__table_args__` now declares
`UniqueConstraint("tenant_id", "resource_id", "namespace", "key",
"source", name="uq_fact_current")` — matches migration 0006
exactly. `test_tenant_id.py::test_facts_with_different_observed_at_allowed`
was removed (it tested the obsolete append-log behavior); the
current-state contract is fully covered in `test_facts_upsert.py`.

## 2. RLS policies (now in place, single-tenant still)

**Status:** scaffolding landed in commit `dc1bb7e feat(api+db):
multi-tenant RLS scaffolding`; audit F-04 closed by migration 0011.
Every tenant-scoped table now has a policy.

**What works today:**
- `tenant.py::bind_tenant(session, tenant_id)` registers an
  `after_begin` event that runs `SELECT set_config('app.current_tenant_id',
  '<uuid>', true)` on every new transaction.
- 13 RLS policies: migration 0007 covers the 9 original tables,
  migration 0011 covers the 4 tables added later without policies
  (`focus_charge_tags`, `audit_events`, `retention_policies`,
  `pii_classifications` — audit F-04). `tests/test_rls.py`
  (Postgres-marked) fails in CI if a new tenant-scoped table ever
  ships without a policy.

**What doesn't yet (V1):**
- The default tenant is hard-coded in
  `apps/api/src/constat_api/settings.py::DEFAULT_TENANT_ID`. V2
  wires this from a request header / service-account context
  (roadmap: gated on a real tenant #2).
- ~~No `BYPASSRLS` discipline~~ **FIXED by migration 0012**: the
  doc's §11.2 promise — "rôle runtime non-owner, non-superuser,
  sans BYPASSRLS" — is now enforced. `constat_app` owns nothing,
  cannot ALTER POLICY (CI-proven in `tests/test_rls.py::
  TestRuntimeRole`). Remaining: the runtime must actually RUN as
  `constat_app` in the pilot env (see `docs/operations/deployment.md`);
  local dev still uses the owner role.
- RLS is verified **only** via the CI Postgres job (Postgres
  service container + `CONSTAT_TEST_DATABASE_URL`). Local runs skip
  those tests unless you set the env var yourself.

**Who owns the follow-up:** the V1 ship owner. Flag in the
PR-description if you touch the API role.

## 3. `inconclusives/{id}` does a small-N scan

**Status:** FIXED in commit `0f413a6`. The router now uses
`repo.get_inconclusive(session, id)` which is O(1) via `session.get()`.

**Original report:** the endpoint fetched `limit=500` rows and
filtered in Python — `O(limit)` per call, not `O(1)`. Fine for the
pilot; not fine at scale.

**Fix applied:** added
`apps/api/src/constat_api/repositories/inconclusive.py::get_inconclusive`
and updated the router to use it. Lookup is now O(1) via the PK
index.

## 4. FOCUS ingestion assumes a single CSV per account

**Where:** `apps/api/src/constat_api/cli/focus.py::ingest_focus_file`
upserts on `(account, service, period)`. Re-ingesting the same
file is idempotent. Re-ingesting a *different* file that covers
the same period is also idempotent — but it **overwrites** the
totals. There is no merge logic for "this file has partial data
for the period".

**Symptom:** if a prospect exports CUR weekly and you ingest each
week, the week's CSV is treated as the whole period. The cost
totals are correct only if the export covers the full period.

**Workaround:** export once per period (monthly), ingest once. For
weekly exports, accumulate *manually* in a `cost_facts` table
(V2).

**Fix:** V2 introduces a `cost_facts` table with
`(period, account, service, source_id, billed, amortized, ...)`;
the `focus_charges` table becomes a view or a rollup.

**Tag-based chargeback (item 1 of the user request)** landed in V1
via `chargeback --tag-key Application` (HTTP body field `tag_key`).
Attribution is proportional to per-row tag counts stored in
`focus_charge_tags` (migration 0009; the V1 even-split approximation
is gone); a `__untagged__` bucket catches charges with no tag for the
key. See `docs/insights/chargeback.md` and migration 0008.

## 5. The web app's `chargeback` page is a stub

**Status:** FIXED in commit `af7e172`. The page now calls
`api.listChargeback()` and renders a per-account table with
billed / amortized / drift / severity.

**Original report:** the page was static, showing only 'how to
populate data' instructions. The chargeback runner emits insights
with a structured payload, but the page didn't render them.

**Fix applied:** `apps/web/app/chargeback/page.tsx` rewritten.
Groups insights by account, shows totals per account, table per
(account, period, service). Falls back to the static instructions
when no data exists. Build clean (6 routes, /chargeback now
dynamic).

## 6. CRLF / LF line endings

**Status:** FIXED. The repo root now has a `.gitattributes` that
sets `* text=auto eol=lf`. Git normalizes line endings to LF on
commit; Windows checkouts no longer see "LF will be replaced by
CRLF" warnings on every `git add`.

**Original report:** every `git add` in this Windows dev environment
printed a warning about CRLF because `core.autocrlf` defaulted to
`true`. The warnings were cosmetic; the files on disk matched what
was committed.

**Fix applied:** added `.gitattributes` at the repo root with
`* text=auto eol=lf` and per-extension overrides for the file
types the project actually uses (Python, TypeScript, SQL, Markdown,
shell, TOML, YAML, etc.). No more per-file autocrlf config needed.

## 7. `tsbuildinfo` in `apps/web/tsconfig.json`

**Status:** FIXED in commit `0f413a6`. Added
`apps/web/tsconfig.tsbuildinfo` to `.gitignore`.

**Original report:** the committed `tsconfig.tsbuildinfo` is a
Next.js cache artifact, not in `.gitignore`. Caused noisy diffs
after `next build`.

**Fix applied:** added the file to the root `.gitignore`.

## 8. MinIO in docker-compose is unused

**Where:** `docker-compose.yml` (`minio` service).

**Symptom:** the compose stack starts a MinIO container, but nothing
connects to it. Observation payloads live in JSONB columns
(`observations.payload`), not in object storage.

**Status:** accepted V1 debt. The S3/Parquet replay path in the
architecture doc ("observations replayable from S3/Parquet") is not
wired. Keep the service for the demo topology, or drop it when the
V2 object-storage decision lands.

## 9. Sync HTTP collection (fine ≤5 accounts)

**Where:** the AWS collector runs synchronously inside the API
process/request.

**Symptom:** a scan blocks the request for the duration of the boto3
calls. At ≤5 monitored accounts this is acceptable; beyond that the
request latency becomes a timeout risk.

**Status:** accepted V1 constraint. V2 moves collection to a
background task queue.

## 10. PII classifier records the region as PII

**Status:** FIXED. The collector no longer classifies regions. It now
records only `aws_account_id` and the target `role_arn` (`arn`) —
see `apps/api/src/constat_api/collectors/aws.py` (the `pii.record(...)`
calls after each scan).

**Original report:** AWS region names (e.g. `eu-west-1`) were
classified and hashed as if they were customer PII. Harmless but
noisy — it inflated `pii_classifications` with rows that answered a
question nobody asked.

## 11. `accounts.external_id` was globally unique

**Status:** FIXED by migration 0011 (audit F-12).

**Original report:** 0001 declared `external_id TEXT UNIQUE` with no
`tenant_id` in the key, so two tenants could never reference the same
AWS account id — breaking the MSP case (one AWS account, several
customers).

**Fix applied:** dropped the global unique, added
`UNIQUE(tenant_id, external_id)` (constraint
`uq_accounts_tenant_external`). `repositories/accounts.py` now scopes
`get_by_external_id` to the session's tenant.

## Reporting new issues

When you find a new one, add a section here. Each section should
have: **severity**, **where** (file path), **symptom**, **fix**
(sketch), **owner**. If the issue blocks the V1 pilot, escalate
to the ship owner.

## See also

- [`setup.md`](./setup.md) — the dev environment
- [`running-the-stack.md`](./running-the-stack.md) — the demo path
- [`../data-model.md`](../data-model.md) — the schema
