"""Tests for the insight runner (resource -> facts -> evaluate -> write)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from constat_api.insights.runner import (
    DEFAULT_SOURCE,
    _is_scope_proven,
    run_rds_eol,
)
from constat_api.orm import (
    InconclusiveORM,
    InsightORM,
    InsightRunORM,
    ResourceORM,
)
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.settings import DEFAULT_TENANT_ID
from constat_core.models import Fact, ValueState
from sqlalchemy.orm import Session


def _make_db(arn: str = "arn:aws:rds:eu-west-1:111111111111:db:pg14") -> dict[str, Any]:
    return {
        "DBInstanceArn": arn,
        "DBInstanceIdentifier": "pg14",
        "Engine": "postgres",
        "EngineVersion": "14.7",
        "DBInstanceClass": "db.m5.xlarge",
        "DBInstanceStatus": "available",
        "AllocatedStorage": 100,
        "InstanceCreateTime": datetime(2024, 1, 1, tzinfo=UTC),
        "MultiAZ": True,
        "StorageEncrypted": True,
        "DBSubnetGroup": {"DBSubnetGroupName": "default"},
        "Endpoint": {"Address": "pg14.xxxx.eu-west-1.rds.amazonaws.com"},
    }


def _bootstrap_pg14(session: Session) -> ResourceORM:
    """A complete bootstrap: account + resource + successful source_run + PG14 facts."""
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

    # A successful source_run proves the scope was scanned.
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source=DEFAULT_SOURCE,
    )
    source_runs_repo.finish_run(session, run, status="success", resources_found=1)
    session.commit()

    # PG14 facts (4 vCPU). The current-state design + the fix-4.1 changes use upsert.
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
                source=DEFAULT_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
            Fact(
                resource_id=resource.id,
                account_id=str(acc.id),
                namespace="aws.rds",
                key="engine_version",
                value="14.7",
                value_state=ValueState.KNOWN,
                source=DEFAULT_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
            Fact(
                resource_id=resource.id,
                account_id=str(acc.id),
                namespace="aws.rds",
                key="instance_class",
                value="db.m5.xlarge",
                value_state=ValueState.KNOWN,
                source=DEFAULT_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
            Fact(
                resource_id=resource.id,
                account_id=str(acc.id),
                namespace="aws.rds",
                key="vcpu",
                value=4,
                value_state=ValueState.KNOWN,
                source=DEFAULT_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
        ],
        source_run_id=run.id,
    )
    session.commit()
    return resource


# ---- Scope check -----------------------------------------------------------


def test_is_scope_proven_true_with_successful_run(session: Session) -> None:
    resource = _bootstrap_pg14(session)
    assert _is_scope_proven(session, resource) is True


def test_is_scope_proven_false_with_no_run(session: Session) -> None:
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:no-run",
    )
    session.add(resource)
    session.commit()
    assert _is_scope_proven(session, resource) is False


def test_is_scope_proven_false_with_failed_run(session: Session) -> None:
    """A failed run does NOT prove the scope; we must emit INCONCLUSIVE."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:failed",
    )
    session.add(resource)
    session.commit()

    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source=DEFAULT_SOURCE,
    )
    source_runs_repo.finish_run(
        session, run, status="failed", resources_found=0, error="AccessDenied"
    )
    session.commit()
    assert _is_scope_proven(session, resource) is False


# ---- Runner happy path -----------------------------------------------------


def test_runner_emits_insight_for_pg14_in_window(session: Session) -> None:
    """End-to-end: bootstrap PG14 with EOL in 90 days, run, expect 1 insight."""
    _bootstrap_pg14(session)
    # PG14 EOL = 2027-02-28. From 2026-12-01, 89 days.
    result = run_rds_eol(session, today=date(2026, 12, 1))
    assert result.resources_scanned == 1
    assert result.insights_emitted == 1
    assert result.inconclusive_emitted == 0
    assert result.errors == []

    rows = session.query(InsightORM).all()
    assert len(rows) == 1
    assert rows[0].rule_name == "rds_eol"
    assert rows[0].severity == "warning"


