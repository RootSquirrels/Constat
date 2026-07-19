"""Tests for read attribution — "who saw my data" (CISO requirement 3.3).

Every sensitive read (insights list/detail/export, inconclusives,
accounts, status) writes one `api.read` row to audit_events with the
principal's name as actor ("anonymous" when auth is open). Metadata is
strictly non-PII: route template, filters present as booleans, row count.
"""

from __future__ import annotations

import pytest
from constat_api.auth import _get_settings
from constat_api.main import app
from constat_api.orm import AuditEventORM
from constat_api.settings import ApiKeyEntry, Settings
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

ALICE_KEY = "alice-operator-key"
BOB_KEY = "bob-reader-key"


@pytest.fixture
def named_principals(client: TestClient):
    cfg = Settings(
        api_keys=(
            ApiKeyEntry(name="alice", role="operator", key=ALICE_KEY),
            ApiKeyEntry(name="bob", role="reader", key=BOB_KEY),
        )
    )
    app.dependency_overrides[_get_settings] = lambda: cfg
    yield cfg
    app.dependency_overrides.pop(_get_settings, None)


def _read_events(session: Session) -> list[AuditEventORM]:
    return (
        session.query(AuditEventORM)
        .filter(AuditEventORM.action == "api.read")
        .order_by(AuditEventORM.id)
        .all()
    )


def test_insights_list_read_is_attributed(
    named_principals: Settings, client: TestClient, session: Session
) -> None:
    response = client.get("/insights", headers={"X-API-Key": ALICE_KEY})
    assert response.status_code == 200

    events = _read_events(session)
    assert len(events) == 1
    event = events[0]
    assert event.actor == "machine:alice"
    assert event.action == "api.read"
    assert event.target_type == "insights"
    # Metadata: route template + row count + filter booleans, no PII.
    assert event.metadata_json["route"] == "/insights"
    assert event.metadata_json["row_count"] == 0
    assert event.metadata_json["filter_rule_name"] is False
    assert "account_id" not in event.metadata_json


def test_insights_export_read_is_attributed(
    named_principals: Settings, client: TestClient, session: Session
) -> None:
    response = client.get("/insights/export.csv", headers={"X-API-Key": BOB_KEY})
    assert response.status_code == 200

    events = _read_events(session)
    assert len(events) == 1
    assert events[0].actor == "machine:bob"  # readers may read; their reads are attributed
    assert events[0].metadata_json["route"] == "/insights/export.csv"


def test_open_auth_read_records_anonymous(client: TestClient, session: Session) -> None:
    """Dev mode (no keys): reads are still logged, actor = 'anonymous'."""
    response = client.get("/insights")
    assert response.status_code == 200

    events = _read_events(session)
    assert len(events) == 1
    assert events[0].actor == "machine:anonymous"


def test_failed_read_is_not_attributed(
    named_principals: Settings, client: TestClient, session: Session
) -> None:
    """A 401 (unknown key) never reaches the endpoint -> no audit row."""
    response = client.get("/insights", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401
    assert _read_events(session) == []


def test_audit_events_endpoint_answers_who_saw_what(
    named_principals: Settings, client: TestClient
) -> None:
    """The demonstrable answer: after alice reads /insights, the operator
    can filter /compliance/audit-events by actor=alice and see the read."""
    client.get("/insights", headers={"X-API-Key": ALICE_KEY})
    response = client.get(
        "/compliance/audit-events",
        params={"actor": "machine:alice", "action": "api.read"},
        headers={"X-API-Key": ALICE_KEY},
    )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["actor"] == "machine:alice"
    assert rows[0]["metadata"]["route"] == "/insights"
