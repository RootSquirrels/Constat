"""SQLAlchemy engine, session, and FastAPI dependency."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from constat_api.settings import settings

# pool_pre_ping survives idle disconnects from RDS / managed Postgres.
engine: Engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yield a session, ensure close."""
    session = SessionLocal()
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
