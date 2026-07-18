"""Tests for the POST /insights/run HTTP endpoint."""

from __future__ import annotations

from datetime import UTC, datetime

from constat_api.orm import ResourceORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.settings import DEFAULT_TENANT_ID
from constat_core.models import Fact, ValueState
from fastapi.testclient import TestClient


def _bootstrap_pg14(session, *, with_facts: bool = True) -> ResourceORM:
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:aws:rds:eu-west-1:111111111111:db:pg14",
    )
    session.add(resource)
    session.commit()
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    source_runs_repo.finish_run(session, run, status="success", resources_found=1)
    session.commit()

    if with_facts:
        facts_repo.upsert_facts(
            session,
            [
                Fact(
                    resource_id=resource.id,
                    account_id=str(acc.id),
                    namespace="aws.rds",
                    key="engine",
                    value="postgres",
                    value_state=ValueState.KNOWN,
                    source="aws_rds",
                    observed_at=datetime(2026, 7, 18, tzinfo=UTC),
                ),
                Fact(
                    resource_id=resource.id,
                    account_id=str(acc.id),
                    namespace="aws.rds",
                    key="engine_version",
                    value="14.7",
                    value_state=ValueState.KNOWN,
                    source="aws_rds",
                    observed_at=datetime(2026, 7, 18, tzinfo=UTC),
                ),
                Fact(
                    resource_id=resource.id,
                    account_id=str(acc.id),
                    namespace="aws.rds",
                    key="instance_class",
                    value="db.m5.xlarge",
                    value_state=ValueState.KNOWN,
                    source="aws_rds",
                    observed_at=datetime(2026, 7, 18, tzinfo=UTC),
                ),
                Fact(
                    resource_id=resource.id,
                    account_id=str(acc.id),
                    namespace="aws.rds",
                    key="vcpu",
                    value=4,
                    value_state=ValueState.KNOWN,
                    source="aws_rds",
                    observed_at=datetime(2026, 7, 18, tzinfo=UTC),
                ),
            ],
            source_run_id=run.id,
        )
        session.commit()
    return resource


def test_run_endpoint_emits_insight(client: TestClient, session) -> None:
    _bootstrap_pg14(session)
    response = client.post(
        "/insights/run",
        json={"rule": "rds_eol"},
        params={"today": "2026-12-01"},  # PG14 EOL is 2027-02-28
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rule_name"] == "rds_eol"
    assert body["resources_scanned"] == 1
    assert body["insights_emitted"] == 1
    assert body["inconclusive_emitted"] == 0
    assert body["errors"] == []


def test_run_endpoint_with_inconclusive(client: TestClient, session) -> None:
    """Resource with no facts -> INCONCLUSIVE, no insight."""
    _bootstrap_pg14(session, with_facts=False)
    response = client.post(
        "/insights/run",
        json={"rule": "rds_eol"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["insights_emitted"] == 0
    assert body["inconclusive_emitted"] == 1


def test_run_endpoint_rejects_unknown_rule(client: TestClient) -> None:
    response = client.post(
        "/insights/run",
        json={"rule": "chargeback"},
    )
    assert response.status_code == 400
    assert "unknown rule" in response.json()["detail"]


def test_run_endpoint_optional_today(client: TestClient, session) -> None:
    """today is optional; defaults to date.today() inside the runner."""
    _bootstrap_pg14(session)
    response = client.post("/insights/run", json={"rule": "rds_eol"})
    assert response.status_code == 200
    body = response.json()
    # PG14 in 2026-07-18: too far from EOL (591 days) -> no insight
    assert body["insights_emitted"] == 0
