# ADR-17 — Alembic adoption, baseline at SQL migration 0021

**Status:** accepted (2026-07-19) — closes the AGENTS.md "Alembic later"
line that had been carrying the open question since the project started.

## Context

Twenty-one hand-written SQL files (`db/migrations/0001_…0021_…`) had
been the canonical schema path since 2026-04. Two recurring problems
made the cost of staying on raw SQL exceed the cost of switching:

1. **Drift.** The ORM (`apps/api/src/constat_api/orm.py`) and the SQL
   chain diverged at every schema change. The known-issues doc carried
   a running list (the `facts` UNIQUE in 0006, the RLS policies in
   0007/0011, the runtime role in 0012, the insight_acks columns in
   0013, the per-row costs in 0020). Each new ORM change required
   hand-writing a parallel SQL migration; each new SQL migration needed
   a parallel ORM edit. Two sources of truth for the same schema.

2. **Surface area.** The list of "things the next migration needs to do"
   kept growing: account_id provider discrimination (Azure, see
   separate plan), RLS for the next tenant-scoped table, BCRYPT
   indexes for the auth-key tables, the `external_id_set` write-only
   pattern for AssumeRole ExternalId. Each required a SQL craft
   document, a `tests/test_rls.py` update, a CI step, and a docker
   mount. The compile-test loop was long enough that mistakes slipped
   in (the AuditCurrency miss was caught in the audit, not in CI).

The user-visible promise ("in 2h of connection, we prove what you
don't know") doesn't care which migration tool we use, but the
*change velocity* it implies does — every prospect-driven change
needs a migration, and the migration cost is a direct hit on the
loop.

## Decision

Adopt Alembic (`db/alembic/`), keep the 21 SQL files as
`db/migrations/_archived/` (historical record, not applied to fresh
DBs).

- The first revision is a no-op baseline (`8574f09d5d61`) that
  anchors the chain at the schema produced by SQL migrations
  0001..0021. New DBs run Alembic from scratch; existing DBs at the
  post-0021 state are stamped (`alembic stamp head`).
- The ORM is the single source of truth. Future revisions are
  generated with `alembic revision --autogenerate -m "..."` from
  the ORM diff. RLS policies, the runtime role grant, and the
  indexes the SQL chain had but the ORM didn't are not picked up by
  autogenerate — they're hand-written in future revisions as
  `op.execute("...")` blocks, with a per-policy test in
  `tests/test_rls.py` (the CI guard).
- `env.py` reads `CONSTAT_DATABASE_URL` (the same env var the API
  uses) and points at `constat_api.orm.Base.metadata` for
  autogenerate. `compare_type=True` is set so column-type changes
  (length, nullable) are picked up — the silent-drift class that
  bit 0019 (`BillingCurrency` nullable → NOT NULL).
- boto3 is a dev-dep: the alembic CLI transitively imports
  `constat_api.settings` (which imports boto3) when it loads the
  ORM. The `uv sync` dev env includes it via the root
  `pyproject.toml`'s dev-deps. (A lazier-import refactor in
  `settings.py` would clean this up; out of scope here.)

## Consequences

- Schema changes are a one-line ORM edit + `alembic revision
  --autogenerate` + a `tests/test_rls.py` extension when an RLS
  policy is involved. The hand-written-SQL step is gone.
- The 21 archived SQL files are immutable historical record. The
  "audit F-04 closed by migration 0011" prose in `known-issues.md`
  is still meaningful — those files explain *why* each table has
  the columns and policies it does.
- The `tests/conftest.py` path (sqlite in-memory + `Base.metadata
  .create_all()`) is unchanged. Tests still bypass Alembic; the
  test path and the migration path converge on the same ORM.
- CI applies migrations via Alembic against a service-container
  Postgres, exactly as it did with the SQL files. The CI failure
  mode (broken chain / model vs DB drift) is the same; the surface
  is just one `alembic upgrade head` instead of a `for f in
  db/migrations/*.sql` loop.
- docker-compose no longer mounts `db/migrations/` into
  `/docker-entrypoint-initdb.d/`. The operator runs `alembic
  upgrade head` after `docker compose up -d` (the bootstrap doc
  spells this out).

## Open items (not blockers)

- The RLS policies (0007, 0011), the runtime role grant (0012),
  and a handful of indexes still live in the archived SQL because
  the ORM doesn't model them. A follow-up Alembic revision
  (or `op.execute` blocks in future revisions) is the way to
  bring them into the chain. Until then, `tests/test_rls.py` is
  the proof that the production schema is consistent end-to-end.
- `boto3-stubs[rds]` is still in dev-deps; the actual `boto3` is
  now a sibling dev-dep for the reason above. A lazy-import
  refactor in `settings.py` would let the dev env drop the
  runtime `boto3` (keeping only the stubs), but that's a separate
  concern.
- `tool.uv.dev-dependencies` is deprecated in favor of
  `dependency-groups.dev`. Not in scope for this change.
