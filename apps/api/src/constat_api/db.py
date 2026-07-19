"""SQLAlchemy engine, session, and FastAPI dependency.

Tenant context is set per session by `get_db` from the authenticated
principal (roadmap 3.1): the API key's configured tenant is installed
into the Postgres GUC `app.current_tenant_id`, and RLS does the rest.
The GUC itself is installed lazily by the `after_begin` event in
`constat_api.tenant` — only when a transaction actually starts. That
keeps connection acquisition cheap and the GUC tied to the right
transactional window (RLS policies re-evaluate per statement).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from constat_api.auth import Principal, optional_principal
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


def get_db(principal: Annotated[Principal, Depends(optional_principal)]) -> Iterator[Session]:
    """FastAPI dependency: yield a session bound to the caller's tenant.

    The tenant comes from the authenticated principal (the API key's
    configured tenant, roadmap 3.1) — never from the request. Anonymous
    callers (auth open, or an open route like /health) get the V1
    default tenant, unchanged from before. The Postgres GUC
    `app.current_tenant_id` is installed by the `after_begin` event in
    `constat_api.tenant` when the first SQL statement runs, then
    re-installed after every commit/rollback.

    `optional_principal` (not `verify_api_key`) is used here on purpose:
    /health depends on `get_db` and must stay open. Protected routes
    enforce `verify_api_key` at the router level, so an unauthenticated
    request never reaches a protected handler — this dep only chooses
    the tenant, and FastAPI dedupes the settings/header sub-dependencies
    with the router-level auth.
    """
    session = SessionLocal()
    bind_tenant(session, principal.tenant_id)
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
