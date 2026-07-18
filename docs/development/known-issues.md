# Known issues (V1)

> This document tracks schema/code drift and other traps that the tests
> don't catch. The schema source of truth is the SQL migrations in
> `db/migrations/`, **not** the ORM in `apps/api/src/constat_api/orm.py`.
> The ORM is test-only (sqlite in-memory). When they disagree, the
> production database is right and the ORM is wrong.

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
multi-tenant RLS scaffolding`. The `tenant_id` column was already on
all 9 tables (migration 0004); commit `dc1bb7e` adds the policies
plus the per-session GUC binding in
`apps/api/src/constat_api/tenant.py`.

**What works today:**
- `tenant.py::bind_tenant(session, tenant_id)` registers an
  `after_begin` event that runs `SELECT set_config('app.tenant',
  '<uuid>', true)` on every new transaction.
- The 7 RLS policies are in migration 0007.

**What doesn't yet (V1):**
- The default tenant is hard-coded in
  `apps/api/src/constat_api/settings.py::DEFAULT_TENANT_ID`. V2
  wires this from a request header / service-account context.
- No `BYPASSRLS` discipline: the API role is the migration owner
  for now. The doc's §11.2 promise — "rôle runtime non-owner, non-
  superuser, sans BYPASSRLS" — is **not yet enforced**. Don't
  promote to multi-tenant until this is fixed.

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

**Where:** `apps/api/src/constat_api/cli/focus.py::ingest_focus_csv`
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

**Who owns the fix:** the workstream that adds tag-based chargeback
(V2).

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

**Symptom:** every `git add` in this Windows dev environment
prints a warning about CRLF.

**Why:** `core.autocrlf` defaults to `true` on this checkout. Git
rewrites LF to CRLF on checkout, then back to LF on commit. The
warnings are cosmetic; the files on disk match what was committed.

**Fix:** none required. If the warnings are annoying, set
`git config core.autocrlf false` for this repo. CI runs on Linux
and doesn't care.

## 7. `tsbuildinfo` in `apps/web/tsconfig.json`

**Status:** FIXED in commit `0f413a6`. Added
`apps/web/tsconfig.tsbuildinfo` to `.gitignore`.

**Original report:** the committed `tsconfig.tsbuildinfo` is a
Next.js cache artifact, not in `.gitignore`. Caused noisy diffs
after `next build`.

**Fix applied:** added the file to the root `.gitignore`.

## Reporting new issues

When you find a new one, add a section here. Each section should
have: **severity**, **where** (file path), **symptom**, **fix**
(sketch), **owner**. If the issue blocks the V1 pilot, escalate
to the ship owner.

## See also

- [`setup.md`](./setup.md) — the dev environment
- [`running-the-stack.md`](./running-the-stack.md) — the demo path
- [`../data-model.md`](../data-model.md) — the schema
