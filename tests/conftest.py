"""Pytest config: ensure local src/ paths are importable without uv sync.

Provides a sqlite-in-memory DB fixture and a FastAPI TestClient wired to it.

Note on sqlite + StaticPool: a default `sqlite:///:memory:` engine gives each
pooled connection its own private in-memory database. We use StaticPool to
force a single shared connection so all sessions see the same tables.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parent.parent
SRC_PATHS = [
    ROOT / "packages" / "core" / "src",
    ROOT / "packages" / "connectors" / "aws_rds" / "src",
    ROOT / "packages" / "connectors" / "focus" / "src",
    ROOT / "packages" / "insights" / "rds_eol" / "src",
    ROOT / "packages" / "insights" / "chargeback" / "src",
    ROOT / "apps" / "api" / "src",
]
for p in SRC_PATHS:
    p_str = str(p)
    if p_str not in sys.path:
        sys.path.insert(0, p_str)


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory sqlite engine. StaticPool keeps one connection so tables persist."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    from constat_api.orm import Base

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    """FastAPI TestClient wired to the in-memory test DB session via dep override."""
    from constat_api.db import get_db
    from constat_api.main import app

    def _override_get_db() -> Iterator[Session]:
        try:
            yield session
        finally:
            pass  # session lifecycle owned by the fixture

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
