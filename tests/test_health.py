"""Tests for the V1 /health endpoint (UX/ops P3 item 12).

Covers:
- 200 when everything is healthy (DB up, FOCUS data fresh, no stuck runs)
- 200 on day 1 with no FOCUS data
- 503 when FOCUS data is older than the freshness threshold
- 503 when there are stuck source_runs
- 200 again after the issues are fixed
- LB sees 503, ops sees the body
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from constat_api.orm import FocusChargeORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.settings import DEFAULT_TENANT_ID
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


def test_health_returns_200_when_all_checks_pass(client: TestClient, session: Session) -> None:
    """No FOCUS data, no stuck runs -> 200 'ok' (day 1 is fine)."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["db"]["status"] == "ok"
    assert body["checks"]["focus_freshness"]["status"] == "ok"
    assert body["checks"]["stuck_runs"]["status"] == "ok"
    # No FOCUS rows yet
    assert body["checks"]["focus_freshness"]["ingested_count"] == 0
    assert body["checks"]["stuck_runs"]["stuck_count"] == 0


def test_health_returns_200_when_focus_data_is_fresh(client: TestClient, session: Session) -> None:
    """A focus_charge ingested 1 hour ago is fresh under the 24h default."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    now = datetime.now(tz=UTC)
    session.add(
        FocusChargeORM(  # type: ignore[call-arg]
            id=1,
            tenant_id=DEFAULT_TENANT_ID,
            account_id=acc.id,
            period_start=now - timedelta(days=30),
            period_end=now - timedelta(days=1),
            service="AmazonRDS",
            billed_cost=100,
            amortized_cost=100,
            ingested_at=now - timedelta(hours=1),
        )
    )
    session.commit()

    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["focus_freshness"]["status"] == "ok"
    assert body["checks"]["focus_freshness"]["ingested_count"] == 1
    assert body["checks"]["focus_freshness"]["age_seconds"] < 7200  # < 2h


def test_health_returns_503_when_focus_data_is_stale(client: TestClient, session: Session) -> None:
    """A focus_charge ingested 30 days ago is stale under the 24h default."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    now = datetime.now(tz=UTC)
    session.add(
        FocusChargeORM(  # type: ignore[call-arg]
            id=1,
            tenant_id=DEFAULT_TENANT_ID,
            account_id=acc.id,
            period_start=now - timedelta(days=60),
            period_end=now - timedelta(days=30),
            service="AmazonRDS",
            billed_cost=100,
            amortized_cost=100,
            ingested_at=now - timedelta(days=30),
        )
    )
    session.commit()

    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["focus_freshness"]["status"] == "stale"
    assert body["checks"]["focus_freshness"]["ingested_count"] == 1
    # Body should carry the threshold so ops can see what the LB is checking against
    assert body["checks"]["focus_freshness"]["stale_threshold_hours"] == 24.0


def test_health_returns_503_when_stuck_runs_exist(client: TestClient, session: Session) -> None:
    """A 'running' source_run older than the stuck threshold -> 503."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    # Backdate started_at to 3 hours ago
    run.started_at = datetime.now(tz=UTC) - timedelta(hours=3)
    session.commit()

    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["stuck_runs"]["status"] == "error"
    assert body["checks"]["stuck_runs"]["stuck_count"] == 1
    # The detail field tells ops what to do
    assert "cleanup-stuck-runs" in body["checks"]["stuck_runs"]["detail"]


def test_health_returns_200_after_stuck_runs_cleaned(client: TestClient, session: Session) -> None:
    """Cleanup the stuck run via the existing endpoint, /health recovers."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    run.started_at = datetime.now(tz=UTC) - timedelta(hours=3)
    session.commit()

    # Sanity: 503 now
    assert client.get("/health").status_code == 503

    # Cleanup
    cleaned = source_runs_repo.cleanup_stuck_runs(session)
    assert cleaned == 1

    # Now 200
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["stuck_runs"]["status"] == "ok"


def test_health_query_params_override_thresholds(client: TestClient, session: Session) -> None:
    """stale_after_hours and stuck_run_hours are tunable per request."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    now = datetime.now(tz=UTC)
    session.add(
        FocusChargeORM(  # type: ignore[call-arg]
            id=1,
            tenant_id=DEFAULT_TENANT_ID,
            account_id=acc.id,
            period_start=now - timedelta(days=2),
            period_end=now - timedelta(days=1),
            service="AmazonRDS",
            billed_cost=100,
            amortized_cost=100,
            ingested_at=now - timedelta(hours=12),
        )
    )
    session.commit()

    # Default 24h: 12h-old data is fresh
    assert client.get("/health").status_code == 200

    # Override to 6h: 12h-old data is now stale
    response = client.get("/health?stale_after_hours=6")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["focus_freshness"]["stale_threshold_hours"] == 6.0


def test_health_does_not_require_auth(client: TestClient) -> None:
    """The /health endpoint must remain open (LB / k8s liveness probes
    do not carry API keys)."""
    response = client.get("/health")
    assert response.status_code in (200, 503)  # never 401
    # Should never be a 401 (we don't require X-API-Key here)
    assert "X-API-Key" not in response.headers.get("WWW-Authenticate", "")


def test_health_body_includes_checked_at_and_tenant(
    client: TestClient,
) -> None:
    """The body always carries 'checked_at' (ISO 8601) and the tenant id
    for log correlation."""
    response = client.get("/health")
    body = response.json()
    assert "checked_at" in body
    # checked_at is parseable
    datetime.fromisoformat(body["checked_at"])
    assert body["tenant"] == str(DEFAULT_TENANT_ID)
