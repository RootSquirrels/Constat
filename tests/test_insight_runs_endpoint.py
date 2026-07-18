"""Tests for the /insight-runs history endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from constat_api.insights.runner import run_rds_eol
from constat_api.orm import (
    InsightRunORM,
    ResourceORM,
)
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.settings import DEFAULT_TENANT_ID
from constat_core.models import Fact, ValueState
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


def test_endpoint_returns_empty_when_no_runs(client: TestClient) -> None:
    response = client.get("/insight-runs")
    assert response.status_code == 200
    assert response.json() == []


def test_endpoint_lists_recent_runs_newest_first(client: TestClient, session: Session) -> None:
    """Insert 2 runs manually with different started_at, list returns newest first."""
    older = InsightRunORM(
        id=uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        rule_name="rds_eol",
        status="success",
        started_at=datetime(2026, 7, 17, 10, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 17, 10, 5, tzinfo=UTC),
        resources_scanned=1,
        insights_emitted=1,
    )
    newer = InsightRunORM(
        id=uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        rule_name="chargeback",
        status="success",
        started_at=datetime(2026, 7, 18, 10, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 18, 10, 1, tzinfo=UTC),
        resources_scanned=1,
        insights_emitted=2,
    )
    session.add_all([older, newer])
    session.commit()

    response = client.get("/insight-runs")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert body[0]["id"] == str(newer.id)
    assert body[0]["rule_name"] == "chargeback"
    assert body[1]["id"] == str(older.id)
    assert body[1]["rule_name"] == "rds_eol"


def test_endpoint_filters_by_rule(client: TestClient, session: Session) -> None:
    for rule in ("rds_eol", "chargeback", "rds_eol"):
        session.add(
            InsightRunORM(
                id=uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                rule_name=rule,
                status="success",
            )
        )
    session.commit()

    response = client.get("/insight-runs", params={"rule_name": "rds_eol"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert all(r["rule_name"] == "rds_eol" for r in body)


def test_endpoint_filters_by_status(client: TestClient, session: Session) -> None:
    session.add_all(
        [
            InsightRunORM(
                id=uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                rule_name="rds_eol",
                status="success",
            ),
            InsightRunORM(
                id=uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                rule_name="rds_eol",
                status="failed",
                error="boom",
            ),
        ]
    )
    session.commit()

    response = client.get("/insight-runs", params={"status": "failed"})
    body = response.json()
    assert len(body) == 1
    assert body[0]["status"] == "failed"
    assert body[0]["error"] == "boom"


def test_endpoint_respects_limit(client: TestClient, session: Session) -> None:
    for _ in range(5):
        session.add(
            InsightRunORM(
                id=uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                rule_name="rds_eol",
                status="success",
            )
        )
    session.commit()

    response = client.get("/insight-runs", params={"limit": 2})
    assert len(response.json()) == 2


def test_endpoint_lists_runs_created_by_runner(client: TestClient, session: Session) -> None:
    """End-to-end: run_rds_eol creates a run, /insight-runs lists it."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:pg14",
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

    run_rds_eol(session, today=None)

    response = client.get("/insight-runs")
    body = response.json()
    assert len(body) == 1
    assert body[0]["rule_name"] == "rds_eol"
    assert body[0]["status"] == "success"
    assert body[0]["insights_emitted"] == 0  # PG14 in 2026-07-18 too far
    assert body[0]["finished_at"] is not None
