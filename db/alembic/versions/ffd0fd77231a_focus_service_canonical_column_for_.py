"""focus: service_canonical column for cross-provider service name

Revision ID: ffd0fd77231a
Revises: 8574f09d5d61
Create Date: 2026-07-19 17:18:31.164806

Roadmap-consolidation §II.1: a new nullable column `service_canonical`
on `focus_charges` carries the cross-provider canonical name resolved
from the service catalog (e.g. AWS "Amazon RDS" + Azure "Azure
Database for PostgreSQL" both canonical to "managed_postgres"). The
upsert dedup key uses `COALESCE(service_canonical, service)` so
pre-catalog rows continue to aggregate by their native service
name — the canonical takes over as soon as the catalog populates it.

Adding the column is a pure no-op for live data (nullable, no default
that would change the storage key). A follow-up backfill
`UPDATE focus_charges SET service_canonical = <catalog lookup>`
can run as a separate one-off; until then, new ingests set the
column and old rows dedup by their native name.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ffd0fd77231a"
down_revision: str | Sequence[str] | None = "8574f09d5d61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable service_canonical column on focus_charges."""
    op.add_column(
        "focus_charges",
        sa.Column("service_canonical", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Drop the service_canonical column.

    The dedup key returns to the legacy `service` column; the
    chargeback resolver falls back to native service names. Down-
    grading is non-destructive (no data is lost — `service` was
    always populated) but a backfill is needed to restore the
    cross-provider merge if the downgrade is followed by an upgrade.
    """
    op.drop_column("focus_charges", "service_canonical")
