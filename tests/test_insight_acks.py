"""Tests for the operator-acknowledgment endpoint (P1 item 1).

UX/ops P1: the operator needs a way to triage the daily "12
critical" list. This test pins:
- The PATCH endpoint accepts the 4 valid ack_status values.
- The PATCH endpoint rejects invalid values (400).
- The PATCH endpoint returns 404 for unknown insight_id.
- The PATCH sets ack_at server-side (client cannot override).
- The PATCH preserves ack_by when not provided (None unless given).
- The GET /insights filter by ack_status works, including the
  virtual "open" (NULL) value.
- The ORM↔Pydantic round-trip includes the 3 new fields.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from constat_api.orm import InsightORM
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

VALID_ACK_STATUSES = [
    "acknowledged",
    "in_progress",
    "resolved",
    "dismissed",
]


def _make_insight(session: Session, *, severity: str = "critical") -> InsightORM:
    # insights must have at least one of (resource_id, account_id)
    # set — the CHECK constraint insight_scope_present enforces it.
    orm = InsightORM(
        id=uuid4(),
        rule_name="rds_eol",
        account_id=uuid4(),  # account-scoped (no resource_id)
        severity=severity,
        title="RDS PG 11 in Extended Support",
        payload={"estimated_monthly_usd": 584.0},
    )
    session.add(orm)
    session.flush()
    return orm


# ----------------------------------------------------------------------------
# PATCH /insights/{id}
# ----------------------------------------------------------------------------


def test_patch_sets_acknowledged(client: TestClient, session: Session) -> None:
    insight = _make_insight(session)
    response = client.patch(
        f"/insights/{insight.id}",
        json={"ack_status": "acknowledged", "ack_by": "ops@prospect.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ack_status"] == "acknowledged"
    assert body["ack_by"] == "ops@prospect.com"
    assert body["ack_at"] is not None  # server-set


@pytest.mark.parametrize("ack_status", VALID_ACK_STATUSES)
def test_patch_accepts_all_valid_statuses(
    client: TestClient, session: Session, ack_status: str
) -> None:
    insight = _make_insight(session)
    response = client.patch(
        f"/insights/{insight.id}",
        json={"ack_status": ack_status, "ack_by": "qa"},
    )
    assert response.status_code == 200
    assert response.json()["ack_status"] == ack_status


def test_patch_rejects_invalid_status(client: TestClient, session: Session) -> None:
    insight = _make_insight(session)
    response = client.patch(
        f"/insights/{insight.id}", json={"ack_status": "nonsense"}
    )
    assert response.status_code == 400
    assert "invalid ack_status" in response.json()["detail"]


def test_patch_returns_404_for_unknown_id(client: TestClient) -> None:
    response = client.patch(
        f"/insights/{uuid4()}", json={"ack_status": "acknowledged"}
    )
    assert response.status_code == 404


def test_patch_server_sets_ack_at(client: TestClient, session: Session) -> None:
    """The client cannot override ack_at — it's server-set."""
    insight = _make_insight(session)
    # Attempt to send ack_at in the body — Pydantic should reject (model
    # has only ack_status and ack_by).
    response = client.patch(
        f"/insights/{insight.id}",
        json={"ack_status": "resolved", "ack_at": "2020-01-01T00:00:00Z"},
    )
    # Pydantic strips unknown fields by default, so the call succeeds
    # and the server-side ack_at is what gets stored.
    assert response.status_code == 200
    body = response.json()
    assert body["ack_at"] is not None
    assert not body["ack_at"].startswith("2020-01-01")


def test_patch_ack_by_optional(client: TestClient, session: Session) -> None:
    """ack_by is optional. ack_status is required."""
    insight = _make_insight(session)
    response = client.patch(
        f"/insights/{insight.id}", json={"ack_status": "dismissed"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ack_status"] == "dismissed"
    assert body["ack_by"] is None


def test_patch_required_field_missing(client: TestClient, session: Session) -> None:
    insight = _make_insight(session)
    response = client.patch(f"/insights/{insight.id}", json={})
    assert response.status_code == 422  # Pydantic validation


def test_patch_last_write_wins(client: TestClient, session: Session) -> None:
    """A second PATCH overwrites the first. No audit history in V1."""
    insight = _make_insight(session)
    client.patch(
        f"/insights/{insight.id}", json={"ack_status": "acknowledged", "ack_by": "alice"}
    )
    response = client.patch(
        f"/insights/{insight.id}", json={"ack_status": "resolved", "ack_by": "bob"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ack_status"] == "resolved"
    assert body["ack_by"] == "bob"


# ----------------------------------------------------------------------------
# GET /insights with ack_status filter
# ----------------------------------------------------------------------------


def test_get_insights_filter_by_open(client: TestClient, session: Session) -> None:
    """The 'open' filter returns only insights with ack_status IS NULL."""
    open_insight = _make_insight(session)
    acked = _make_insight(session)
    client.patch(f"/insights/{acked.id}", json={"ack_status": "resolved"})

    response = client.get("/insights", params={"ack_status": "open"})
    assert response.status_code == 200
    ids = [i["id"] for i in response.json()]
    assert str(open_insight.id) in ids
    assert str(acked.id) not in ids


def test_get_insights_filter_by_status(client: TestClient, session: Session) -> None:
    in_progress = _make_insight(session)
    resolved = _make_insight(session)
    client.patch(f"/insights/{in_progress.id}", json={"ack_status": "in_progress"})
    client.patch(f"/insights/{resolved.id}", json={"ack_status": "resolved"})

    response = client.get("/insights", params={"ack_status": "in_progress"})
    ids = [i["id"] for i in response.json()]
    assert str(in_progress.id) in ids
    assert str(resolved.id) not in ids


def test_get_insights_filter_by_invalid_status(client: TestClient) -> None:
    """Router validates the filter value (defense in depth — repo also checks)."""
    response = client.get("/insights", params={"ack_status": "nonsense"})
    assert response.status_code == 400
    assert "invalid ack_status" in response.json()["detail"]


def test_get_insights_includes_ack_fields(client: TestClient, session: Session) -> None:
    """The 3 ack fields are present in the GET response, both NULL and set."""
    orm = _make_insight(session)
    response = client.get(f"/insights/{orm.id}")
    assert response.status_code == 200
    body = response.json()
    assert "ack_status" in body
    assert "ack_at" in body
    assert "ack_by" in body
    assert body["ack_status"] is None
    assert body["ack_at"] is None
    assert body["ack_by"] is None
