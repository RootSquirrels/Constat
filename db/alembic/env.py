"""Alembic environment for Constat.

Loads the workspace src/ paths so `constat_api.*` and friends are
importable, then points Alembic at the ORM's metadata (the single
source of truth) and at the database URL from the `CONSTAT_DATABASE_URL`
env var (overrides `alembic.ini`).

The bootstrap (AGENTS.md) invokes alembic as `uv run alembic -c
db/alembic.ini upgrade head` from the workspace root. The pythonpath
below mirrors `pyproject.toml`'s `[tool.pytest.ini_options] pythonpath`
so dev / CI / alembic all resolve imports the same way.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make workspace src/ importable from this script (alembic runs from
# the location of alembic.ini, which is db/, not the workspace root).
ROOT = Path(__file__).resolve().parents[2]
for src in (
    "packages/core/src",
    "packages/connectors/aws_rds/src",
    "packages/connectors/aws_ec2/src",
    "packages/connectors/focus/src",
    "packages/insights/rds_eol/src",
    "packages/insights/mysql_eol/src",
    "packages/insights/aurora_eol/src",
    "packages/insights/ebs_gp2_to_gp3/src",
    "packages/insights/ebs_unattached/src",
    "packages/insights/snapshot_orphan/src",
    "packages/insights/ec2_stopped_with_storage/src",
    "packages/insights/chargeback/src",
    "apps/api/src",
):
    p_str = str(ROOT / src)
    if p_str not in sys.path:
        sys.path.insert(0, p_str)

# `constat_api.settings` is read for `database_url` in the API path,
# but importing it transitively pulls boto3 (the API's runtime dep)
# and that's overkill for the migration CLI. Read the env var directly
# — same knob, no transitive import.
DATABASE_URL = os.environ.get("CONSTAT_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "CONSTAT_DATABASE_URL is not set. The alembic env reads the same env "
        "var the API uses; export it before running `alembic upgrade head`."
    )

from constat_api.orm import Base  # noqa: E402

config = context.config

# Single source of truth for the DB URL: env var CONSTAT_DATABASE_URL,
# the same knob the API uses. The `sqlalchemy.url` line in alembic.ini
# is overridden here so dev / prod / CI never diverge.
config.set_main_option("sqlalchemy.url", DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `compare_type=True`: autogenerate detects column-type changes (length,
# nullable, enum values), not just adds/drops. Cheap and catches the
# drift the SQL-migration era kept producing (e.g. 0019 changing
# BillingCurrency to NOT NULL after it was nullable).
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL scripts without a live DB connection.

    Used to render migrations for the operator to review / apply
    manually (e.g. on a managed Postgres where the API can't open a
    session for Alembic).
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection.

    Tenant GUC is irrelevant here: alembic operates as the migration
    owner, not as a tenant-scoped role — RLS is bypassed for the
    schema-owning role on managed Postgres deployments.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
