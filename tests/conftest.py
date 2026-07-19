"""Pytest config: ensure local src/ paths are importable without uv sync.

Provides a sqlite-in-memory DB fixture, a FastAPI TestClient wired to it,
and shared test helpers (notably `make_rds_db_dict` for the boto3-style
RDS DescribeDBInstances payload that 5 test files used to duplicate).

Note on sqlite + StaticPool: a default `sqlite:///:memory:` engine gives each
pooled connection its own private in-memory database. We use StaticPool to
force a single shared connection so all sessions see the same tables.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Async collection (roadmap 1.1): the API lifespan must NOT start its
# background worker pool in tests — drains are driven deterministically
# via drain_inline_queue() below. Set BEFORE any constat_api import,
# because settings are read at module import time.
os.environ.setdefault("CONSTAT_WORKER_INLINE", "0")

ROOT = Path(__file__).resolve().parent.parent
SRC_PATHS = [
    ROOT / "packages" / "core" / "src",
    ROOT / "packages" / "connectors" / "aws_rds" / "src",
    ROOT / "packages" / "connectors" / "aws_ec2" / "src",
    ROOT / "packages" / "connectors" / "focus" / "src",
    ROOT / "packages" / "insights" / "rds_eol" / "src",
    ROOT / "packages" / "insights" / "chargeback" / "src",
    ROOT / "apps" / "api" / "src",
]
for p in SRC_PATHS:
    p_str = str(p)
    if p_str not in sys.path:
        sys.path.insert(0, p_str)


# Default ARN used by the legacy _make_db helpers we are consolidating.
# Override via the `arn` parameter when you need a different identity.
_DEFAULT_TEST_ARN = "arn:aws:rds:eu-west-1:111111111111:db:test"


def make_rds_db_dict(
    *,
    arn: str = _DEFAULT_TEST_ARN,
    identifier: str = "test",
    engine_version: str = "14.7",
    endpoint_host: str = "test.xxxx.eu-west-1.rds.amazonaws.com",
) -> dict[str, Any]:
    """Build a boto3-style RDS DescribeDBInstances item.

    Used by 5 test files (previously each had its own copy of the same
    function — UX/ops P3 item 13). Defaults match a vanilla PG14
    instance. Override `arn` / `identifier` / `engine_version` /
    `endpoint_host` for variant cases (the test_runner.py bootstrap
    needs a different identifier and endpoint for its PG14 fixture).
    """
    return {
        "DBInstanceArn": arn,
        "DBInstanceIdentifier": identifier,
        "Engine": "postgres",
        "EngineVersion": engine_version,
        "DBInstanceClass": "db.m5.xlarge",
        "DBInstanceStatus": "available",
        "AllocatedStorage": 100,
        "InstanceCreateTime": datetime(2024, 1, 1, tzinfo=UTC),
        "MultiAZ": True,
        "StorageEncrypted": True,
        "DBSubnetGroup": {"DBSubnetGroupName": "default"},
        "Endpoint": {"Address": endpoint_host},
    }


@pytest.fixture(autouse=True)
def _reset_collect_queue() -> Iterator[None]:
    """Fresh in-process collect queue per test.

    The queue is a module-level singleton shared by the API router and
    the worker; without a reset, items enqueued by one test would leak
    into the next test's drain.
    """
    from constat_api.collect_queue import reset_queue

    reset_queue()
    yield
    reset_queue()


def drain_inline_queue(session: Session, *, max_items: int = 100) -> list[Any]:
    """Deterministically drain the in-process collect queue (no sleeps).

    Runs the worker's `drain_once` against the shared queue singleton on
    the test's own session, so enqueued scans execute synchronously in
    the test thread. Returns the per-item outcomes. Nacked items are
    requeued with a not-before delay, so they are NOT returned again by
    this call — drain once, assert, done.
    """
    from constat_api.collect_queue import get_queue
    from constat_api.worker import drain_once

    return drain_once(
        lambda: session,
        get_queue(),
        max_items=max_items,
        base_session=MagicMock(),
    )


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
    # Migration 0021 columns on collect_jobs. orm.py is owned by a
    # parallel workstream and does not map them yet, so create_all cannot
    # add them — mirror the migration here (CI's Postgres job applies the
    # real migration). Drop this once the ORM catches up.
    from sqlalchemy import text

    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE collect_jobs ADD COLUMN enqueue_error TEXT"))
        conn.execute(text("ALTER TABLE collect_jobs ADD COLUMN evaluation_status TEXT"))
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
    from constat_api.audit import get_audit_db
    from constat_api.db import get_db
    from constat_api.main import app

    def _override_get_db() -> Iterator[Session]:
        try:
            yield session
        finally:
            pass  # session lifecycle owned by the fixture

    app.dependency_overrides[get_db] = _override_get_db
    # The audit-write dep (read attribution, CISO 3.3) gets the same test
    # session: in production it MUST be an independent session (see
    # get_audit_db), but in tests pointing it at the real Postgres URL
    # would make every read-endpoint test hit the network. Using the
    # shared in-memory session also lets tests assert on the audit rows.
    app.dependency_overrides[get_audit_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
