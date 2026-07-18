"""API endpoint tests: /health and /insights."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from constat_api.orm import AccountORM, InsightORM
from fastapi.testclient import TestClient


def test_health_pings_db(client: TestClient, session) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def _make_account(session, external_id: str = "111111111111") -> AccountORM:
    acc = AccountORM(external_id=external_id, name="test")
    session.add(acc)
    session.commit()
    return acc


def test_list_insights_empty(client: TestClient) -> None:
    response = client.get("/insights")
    assert response.status_code == 200
    assert response.json() == []


def test_create_and_get_insight(client: TestClient, session) -> None:
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


def test_create_insight_uses_canonical_severity(client: TestClient) -> None:
    """Severity must be one of {info, warning, critical}; rejected otherwise."""
    bad = {
        "rule_name": "rds_eol",
        "severity": "fatal",  # invalid
        "title": "x",
        "payload": {},
    }
    response = client.post("/insights", json=bad)
    assert response.status_code == 422
