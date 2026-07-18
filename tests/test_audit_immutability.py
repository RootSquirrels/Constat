"""Tests for audit_events immutability (migration 0014, CISO requirement 3.4).

The `audit_events_no_update_delete` and `audit_events_no_truncate` triggers
make "append-only" a database-level guarantee: INSERT works, UPDATE /
DELETE / TRUNCATE raise.

These tests need a live Postgres (CI provides one via a service
container). They reuse the `pg_migrated` fixture from tests.test_rls,
which applies migrations 0001 -> latest to a fresh schema; without
CONSTAT_TEST_DATABASE_URL (and psycopg) they skip cleanly.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_rls import _connect, _set_tenant, pg_migrated, requires_postgres

TENANT = "00000000-0000-0000-0000-00000000000a"

_INSERT = "INSERT INTO audit_events (tenant_id, actor, action) VALUES (%s, %s, %s) RETURNING id"

# pg_migrated is consumed by name as a fixture parameter below; listing it
# in __all__ keeps ruff from flagging the import as unused (F401).
__all__ = ["pg_migrated"]


@requires_postgres
@pytest.mark.postgres
class TestAuditImmutability:
    """The triggers of migration 0014 against a live, migrated Postgres."""

    def _insert(self, conn: Any) -> Any:
        _set_tenant(conn, TENANT)
        return conn.execute(_INSERT, (TENANT, "system:test", "immutability_probe")).fetchone()[0]

    def test_insert_still_works(self, pg_migrated: str) -> None:
        """Append-only means appending must keep working."""
        with _connect(pg_migrated) as conn:
            event_id = self._insert(conn)
            assert event_id is not None
            count = conn.execute(
                "SELECT count(*) FROM audit_events WHERE id = %s", (event_id,)
            ).fetchone()[0]
            assert count == 1

    def test_update_is_denied(self, pg_migrated: str) -> None:
        with _connect(pg_migrated) as conn:
            event_id = self._insert(conn)
            with pytest.raises(Exception, match="append-only"):
                conn.execute(
                    "UPDATE audit_events SET actor = 'tampered' WHERE id = %s", (event_id,)
                )

    def test_delete_is_denied(self, pg_migrated: str) -> None:
        with _connect(pg_migrated) as conn:
            event_id = self._insert(conn)
            with pytest.raises(Exception, match="append-only"):
                conn.execute("DELETE FROM audit_events WHERE id = %s", (event_id,))

    def test_truncate_is_denied(self, pg_migrated: str) -> None:
        """Row triggers don't fire on TRUNCATE — the statement-level
        trigger is what closes that hole, so test it explicitly."""
        with _connect(pg_migrated) as conn:
            self._insert(conn)
            with pytest.raises(Exception, match="append-only"):
                conn.execute("TRUNCATE audit_events")
