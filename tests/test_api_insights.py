"""API endpoint tests: /health and /insights."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from constat_api.auth import _get_settings
from constat_api.main import app
from constat_api.orm import AccountORM, InsightORM
from constat_api.settings import Settings
from fastapi.testclient import TestClient


@pytest.fixture
def manual_insights(client: TestClient) -> Iterator[None]:
    """Enable CONSTAT_ENABLE_MANUAL_INSIGHTS for one test (F-10).

    POST /insights is gated behind the flag, default off. Tests that
    exercise the manual insert path opt in via a settings dep override,
    same pattern as the auth tests.
    """

    def _override_settings() -> Settings:
        return Settings(enable_manual_insights=True)

    app.dependency_overrides[_get_settings] = _override_settings
    yield
    app.dependency_overrides.pop(_get_settings, None)


def test_health_pings_db(client: TestClient, session) -> None:
    """The V1 /health now returns a structured body. The V1 test
    asserted just the `status` key; we keep that minimal check here
    (the full coverage lives in tests/test_health.py)."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "checks" in body


def _make_account(session, external_id: str = "111111111111") -> AccountORM:
    acc = AccountORM(external_id=external_id, name="test")
    session.add(acc)
    session.commit()
    return acc


def test_list_insights_empty(client: TestClient) -> None:
    response = client.get("/insights")
    assert response.status_code == 200
    assert response.json() == []


def test_create_insight_forbidden_when_flag_disabled(client: TestClient, session) -> None:
    """F-10: with the default settings, POST /insights is refused (403)."""
    acc = _make_account(session)
    payload = {
        "rule_name": "rds_eol",
        "account_id": str(acc.id),
        "severity": "warning",
        "title": "forged insight",
        "payload": {},
    }
    response = client.post("/insights", json=payload)
    assert response.status_code == 403


def test_create_and_get_insight(client: TestClient, session, manual_insights: None) -> None:
    acc = _make_account(session)
    payload = {
        "rule_name": "rds_eol",
        "account_id": str(acc.id),
        "severity": "warning",
        "title": "RDS PG 14 reaches EOL in 89 days",
        "payload": {"days_to_eol": 89, "ext_support_monthly_usd_estimate": 584.0},
    }
    response = client.post("/insights", json=payload)
    assert response.status_code == 201, response.text

    created = response.json()
    assert created["rule_name"] == "rds_eol"
    assert created["severity"] == "warning"
    # F-10: manual insights are visibly stamped as such.
    assert created["payload"]["source"] == "manual"
    assert created["payload"]["days_to_eol"] == 89
    insight_id = created["id"]

    # GET by id
    response = client.get(f"/insights/{insight_id}")
    assert response.status_code == 200
    assert response.json()["title"] == payload["title"]


def test_get_insight_404(client: TestClient) -> None:
    response = client.get(f"/insights/{uuid4()}")
    assert response.status_code == 404


def test_list_insights_filters_by_rule_and_severity(client: TestClient, session) -> None:
    acc = _make_account(session)
    # Insert 3 insights with different rule/severity
    session.add(
        InsightORM(
            id=uuid4(),
            rule_name="rds_eol",
            account_id=acc.id,
            severity="warning",
            title="t1",
            payload={},
            computed_at=datetime(2026, 7, 18, tzinfo=UTC),
        )
    )
    session.add(
        InsightORM(
            id=uuid4(),
            rule_name="rds_eol",
            account_id=acc.id,
            severity="critical",
            title="t2",
            payload={},
            computed_at=datetime(2026, 7, 18, 1, tzinfo=UTC),
        )
    )
    session.add(
        InsightORM(
            id=uuid4(),
            rule_name="chargeback",
            account_id=acc.id,
            severity="info",
            title="t3",
            payload={},
            computed_at=datetime(2026, 7, 18, 2, tzinfo=UTC),
        )
    )
    session.commit()

    # Filter by rule
    response = client.get("/insights", params={"rule_name": "rds_eol"})
    assert response.status_code == 200
    titles = [i["title"] for i in response.json()]
    assert sorted(titles) == ["t1", "t2"]

    # Filter by severity
    response = client.get("/insights", params={"severity": "critical"})
    assert response.status_code == 200
    assert [i["title"] for i in response.json()] == ["t2"]


def test_create_insight_uses_canonical_severity(client: TestClient, manual_insights: None) -> None:
    """Severity must be one of {info, warning, critical}; rejected otherwise."""
    bad = {
        "rule_name": "rds_eol",
        "severity": "fatal",  # invalid
        "title": "x",
        "payload": {},
    }
    response = client.post("/insights", json=bad)
    assert response.status_code == 422
