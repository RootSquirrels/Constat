db/migrations

This directory used to hold the hand-written SQL migration chain
(0001..0021, applied in order via the dev bootstrap's `psql -f`
loop). On 2026-07-19 the project moved to Alembic; the canonical
schema is now in `apps/api/src/constat_api/orm.py` and the live
migration tooling is in `db/alembic/`.

The 21 historical SQL files live in `_archived/` for the reasons
explained in that subdirectory's README. Do not apply them to a
fresh DB — they pre-date the RLS policy fine-tuning in 0011 and the
runtime role in 0012, and applying them in order without the
follow-up Alembic revisions will leave the schema in a state the
API doesn't expect.

For new deployments:

    uv run alembic -c db/alembic.ini upgrade head

For pre-2026-07-19 deployments upgrading to the Alembic chain,
see ADR-17 for the `alembic stamp head` procedure.
