"""baseline: schema as of SQL migration 0021

No-op anchor revision. The schema this baseline represents was produced
by the 21 SQL files in `db/migrations/_archived/` (now historical
record, not applied to fresh DBs). New revisions chain from this one.

Fresh DBs: `alembic upgrade head` runs this revision's `upgrade()` and
stamps the chain. Existing DBs at the post-0021 state are stamped with
`alembic stamp head` (no SQL emitted).

Revision ID: 8574f09d5d61
Revises:
Create Date: 2026-07-19 15:43:10.297453
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "8574f09d5d61"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: the schema is already at the post-0021 state."""


def downgrade() -> None:
    """No-op: there is no previous revision to roll back to.

    Operators with a DB at the post-0021 state that needs to be reset
    should drop the alembic_version table and re-apply the archived
    SQL files in `db/migrations/_archived/` (in order), then re-stamp
    with `alembic stamp head`. Do not run `alembic downgrade` from
    this revision; the chain has no lower anchor.
    """
