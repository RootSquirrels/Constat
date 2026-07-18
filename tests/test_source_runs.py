"""Tests for SourceRun lifecycle and scope-completeness tracking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

from botocore.exceptions import ClientError
from constat_api.collectors.aws import TargetAccount, collect_target
from constat_api.orm import SourceRunORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.settings import DEFAULT_TENANT_ID
from sqlalchemy.orm import Session


def _make_db(arn: str = "arn:aws:rds:eu-west-1:111111111111:db:test") -> dict[str, Any]:
    return {
        "DBInstanceArn": arn,
        "DBInstanceIdentifier": "test",
        "Engine": "postgres",
        "EngineVersion": "14.7",
        "DBInstanceClass": "db.m5.xlarge",
        "DBInstanceStatus": "available",
        "AllocatedStorage": 100,
        "InstanceCreateTime": datetime(2024, 1, 1, tzinfo=UTC),
        "MultiAZ": True,
        "StorageEncrypted": True,
        "DBSubnetGroup": {"DBSubnetGroupName": "default"},
        "Endpoint": {"Address": "test.xxxx.eu-west-1.rds.amazonaws.com"},
    }


def _no_assume_role(base_session, target):
    return base_session


def test_start_run_creates_running_record(session: Session) -> None:
    acc = accounts_repo.get_or_create(session, "111111111111")
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    session.commit()

    assert run is not None
    assert run.id is not None
    assert run.tenant_id == DEFAULT_TENANT_ID
    assert run.account_id == acc.id
    assert run.region == "eu-west-1"
    assert run.status == "running"
    assert run.started_at is not None
    assert run.finished_at is None


def test_second_active_run_for_same_scope_returns_none(session: Session) -> None:
    acc = accounts_repo.get_or_create(session, "111111111111")
    run1 = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    session.commit()
    assert run1 is not None

    run2 = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    session.commit()
    assert run2 is None  # partial unique index: only one running at a time


def test_finish_run_marks_success(session: Session) -> None:
    acc = accounts_repo.get_or_create(session, "111111111111")
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    session.commit()
    source_runs_repo.finish_run(session, run, status="success", resources_found=3)
    session.commit()

    assert run.status == "success"
    assert run.resources_found == 3
    assert run.finished_at is not None


def test_finish_run_then_new_run_allowed(session: Session) -> None:
    """After a run completes, a new run for the same scope can start."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    run1 = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    source_runs_repo.finish_run(session, run1, status="success", resources_found=1)
    session.commit()

    run2 = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    session.commit()
    assert run2 is not None
    assert run2.id != run1.id


def test_latest_successful_run_returns_most_recent(session: Session) -> None:
    acc = accounts_repo.get_or_create(session, "111111111111")
    run1 = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    source_runs_repo.finish_run(session, run1, status="success", resources_found=1)
    session.commit()

    run2 = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    source_runs_repo.finish_run(session, run2, status="success", resources_found=5)
    session.commit()

    latest = source_runs_repo.latest_successful_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    assert latest is not None
    assert latest.id == run2.id
    assert latest.resources_found == 5


def test_collector_creates_source_runs(session: Session) -> None:
    """End-to-end: collect_target creates a SourceRun per region and marks it
    success when the scan completes."""
    target = TargetAccount(
        aws_account_id="111111111111",
        regions=("eu-west-1", "us-east-1"),
    )

    def _scan(session, regions):
        for r in regions:
            yield {"_region": r, **_make_db()}

    result = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan,
    )

    assert result.resources_written == 2
    runs = session.query(SourceRunORM).all()
    assert len(runs) == 2
    for run in runs:
        assert run.status == "success"
        assert run.resources_found == 1
        assert run.finished_at is not None


def test_collector_marks_failed_run_on_region_error(session: Session) -> None:
    """A failing region produces a SourceRun with status='failed' + error."""
    target = TargetAccount(
        aws_account_id="111111111111",
        regions=("eu-west-1", "us-east-1"),
    )

    def _flaky_scan(session, regions):
        for region in regions:
            if region == "eu-west-1":
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                    "DescribeDBInstances",
                )
            yield {"_region": region, **_make_db()}

    collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_flaky_scan,
    )

    runs = session.query(SourceRunORM).all()
    assert len(runs) == 2
    by_region = {r.region: r for r in runs}
    assert by_region["eu-west-1"].status == "failed"
    assert "AccessDenied" in by_region["eu-west-1"].error
    assert by_region["us-east-1"].status == "success"


def test_collector_returns_none_run_when_scan_in_progress(session: Session) -> None:
    """If a scan is already running, the collector records an error and
    doesn't double-count resources."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    session.commit()

    target = TargetAccount(
        aws_account_id="111111111111",
        regions=("eu-west-1",),
    )
    result = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=lambda s, r: iter([{"_region": "eu-west-1", **_make_db()}]),
    )

    assert result.resources_written == 0
    assert any("in progress" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Stuck-run cleanup + force flag
# ---------------------------------------------------------------------------


def test_cleanup_stuck_runs_marks_old_running_as_failed(session: Session) -> None:
    """A run that's been 'running' for > threshold gets marked 'failed'."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    # Backdate started_at to 3 hours ago.
    run.started_at = datetime.now(tz=UTC) - timedelta(hours=3)
    session.commit()

    cleaned = source_runs_repo.cleanup_stuck_runs(session, threshold=timedelta(hours=1))
    assert cleaned == 1
    session.refresh(run)
    assert run.status == "failed"
    assert "stuck_run_cleanup" in (run.error or "")


def test_cleanup_stuck_runs_leaves_recent_runs_alone(session: Session) -> None:
    """A run that's been 'running' for < threshold is NOT touched."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    # started_at is "now" (default), well under 2h.
    session.commit()

    cleaned = source_runs_repo.cleanup_stuck_runs(session, threshold=timedelta(hours=2))
    assert cleaned == 0
    session.refresh(run)
    assert run.status == "running"


def test_start_run_with_force_aborts_active_run(session: Session) -> None:
    """force=True marks the active run as 'failed' so a new one can start."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    first = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    session.commit()
    assert first is not None

    # Without force: blocked
    blocked = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    assert blocked is None

    # With force: succeeds, and the first run is marked 'failed'.
    second = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
        force=True,
    )
    session.commit()
    assert second is not None
    assert second.id != first.id

    session.refresh(first)
    assert first.status == "failed"
    assert "aborted" in (first.error or "")
