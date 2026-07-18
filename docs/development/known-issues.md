# Known issues (V1)

> This document tracks schema/code drift and other traps that the tests
> don't catch. The schema source of truth is the SQL migrations in
> `db/migrations/`, **not** the ORM in `apps/api/src/constat_api/orm.py`.
> The ORM is test-only (sqlite in-memory). When they disagree, the
> production database is right and the ORM is wrong.

## 1. Drift: `facts` UNIQUE constraint (ORM vs migration 0006)

**Severity:** medium. Affects tests only (sqlite); production (Postgres)
runs migrations, so production is on the right schema. But: if you
rely on the ORM to introspect the schema, you'll see the wrong
constraint.

**Where:**
- ORM: `apps/api/src/constat_api/orm.py:143-151` declares
  `UniqueConstraint("tenant_id", "resource_id", "namespace", "key",
  "source", "observed_at", name="uq_fact_snapshot")`.
- Migrations: `0004_tenant_id.sql` created `uq_fact_snapshot` (with
  `observed_at`); `0006_facts_current_state.sql` **dropped it** and
  added `uq_fact_current` on
  `(tenant_id, resource_id, namespace, key, source)` (no
  `observed_at`).

**Symptom:** A test that creates a fresh sqlite DB, then upserts
the same fact twice in a row with different `observed_at`, will
*fail* on the ORM-created schema (because the unique includes
`observed_at`) and *succeed* on the migration-created schema (no
`observed_at` in the unique). We have tests that expect the
current-state behavior; they pass only when the migration has
been applied to a real Postgres. In a fresh sqlite test DB
without migrations, the ORM creates the wrong constraint.

**Why it happened:** the schema moved from "append-log" (with
`observed_at` in the unique) to "current-state" (without) in
migration 0006. The ORM was not updated to match.

**Fix:** one line, in the ORM:
```python
UniqueConstraint(
    "tenant_id", "resource_id", "namespace", "key", "source",
    name="uq_fact_current",  # was: "uq_fact_snapshot", with "observed_at" added
),
```
And update the related `test_facts_upsert.py` to assert
current-state behavior (one row per identity, regardless of
`observed_at`).

**Who owns the fix:** the `core` package owner (since `packages/core`
is the stable contract). Not a V1 ship blocker if production is on
Postgres; a V1 ship blocker if you run sqlite-staging as a
mirror of production.

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

**Where:** `apps/api/src/constat_api/routers/inconclusive.py::get_inconclusive_endpoint`
fetches `limit=500` rows and filters in Python.

**Why:** V1 doesn't have a `get_inconclusive_by_id` repository
method. Fine for the pilot; not fine at scale.

**Symptom:** the lookup is `O(limit)` per call, not `O(1)`.

**Fix:** add `repositories/inconclusive.py::get_inconclusive(session,
id)`. Trivial. Not a V1 ship blocker.

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

**Where:** `apps/web/app/chargeback/page.tsx` shows a static page
explaining how to populate data. It does **not** call the API
yet.

**Why:** the chargeback runner emits `insights` rows with
`account_id` set and a structured payload; the page should list
those. Wiring is a 30-line client-side fetch. Not done because
the V1 demo focuses on `/insights` (which is wired).

**Fix:** add a `listChargebackByAccount` API client + a small
table. Not a V1 ship blocker for the pilot (the customer can
filter `/insights?rule_name=chargeback` in the meantime).

**Who owns the fix:** the web workstream.

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

**Where:** `apps/web/tsconfig.json::tsBuildInfoFile`. The committed
`tsconfig.tsbuildinfo` is a Next.js cache artifact. Not in
`.gitignore`.

**Symptom:** occasional noisy diffs after `next build`.

**Fix:** add `apps/web/tsconfig.tsbuildinfo` to `.gitignore`. Not
blocking.

## Reporting new issues

When you find a new one, add a section here. Each section should
have: **severity**, **where** (file path), **symptom**, **fix**
(sketch), **owner**. If the issue blocks the V1 pilot, escalate
to the ship owner.

## See also

- [`setup.md`](./setup.md) — the dev environment
- [`running-the-stack.md`](./running-the-stack.md) — the demo path
- [`../data-model.md`](../data-model.md) — the schema
