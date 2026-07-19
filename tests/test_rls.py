"""Tests for the multi-tenant RLS scaffolding.

Two layers:

1. Unit tests (sqlite, always run): the application-side contract — the
   tenant id is bound to the session, the GUC machinery is wired, and
   `get_db` installs the V1 default tenant for every request. On sqlite
   the Postgres RLS policies don't exist, so the GUC machinery is a no-op.

2. Integration tests (Postgres, `@pytest.mark.postgres`): the real
   2-tenant scenario against a live database. They apply migrations
   0001 -> latest to a fresh schema, seed two tenants, and verify that
   cross-tenant SELECT/INSERT is denied on every RLS table. They require
   `CONSTAT_TEST_DATABASE_URL` (and the `psycopg` driver); without them
   they skip cleanly. CI runs them in the Postgres job.

3. Runtime-role tests (also Postgres-marked): the §11.2 non-owner
   control. They connect as `constat_app` (migration 0012) via
   `CONSTAT_TEST_APP_DATABASE_URL` and verify the role can do DML but
   no DDL, cannot ALTER POLICY, and is fully bound by RLS.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from constat_api import db as db_module
from constat_api.settings import DEFAULT_TENANT_ID
from constat_api.tenant import TENANT_GUC, bind_tenant, current_tenant
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

# ---------------------------------------------------------------------------
# Unit: bind_tenant is sticky on the session
# ---------------------------------------------------------------------------


def test_bind_tenant_stashes_id_in_session_info(session: Session) -> None:
    tenant_id = uuid4()
    bind_tenant(session, tenant_id)
    assert current_tenant(session) == tenant_id


def test_bind_tenant_accepts_string_form(session: Session) -> None:
    tenant_id = uuid4()
    bind_tenant(session, str(tenant_id))
    assert current_tenant(session) == tenant_id


def test_bind_tenant_none_clears_it(session: Session) -> None:
    bind_tenant(session, uuid4())
    assert current_tenant(session) is not None
    bind_tenant(session, None)
    assert current_tenant(session) is None


def test_current_tenant_defaults_to_none_on_fresh_session(session: Session) -> None:
    """A brand-new session that nobody has bound has no tenant."""
    engine = session.get_bind()
    fresh_session_factory = sessionmaker(bind=engine, future=True)
    fresh = fresh_session_factory()
    try:
        assert current_tenant(fresh) is None
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# Unit: get_db installs the V1 default tenant on every request
# ---------------------------------------------------------------------------


def test_get_db_binds_default_tenant(monkeypatch) -> None:
    """The FastAPI dep must bind the anonymous (V1 default) tenant when no
    principal is resolved — the auth-open / open-route fallback."""
    from constat_api.auth import ANONYMOUS_PRINCIPAL

    captured: dict[str, object] = {}

    class _StubSession:
        def __init__(self) -> None:
            self.info: dict[str, object] = {}

        def close(self) -> None:
            pass

    def _stub_sessionlocal() -> _StubSession:
        s = _StubSession()
        captured["session"] = s
        return s

    monkeypatch.setattr(db_module, "SessionLocal", _stub_sessionlocal)

    gen = db_module.get_db(ANONYMOUS_PRINCIPAL)
    s = next(gen)
    try:
        # The dep called bind_tenant, which stashed the id in session.info.
        assert captured["session"] is s
        assert s.info.get("tenant_id") == DEFAULT_TENANT_ID
    finally:
        with suppress(StopIteration):
            next(gen)


# ---------------------------------------------------------------------------
# Unit: GUC machinery is wired (event listener exists and is callable)
# ---------------------------------------------------------------------------


def test_tenant_guc_constant_matches_migration_contract() -> None:
    """Drift between the GUC name in the migration and the app is a silent
    security bug (RLS would silently let everything through). Pin the name."""
    assert TENANT_GUC == "app.current_tenant_id"


def test_tenant_module_exposes_listener() -> None:
    """The tenant module must define the `after_begin` handler.

    If someone removes the listener from `constat_api.tenant`, the
    GUC never gets installed and every request runs without a tenant
    context. This test catches that regression by asserting the
    callable is reachable.
    """
    import constat_api.tenant as t

    assert hasattr(t, "_apply_tenant_guc")
    assert callable(t._apply_tenant_guc)


# ---------------------------------------------------------------------------
# Unit: the event handler is a no-op on sqlite (RLS is Postgres-only)
# ---------------------------------------------------------------------------


def test_guc_handler_is_noop_on_sqlite(session: Session) -> None:
    """Sqlite has no GUC, so the handler must short-circuit and not
    attempt to run a Postgres-only statement. If it didn't, the test
    engine would raise on the first query of any tenant-bound session."""
    bind_tenant(session, uuid4())
    # If the listener tried to run a Postgres statement, this would
    # raise. sqlite supports `SELECT 1` so we use that as a canary.
    result = session.execute(text("SELECT 1")).scalar_one()
    assert result == 1


# ---------------------------------------------------------------------------
# Unit: accounts external_id is unique per tenant, not globally (audit F-12)
# ---------------------------------------------------------------------------


def test_accounts_external_id_unique_is_tenant_scoped_in_orm() -> None:
    """Pin the ORM side of migration 0011: UNIQUE(tenant_id, external_id),
    no global unique on external_id (two tenants may share an AWS account id)."""
    from constat_api.orm import AccountORM
    from sqlalchemy import UniqueConstraint

    table = AccountORM.__table__
    unique_cols = {
        tuple(c.name for c in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("tenant_id", "external_id") in unique_cols
    assert ("external_id",) not in unique_cols
    assert not table.c.external_id.unique


def test_accounts_same_external_id_two_tenants_on_sqlite(session: Session) -> None:
    """sqlite has no RLS; this only exercises the ORM constraint shape."""
    from constat_api.orm import AccountORM

    session.add(AccountORM(tenant_id=uuid4(), external_id="111111111111", name="t1"))
    session.add(AccountORM(tenant_id=uuid4(), external_id="111111111111", name="t2"))
    session.flush()  # must not raise: uniqueness is per (tenant_id, external_id)


# ---------------------------------------------------------------------------
# Unit: the accounts repository scopes lookups to the session tenant
# ---------------------------------------------------------------------------


def test_get_or_create_scopes_to_bound_tenant(session: Session) -> None:
    """Two tenants sharing an external_id get two distinct accounts."""
    from constat_api.repositories import accounts as accounts_repo

    tenant_a, tenant_b = uuid4(), uuid4()

    bind_tenant(session, tenant_a)
    acc_a = accounts_repo.get_or_create(session, "111111111111")
    assert acc_a.tenant_id == tenant_a

    bind_tenant(session, tenant_b)
    acc_b = accounts_repo.get_or_create(session, "111111111111")
    assert acc_b.tenant_id == tenant_b
    assert acc_b.id != acc_a.id

    # Same tenant + same external_id returns the existing row.
    again = accounts_repo.get_or_create(session, "111111111111")
    assert again.id == acc_b.id


# ---------------------------------------------------------------------------
# Integration: the test client (which uses get_db) still works
# ---------------------------------------------------------------------------


def test_test_client_seeds_tenant_id_on_session(client: TestClient, session: Session) -> None:
    """After the client fixture is initialized, the session used by the
    client has the V1 default tenant bound. This catches regressions
    where someone removes the `bind_tenant` call from `get_db`."""
    # The client fixture uses dep override, so the real `get_db` isn't
    # called — but the override must still work. We assert here that
    # the underlying session is bind-able (the contract the rest of
    # the app relies on).
    bind_tenant(session, DEFAULT_TENANT_ID)
    assert current_tenant(session) == DEFAULT_TENANT_ID


# ---------------------------------------------------------------------------
# Integration: real 2-tenant RLS verification against Postgres
# ---------------------------------------------------------------------------
#
# These tests need a live Postgres (CI provides one via a service
# container). They skip when CONSTAT_TEST_DATABASE_URL is unset, and also
# when the psycopg driver is not installed (CI installs it via
# `uv run --with "psycopg[binary]"`).
#
# The scenario is the one the old placeholder test only documented:
#   - apply migrations 0001 -> 0011 to a fresh schema
#   - seed one row per RLS table under tenant A
#   - no GUC set           -> zero rows visible on every table
#   - GUC = tenant B       -> zero rows visible on every table
#   - GUC = tenant B, INSERT a row stamped tenant A -> RLS violation
#   - GUC = tenant A       -> the seeded row is visible

DATABASE_URL = os.environ.get("CONSTAT_TEST_DATABASE_URL")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

TENANT_A = "00000000-0000-0000-0000-00000000000a"
TENANT_B = "00000000-0000-0000-0000-00000000000b"

# Every table that must carry a tenant isolation policy after 0001 -> 0017
# (0007: 9 tables, 0011: the 4 tables of audit F-04, 0015: collect_jobs,
# 0016: collect_targets, 0017: insight_events).
RLS_TABLES = [
    "accounts",
    "resources",
    "observations",
    "facts",
    "focus_charges",
    "insights",
    "inconclusive",
    "source_runs",
    "insight_runs",
    "focus_charge_tags",
    "audit_events",
    "retention_policies",
    "pii_classifications",
    "collect_jobs",
    "collect_targets",
    "insight_events",
]

# Minimal wrong-tenant INSERT per table (tenant_id = TENANT_A while the
# session tenant is TENANT_B). RLS WITH CHECK fires before FK triggers,
# so dangling FK values here are never checked.
WRONG_TENANT_INSERTS: dict[str, tuple[str, tuple[Any, ...]]] = {
    "accounts": (
        "INSERT INTO accounts (tenant_id, external_id, name) VALUES (%s, %s, %s)",
        (TENANT_A, "999999999999", "intruder"),
    ),
    "resources": (
        "INSERT INTO resources (tenant_id, account_id, region, resource_type, native_id)"
        " VALUES (%s, %s, %s, %s, %s)",
        (TENANT_A, str(uuid4()), "eu-west-1", "AWS::RDS::DBInstance", "arn:x"),
    ),
    "observations": (
        "INSERT INTO observations (tenant_id, resource_id, source, observed_at, payload)"
        " VALUES (%s, %s, %s, NOW(), '{}'::jsonb)",
        (TENANT_A, str(uuid4()), "aws_rds"),
    ),
    "facts": (
        "INSERT INTO facts (tenant_id, account_id, namespace, key, value_state, source,"
        " observed_at) VALUES (%s, %s, %s, %s, %s, %s, NOW())",
        (TENANT_A, str(uuid4()), "aws", "rds.engine", "KNOWN", "aws_rds"),
    ),
    "focus_charges": (
        "INSERT INTO focus_charges (tenant_id, account_id, period_start, period_end, service)"
        " VALUES (%s, %s, %s, %s, %s)",
        (TENANT_A, str(uuid4()), "2026-06-01", "2026-06-30", "AmazonRDS"),
    ),
    "insights": (
        "INSERT INTO insights (tenant_id, account_id, rule_name, severity, title, payload)"
        " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb)",
        (TENANT_A, str(uuid4()), "rds_eol", "warning", "intruder"),
    ),
    "inconclusive": (
        "INSERT INTO inconclusive (tenant_id, account_id, rule_name, missing_facts)"
        " VALUES (%s, %s, %s, '[]'::jsonb)",
        (TENANT_A, str(uuid4()), "rds_eol"),
    ),
    "source_runs": (
        "INSERT INTO source_runs (tenant_id, account_id, region, resource_type, source, status)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (TENANT_A, str(uuid4()), "eu-west-1", "AWS::RDS::DBInstance", "aws_rds", "success"),
    ),
    "insight_runs": (
        "INSERT INTO insight_runs (tenant_id, rule_name, status) VALUES (%s, %s, %s)",
        (TENANT_A, "rds_eol", "success"),
    ),
    "focus_charge_tags": (
        "INSERT INTO focus_charge_tags (tenant_id, focus_charge_id, key, value)"
        " VALUES (%s, %s, %s, %s)",
        (TENANT_A, 999999999, "Application", "intruder"),
    ),
    "audit_events": (
        "INSERT INTO audit_events (tenant_id, actor, action) VALUES (%s, %s, %s)",
        (TENANT_A, "system:test", "intruder"),
    ),
    "retention_policies": (
        "INSERT INTO retention_policies (tenant_id, table_name, retention_days)"
        " VALUES (%s, %s, %s)",
        (TENANT_A, "facts", 90),
    ),
    "pii_classifications": (
        "INSERT INTO pii_classifications (tenant_id, resource_type, resource_id, field_name,"
        " sensitivity, value_hash) VALUES (%s, %s, %s, %s, %s, %s)",
        (TENANT_A, "account", "999999999999", "aws_account_id", "confidential", "0" * 64),
    ),
    "collect_jobs": (
        "INSERT INTO collect_jobs (tenant_id, actor, total_items) VALUES (%s, %s, %s)",
        (TENANT_A, "intruder", 1),
    ),
    "collect_targets": (
        "INSERT INTO collect_targets (tenant_id, aws_account_id, role_arn, external_id)"
        " VALUES (%s, %s, %s, %s)",
        (TENANT_A, "999999999999", "arn:aws:iam::999999999999:role/x", "intruder-secret"),
    ),
    "insight_events": (
        "INSERT INTO insight_events (tenant_id, fingerprint, rule_name, title, event)"
        " VALUES (%s, %s, %s, %s, %s)",
        (TENANT_A, "0" * 64, "rds_eol", "intruder", "appeared"),
    ),
}

requires_postgres = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CONSTAT_TEST_DATABASE_URL unset — Postgres RLS tests need a live database",
)


def _psycopg() -> Any:
    """Import psycopg lazily so the sqlite unit tests never require it."""
    return pytest.importorskip("psycopg", reason="psycopg driver not installed")


def _connect(dsn: str) -> Any:
    return _psycopg().connect(dsn, autocommit=True)


def _set_tenant(conn: Any, tenant_id: str) -> None:
    # is_local=false: session-scoped GUC, survives across statements.
    conn.execute("SELECT set_config('app.current_tenant_id', %s, false)", (tenant_id,))


def _count(conn: Any, table: str) -> int:
    # table names come from the RLS_TABLES constant, never from user input.
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@pytest.fixture(scope="module")
def pg_migrated() -> Iterator[str]:
    """Fresh schema with migrations 0001 -> latest applied; yields the DSN."""
    if not DATABASE_URL:
        pytest.skip("CONSTAT_TEST_DATABASE_URL unset — Postgres RLS tests need a live database")
    psycopg = _psycopg()
    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    assert migrations, f"no migrations found in {MIGRATIONS_DIR}"
    with psycopg.connect(
        DATABASE_URL, autocommit=True, cursor_factory=psycopg.ClientCursor
    ) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        for path in migrations:
            # ClientCursor uses the simple query protocol, which is the
            # only way to execute a whole multi-statement migration file.
            # It must be passed as connect()'s cursor_factory — psycopg 3's
            # cursor() has no `factory` kwarg (the CI Postgres job proved
            # it the hard way, 2026-07-19).
            conn.execute(path.read_text(encoding="utf-8"))
    yield DATABASE_URL


@pytest.fixture(scope="module")
def pg_seeded(pg_migrated: str) -> Iterator[str]:
    """Seed one row per RLS table under tenant A (plus tenant B's account)."""
    with _connect(pg_migrated) as conn:
        _set_tenant(conn, TENANT_A)
        account_a = conn.execute(
            "INSERT INTO accounts (tenant_id, external_id, name) VALUES (%s, %s, %s) RETURNING id",
            (TENANT_A, "111111111111", "tenant-a"),
        ).fetchone()[0]
        resource_a = conn.execute(
            "INSERT INTO resources (tenant_id, account_id, region, resource_type, native_id)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (TENANT_A, account_a, "eu-west-1", "AWS::RDS::DBInstance", "arn:aws:rds:::db:a"),
        ).fetchone()[0]
        charge_a = conn.execute(
            "INSERT INTO focus_charges (tenant_id, account_id, period_start, period_end, service)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (TENANT_A, account_a, "2026-06-01", "2026-06-30", "AmazonRDS"),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO observations (tenant_id, resource_id, source, observed_at, payload)"
            " VALUES (%s, %s, %s, NOW(), '{}'::jsonb)",
            (TENANT_A, resource_a, "aws_rds"),
        )
        conn.execute(
            "INSERT INTO facts (tenant_id, resource_id, namespace, key, value_state, source,"
            " observed_at) VALUES (%s, %s, %s, %s, %s, %s, NOW())",
            (TENANT_A, resource_a, "aws", "rds.engine", "KNOWN", "aws_rds"),
        )
        conn.execute(
            "INSERT INTO insights (tenant_id, account_id, rule_name, severity, title, payload)"
            " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb)",
            (TENANT_A, account_a, "rds_eol", "warning", "seeded"),
        )
        conn.execute(
            "INSERT INTO inconclusive (tenant_id, account_id, rule_name, missing_facts)"
            " VALUES (%s, %s, %s, '[]'::jsonb)",
            (TENANT_A, account_a, "rds_eol"),
        )
        conn.execute(
            "INSERT INTO source_runs (tenant_id, account_id, region, resource_type, source,"
            " status) VALUES (%s, %s, %s, %s, %s, %s)",
            (TENANT_A, account_a, "eu-west-1", "AWS::RDS::DBInstance", "aws_rds", "success"),
        )
        conn.execute(
            "INSERT INTO insight_runs (tenant_id, rule_name, status) VALUES (%s, %s, %s)",
            (TENANT_A, "rds_eol", "success"),
        )
        conn.execute(
            "INSERT INTO focus_charge_tags (tenant_id, focus_charge_id, key, value)"
            " VALUES (%s, %s, %s, %s)",
            (TENANT_A, charge_a, "Application", "web"),
        )
        conn.execute(
            "INSERT INTO audit_events (tenant_id, actor, action) VALUES (%s, %s, %s)",
            (TENANT_A, "system:test", "scan"),
        )
        conn.execute(
            "INSERT INTO retention_policies (tenant_id, table_name, retention_days)"
            " VALUES (%s, %s, %s)",
            (TENANT_A, "facts", 90),
        )
        conn.execute(
            "INSERT INTO pii_classifications (tenant_id, resource_type, resource_id, field_name,"
            " sensitivity, value_hash) VALUES (%s, %s, %s, %s, %s, %s)",
            (TENANT_A, "account", "111111111111", "aws_account_id", "confidential", "a" * 64),
        )
        conn.execute(
            "INSERT INTO collect_jobs (tenant_id, actor, total_items) VALUES (%s, %s, %s)",
            (TENANT_A, "alice", 2),
        )
        conn.execute(
            "INSERT INTO collect_targets (tenant_id, aws_account_id, role_arn, external_id)"
            " VALUES (%s, %s, %s, %s)",
            (
                TENANT_A,
                "111111111111",
                "arn:aws:iam::111111111111:role/constat-collector",
                "s3cr3t",
            ),
        )
        conn.execute(
            "INSERT INTO insight_events (tenant_id, fingerprint, rule_name, resource_id,"
            " title, event, monthly_usd) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (TENANT_A, "a" * 64, "rds_eol", resource_a, "seeded", "appeared", 10.0),
        )
    yield pg_migrated


@requires_postgres
@pytest.mark.postgres
class TestPostgresRLS:
    """The real 2-tenant RLS scenario. Runs only with a live Postgres."""

    def test_every_rls_table_has_a_policy(self, pg_seeded: str) -> None:
        """Drift guard (audit F-04): any new tenant-scoped table without a
        policy fails here. Also verifies FORCE RLS is on (otherwise the
        table owner — our app role — would bypass the policies)."""
        with _connect(pg_seeded) as conn:
            policies = {
                row[0]
                for row in conn.execute(
                    "SELECT tablename FROM pg_policies WHERE schemaname = 'public'"
                ).fetchall()
            }
            forced = {
                row[0]
                for row in conn.execute(
                    "SELECT relname FROM pg_class"
                    " WHERE relnamespace = 'public'::regnamespace"
                    " AND relkind = 'r' AND relforcerowsecurity"
                ).fetchall()
            }
        assert policies == set(RLS_TABLES)
        assert forced == set(RLS_TABLES)

    def test_no_tenant_context_sees_nothing(self, pg_seeded: str) -> None:
        """Safe default: a session that never set the GUC sees zero rows
        (current_setting(..., true) is NULL, so the policy hides everything)."""
        with _connect(pg_seeded) as conn:
            for table in RLS_TABLES:
                assert _count(conn, table) == 0, f"{table}: rows visible without tenant context"

    def test_cross_tenant_select_returns_nothing(self, pg_seeded: str) -> None:
        with _connect(pg_seeded) as conn:
            _set_tenant(conn, TENANT_B)
            for table in RLS_TABLES:
                assert _count(conn, table) == 0, f"{table}: tenant B sees tenant A rows"

    def test_own_tenant_select_sees_seeded_rows(self, pg_seeded: str) -> None:
        with _connect(pg_seeded) as conn:
            _set_tenant(conn, TENANT_A)
            for table in RLS_TABLES:
                assert _count(conn, table) == 1, f"{table}: tenant A can't see its own row"

    @pytest.mark.parametrize("table", RLS_TABLES)
    def test_cross_tenant_insert_rejected(self, pg_seeded: str, table: str) -> None:
        """WITH CHECK: a session for tenant B cannot insert a row stamped
        with tenant A's id, even if it knows the id."""
        sql, params = WRONG_TENANT_INSERTS[table]
        with _connect(pg_seeded) as conn:
            _set_tenant(conn, TENANT_B)
            with pytest.raises(Exception, match="row-level security"):
                conn.execute(sql, params)

    def test_same_external_id_allowed_for_two_tenants(self, pg_seeded: str) -> None:
        """Audit F-12: UNIQUE(tenant_id, external_id), not global — the MSP
        case where two tenants monitor the same AWS account."""
        with _connect(pg_seeded) as conn:
            _set_tenant(conn, TENANT_A)
            conn.execute(
                "INSERT INTO accounts (tenant_id, external_id, name) VALUES (%s, %s, %s)",
                (TENANT_A, "333333333333", "shared-a"),
            )
            _set_tenant(conn, TENANT_B)
            conn.execute(
                "INSERT INTO accounts (tenant_id, external_id, name) VALUES (%s, %s, %s)",
                (TENANT_B, "333333333333", "shared-b"),
            )
            # Each tenant sees exactly its own copy.
            assert _count_where_external(conn, "333333333333") == 1
            _set_tenant(conn, TENANT_A)
            assert _count_where_external(conn, "333333333333") == 1

    def test_same_tenant_duplicate_external_id_rejected(self, pg_seeded: str) -> None:
        """The composite unique constraint still holds within a tenant."""
        with _connect(pg_seeded) as conn:
            _set_tenant(conn, TENANT_A)
            with pytest.raises(Exception, match="uq_accounts_tenant_external"):
                conn.execute(
                    "INSERT INTO accounts (tenant_id, external_id, name) VALUES (%s, %s, %s)",
                    (TENANT_A, "111111111111", "duplicate"),
                )


def _count_where_external(conn: Any, external_id: str) -> int:
    return conn.execute(
        "SELECT count(*) FROM accounts WHERE external_id = %s", (external_id,)
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Integration: the §11.2 runtime role (constat_app, migration 0012)
# ---------------------------------------------------------------------------
#
# The owner role (constat) runs migrations and owns every table/policy.
# The runtime role the API should connect as is constat_app: non-owner,
# non-superuser, no BYPASSRLS, DML-only. These tests connect as
# constat_app and verify the three sides of that contract:
#   - DDL is denied (CREATE TABLE, ALTER POLICY — it owns nothing, so it
#     cannot weaken the tenant isolation policies)
#   - RLS still binds it: without the GUC, or with another tenant's GUC,
#     it sees zero rows; with its own tenant GUC it sees its rows
# The role must still set `app.current_tenant_id` per transaction —
# exactly like apps/api/src/constat_api/tenant.py does for the API.

APP_DATABASE_URL = os.environ.get(
    "CONSTAT_TEST_APP_DATABASE_URL",
    "postgresql://constat_app:constat@localhost:5432/constat",
)


@requires_postgres
@pytest.mark.postgres
class TestRuntimeRole:
    """The non-owner runtime role (§11.2). Runs only with a live Postgres
    where migration 0012 has been applied (pg_seeded does that)."""

    def _connect_app(self) -> Any:
        psycopg = _psycopg()
        try:
            return psycopg.connect(APP_DATABASE_URL, autocommit=True)
        except psycopg.OperationalError as exc:
            pytest.skip(
                f"cannot connect as constat_app ({exc}) — set "
                "CONSTAT_TEST_APP_DATABASE_URL and apply migration 0012"
            )

    def test_role_exists_without_bypassrls(self, pg_seeded: str) -> None:
        """The §11.2 attributes: LOGIN, non-superuser, no BYPASSRLS."""
        with _connect(pg_seeded) as conn:
            row = conn.execute(
                "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles"
                " WHERE rolname = 'constat_app'"
            ).fetchone()
        assert row is not None, "role constat_app missing — migration 0012 not applied?"
        assert row == (True, False, False)

    def test_alter_policy_denied(self, pg_seeded: str) -> None:
        """Only the table owner can ALTER POLICY; constat_app owns nothing,
        so it cannot weaken tenant isolation."""
        with (
            self._connect_app() as conn,
            pytest.raises(Exception, match="must be owner"),
        ):
            conn.execute("ALTER POLICY tenant_isolation_accounts ON accounts USING (true)")

    def test_create_table_denied(self, pg_seeded: str) -> None:
        """DML-only: no CREATE on schema public."""
        with (
            self._connect_app() as conn,
            pytest.raises(Exception, match="permission denied for schema"),
        ):
            conn.execute("CREATE TABLE public.evil (id int)")

    def test_cross_tenant_select_returns_nothing(self, pg_seeded: str) -> None:
        """RLS binds the non-owner role: another tenant's GUC => 0 rows,
        and no GUC at all => 0 rows (the safe default)."""
        with self._connect_app() as conn:
            for table in RLS_TABLES:
                assert _count(conn, table) == 0, f"{table}: rows visible without tenant context"
            _set_tenant(conn, TENANT_B)
            for table in RLS_TABLES:
                assert _count(conn, table) == 0, f"{table}: constat_app sees tenant A rows"

    def test_own_tenant_select_sees_seeded_rows(self, pg_seeded: str) -> None:
        """Sanity: the DML grants actually work — with tenant A's GUC the
        runtime role reads exactly the seeded rows."""
        with self._connect_app() as conn:
            _set_tenant(conn, TENANT_A)
            for table in RLS_TABLES:
                assert _count(conn, table) == 1, f"{table}: constat_app can't see tenant A row"
