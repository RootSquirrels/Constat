"""SQLAlchemy engine, session, and FastAPI dependency.

Tenant context is set per session by `get_db` (V1: hardcoded default
tenant). The actual Postgres GUC is installed lazily by the `after_begin`
event in `constat_api.tenant` — only when a transaction actually starts.
That keeps connection acquisition cheap and the GUC tied to the right
transactional window (RLS policies re-evaluate per statement).
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from constat_api.settings import settings

# Importing tenant registers the `after_begin` event listener on Session.
# It must be imported here (or anywhere in the app process) exactly once.
from constat_api.tenant import bind_tenant

# pool_pre_ping survives idle disconnects from RDS / managed Postgres.
engine: Engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yield a session bound to the V1 default tenant.

    V1 is single-tenant, so every request gets the default tenant id.
    The Postgres GUC `app.current_tenant_id` is installed by the
    `after_begin` event in `constat_api.tenant` when the first SQL
    statement runs, then re-installed after every commit/rollback.

    V2: replace this with a dep that resolves the tenant from the request
    (JWT claim or `X-Tenant-Id` header) and calls `bind_tenant` with it.
    """
    session = SessionLocal()
    bind_tenant(session, settings.default_tenant_id)
    try:
        yield session
    finally:
        session.close()


def create_all_tables(target_engine: Engine | None = None) -> None:
    """Create all tables from ORM metadata. Tests only.

    Production uses raw SQL migrations (see db/migrations/). Keep this in sync
    with db/migrations/0001_init.sql — drift between the two is a bug.
    """
    from constat_api.orm import Base

    Base.metadata.create_all(bind=target_engine or engine)
