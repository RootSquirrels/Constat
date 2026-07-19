db/migrations/_archived — historical SQL migrations

These 21 hand-written SQL files were the canonical schema-migration
path through 2026-07-19 (migration 0021). On 2026-07-19 the project
moved to Alembic; the live migration infrastructure now lives in
`db/alembic/` and the canonical entry point is:

    uv run alembic -c db/alembic.ini upgrade head

The first Alembic revision is a no-op baseline that anchors the
revision chain at the schema produced by these SQL files
(see ADR-17). New DBs run Alembic from scratch; existing DBs at the
post-0021 state are stamped (`alembic stamp head`).

These files are kept here for three reasons:

- **Audit trail.** The git history of why each table/index/RLS
  policy exists is in the diff of these files. Autogenerate produces
  the schema, not the reasoning.
- **Reconciliation.** A future Alembic revision will absorb the
  RLS policies, grants, and indexes that exist in these files but
  are not expressed in the ORM (see ADR-17, "open items"). Until
  then, applying these files to a fresh DB and then running
  `alembic stamp head` reproduces the V1 prod schema.
- **Pre-2026-07-19 deployments.** Operators bootstrapping a
  pre-Alembic env still use these; the bootstrap doc references
  them in the historical section.

Do not edit these files. The canonical schema is now in
`apps/api/src/constat_api/orm.py`.
