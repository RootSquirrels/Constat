"""End-to-end tenant isolation proof (roadmap 3.1): API key -> principal
-> session GUC -> RLS.

Runs only against a live Postgres (`CONSTAT_TEST_DATABASE_URL`, CI
Postgres job), because RLS is Postgres-only — on sqlite the GUC
machinery is a no-op and there is nothing to isolate.

Scenario: two API keys configured on two different tenants. Rows are
seeded directly in SQL (one insight per tenant), then the REAL `get_db`
(no dependency override) resolves the principal from each key and binds
its tenant. Tenant A must see exactly its own insight through
GET /insights, tenant B exactly its own, and each must be blind to the
other. That is the whole chain — FastAPI dependency, `bind_tenant`,
`after_begin` GUC install, FORCE RLS policy — exercised in one request.

WRITE LEG: repositories now stamp the session's tenant on insert
(`tenant_or_default`), so POST /insights under tenant A's key passes
the RLS WITH CHECK (pre-chantier the ORM default tenant made it fail
closed) and the fresh row is visible to A and invisible to B.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import pytest
from constat_api import db as db_module
from constat_api.audit import get_audit_db
from constat_api.auth import Principal, _get_settings, optional_principal
from constat_api.main import app
from constat_api.settings import ApiKeyEntry, Settings
from constat_api.tenant import bind_tenant
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.environ.get("CONSTAT_TEST_DATABASE_URL")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = UUID("00000000-0000-0000-0000-00000000000b")
KEY_A = "e2e-tenant-a-key"
KEY_B = "e2e-tenant-b-key"

requires_postgres = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CONSTAT_TEST_DATABASE_URL unset — tenant e2e tests need a live database",
)


def _psycopg() -> Any:
    """Import psycopg lazily so collection never requires the driver."""
    return pytest.importorskip("psycopg", reason="psycopg driver not installed")


@pytest.fixture(scope="module")
def pg_migrated() -> Iterator[str]:
    """Fresh public schema with all migrations applied (same pattern as
    tests/test_rls.py). Yields the DSN."""
    if not DATABASE_URL:
        pytest.skip("CONSTAT_TEST_DATABASE_URL unset — tenant e2e tests need a live database")
    psycopg = _psycopg()
    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    assert migrations, f"no migrations found in {MIGRATIONS_DIR}"
    with psycopg.connect(
        DATABASE_URL, autocommit=True, cursor_factory=psycopg.ClientCursor
    ) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        for path in migrations:
            # ClientCursor = simple query protocol, the only way to run a
            # whole multi-statement migration file in one execute.
            conn.execute(path.read_text(encoding="utf-8"))
    yield DATABASE_URL


@pytest.fixture(scope="module")
def pg_seeded(pg_migrated: str) -> Iterator[str]:
    """One account + one insight per tenant, inserted with the tenant GUC
    set — the same shape the app's write path produces now that
    repositories stamp the session tenant."""
    psycopg = _psycopg()
    with psycopg.connect(pg_migrated, autocommit=True) as conn:
        for tenant_id, external_id, title in (
            (TENANT_A, "111111111111", "insight-of-tenant-A"),
            (TENANT_B, "222222222222", "insight-of-tenant-B"),
        ):
            conn.execute("SELECT set_config('app.current_tenant_id', %s, false)", (str(tenant_id),))
            account_id = conn.execute(
                "INSERT INTO accounts (tenant_id, external_id, name)"
                " VALUES (%s, %s, %s) RETURNING id",
                (str(tenant_id), external_id, f"account-{external_id}"),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO insights (tenant_id, account_id, rule_name, severity, title, payload)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb)",
                (str(tenant_id), account_id, "rds_eol", "warning", title),
            )
    yield pg_migrated


@pytest.fixture(scope="module")
def api_client(pg_seeded: str) -> Iterator[TestClient]:
    """TestClient wired to the migrated Postgres through the REAL get_db.

    `db.SessionLocal` is repointed at the test database (the dep itself
    is NOT overridden — principal resolution and bind_tenant run for
    real). Two API keys are configured, one per tenant. Manual insight
    creation is enabled so the write leg can exercise POST /insights.
    The audit-write dep is overridden only to point at the same
    database; it mirrors the real get_audit_db (principal's tenant),
    so read-attribution rows land in the acting tenant.
    """
    engine = create_engine(pg_seeded, pool_pre_ping=True, future=True)
    pg_session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    cfg = Settings(
        api_keys=(
            ApiKeyEntry(name="alice", role="operator", key=KEY_A, tenant_id=TENANT_A),
            ApiKeyEntry(name="bob", role="reader", key=KEY_B, tenant_id=TENANT_B, kind="human"),
        ),
        enable_manual_insights=True,
    )

    def _audit_db(
        principal: Annotated[Principal, Depends(optional_principal)],
    ) -> Iterator[Session]:
        session = pg_session_factory()
        bind_tenant(session, principal.tenant_id)
        try:
            yield session
        finally:
            session.close()

    original_session_local = db_module.SessionLocal
    db_module.SessionLocal = pg_session_factory
    app.dependency_overrides[_get_settings] = lambda: cfg
    app.dependency_overrides[get_audit_db] = _audit_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        db_module.SessionLocal = original_session_local
        app.dependency_overrides.pop(_get_settings, None)
        app.dependency_overrides.pop(get_audit_db, None)
        engine.dispose()


def _titles(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return sorted(item["title"] for item in response.json())


@requires_postgres
@pytest.mark.postgres
class TestTenantIsolationE2E:
    """API key -> principal -> session GUC -> RLS, against real Postgres."""

    def test_tenant_a_sees_only_its_own_insights(self, api_client: TestClient) -> None:
        response = api_client.get("/insights", headers={"X-API-Key": KEY_A})
        assert _titles(response) == ["insight-of-tenant-A"]

    def test_tenant_b_sees_only_its_own_insights(self, api_client: TestClient) -> None:
        response = api_client.get("/insights", headers={"X-API-Key": KEY_B})
        assert _titles(response) == ["insight-of-tenant-B"]

    def test_cross_tenant_row_is_invisible_not_404_leaky(self, api_client: TestClient) -> None:
        """Tenant B asking for tenant A's insight by id gets the same 404
        as for a nonexistent id — no existence oracle across tenants."""
        response = api_client.get("/insights", headers={"X-API-Key": KEY_A})
        insight_id = response.json()[0]["id"]
        as_b = api_client.get(f"/insights/{insight_id}", headers={"X-API-Key": KEY_B})
        assert as_b.status_code == 404

    def test_unknown_key_is_401(self, api_client: TestClient) -> None:
        response = api_client.get("/insights", headers={"X-API-Key": "not-a-key"})
        assert response.status_code == 401

    def test_post_insight_under_tenant_a_passes_rls_with_check(
        self, api_client: TestClient
    ) -> None:
        """The write leg: insert_insight stamps the session tenant, so the
        RLS WITH CHECK accepts the row — and it lands in tenant A only."""
        accounts = api_client.get("/accounts", headers={"X-API-Key": KEY_A})
        assert accounts.status_code == 200, accounts.text
        account_id = accounts.json()[0]["id"]

        created = api_client.post(
            "/insights",
            headers={"X-API-Key": KEY_A},
            json={
                "rule_name": "rds_eol",
                "account_id": account_id,
                "severity": "warning",
                "title": "manual-insight-of-tenant-A",
                "payload": {},
            },
        )
        assert created.status_code == 201, created.text

        # Visible to A, alongside its seeded insight.
        titles_a = _titles(api_client.get("/insights", headers={"X-API-Key": KEY_A}))
        assert titles_a == ["insight-of-tenant-A", "manual-insight-of-tenant-A"]
        # Invisible to B.
        titles_b = _titles(api_client.get("/insights", headers={"X-API-Key": KEY_B}))
        assert titles_b == ["insight-of-tenant-B"]

    def test_missing_key_is_401_on_protected_route(self, api_client: TestClient) -> None:
        response = api_client.get("/insights")
        assert response.status_code == 401

    def test_tenant_header_is_400_even_with_valid_key(self, api_client: TestClient) -> None:
        """A client may never choose its tenant — not even its own."""
        response = api_client.get(
            "/insights",
            headers={"X-API-Key": KEY_A, "X-Tenant-ID": str(TENANT_A)},
        )
        assert response.status_code == 400
        assert "API key" in response.json()["detail"]