def test_runner_emits_no_insight_for_pg11_within_year_3(session: Session) -> None:
    """PG11 past EOL -> 1 critical insight, year 3 rate."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:pg11",
    )
    session.add(resource)
    session.commit()
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source=DEFAULT_SOURCE,
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
                source=DEFAULT_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
            Fact(
                resource_id=resource.id,
                account_id=str(acc.id),
                namespace="aws.rds",
                key="engine_version",
                value="11.22",
                value_state=ValueState.KNOWN,
                source=DEFAULT_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
            Fact(
                resource_id=resource.id,
                account_id=str(acc.id),
                namespace="aws.rds",
                key="instance_class",
                value="db.m5.xlarge",
                value_state=ValueState.KNOWN,
                source=DEFAULT_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
            Fact(
                resource_id=resource.id,
                account_id=str(acc.id),
                namespace="aws.rds",
                key="vcpu",
                value=2,
                value_state=ValueState.KNOWN,
                source=DEFAULT_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
        ],
        source_run_id=run.id,
    )
    session.commit()

    result = run_rds_eol(session, today=date(2026, 7, 18))
    assert result.insights_emitted == 1
    insight = session.query(InsightORM).one()
    assert insight.severity == "critical"
    assert insight.payload["pricing_tier"] == "year_3_plus"


# ---- Runner INCONCLUSIVE paths --------------------------------------------


def test_runner_emits_inconclusive_when_scope_not_proven(session: Session) -> None:
    """No successful source_run -> INCONCLUSIVE, no insight."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:no-scope",
    )
    session.add(resource)
    session.commit()
    # No source_run.

    result = run_rds_eol(session, today=date(2026, 7, 18))
    assert result.insights_emitted == 0
    assert result.inconclusive_emitted == 1

    inc = session.query(InconclusiveORM).one()
    assert "scope_not_proven" in inc.missing_facts


def test_runner_emits_inconclusive_when_no_facts(session: Session) -> None:
    """Successful run but no facts observed -> INCONCLUSIVE."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:no-facts",
    )
    session.add(resource)
    session.commit()

    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source=DEFAULT_SOURCE,
    )
    source_runs_repo.finish_run(session, run, status="success", resources_found=0)
    session.commit()

    result = run_rds_eol(session, today=date(2026, 7, 18))
    assert result.insights_emitted == 0
    assert result.inconclusive_emitted == 1
    inc = session.query(InconclusiveORM).one()
    assert "<no facts>" in inc.missing_facts


def test_runner_emits_inconclusive_when_vcpu_missing(session: Session) -> None:
    """The UNKNOWN-silently-disappears failure mode is gone:
    vcpu UNKNOWN -> INCONCLUSIVE, not silent skip."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:unknown-vcpu",
    )
    session.add(resource)
    session.commit()
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source=DEFAULT_SOURCE,
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
                source=DEFAULT_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
            Fact(
                resource_id=resource.id,
                account_id=str(acc.id),
                namespace="aws.rds",
                key="engine_version",
                value="14.7",
                value_state=ValueState.KNOWN,
                source=DEFAULT_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
            Fact(
                resource_id=resource.id,
                account_id=str(acc.id),
                namespace="aws.rds",
                key="instance_class",
                value="db.m6g.xlarge",  # Graviton
                value_state=ValueState.KNOWN,
                source=DEFAULT_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
            # vcpu UNKNOWN because the connector didn't see it (or fact wasn't written)
            Fact(
                resource_id=resource.id,
                account_id=str(acc.id),
                namespace="aws.rds",
                key="vcpu",
                value=None,
                value_state=ValueState.UNKNOWN,
                source=DEFAULT_SOURCE,
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
        ],
        source_run_id=run.id,
    )
    session.commit()

    result = run_rds_eol(session, today=date(2026, 7, 18))
    assert result.insights_emitted == 0
    assert result.inconclusive_emitted == 1
    inc = session.query(InconclusiveORM).one()
    assert "aws.rds.vcpu" in inc.missing_facts


# ---- Runner metadata -------------------------------------------------------


def test_runner_records_insight_run_metadata(session: Session) -> None:
    _bootstrap_pg14(session)
    run_rds_eol(session, today=date(2026, 12, 1))

    runs = session.query(InsightRunORM).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.rule_name == "rds_eol"
    assert run.status == "success"
    assert run.resources_scanned == 1
    assert run.insights_emitted == 1
    assert run.finished_at is not None
