"""Tests for the AWS collector.

Strategy: dependency injection. assume_role_fn and scan_fn are mocked so no
real boto3 calls happen. moto would also work but DI is simpler and faster.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

from botocore.exceptions import ClientError
from constat_api.collectors.aws import TargetAccount, collect_target, collect_targets
from constat_api.orm import FactORM, ObservationORM, ResourceORM
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


def _scan_factory(instances: list[dict[str, Any]]):
    def _scan(session, regions):
        for region in regions:
            for inst in instances:
                inst["_region"] = region
                yield inst

    return _scan


def test_collect_writes_resource_and_facts(session: Session) -> None:
    target = TargetAccount(
        aws_account_id="111111111111",
        role_arn=None,
        name="prod",
        regions=("eu-west-1",),
    )
    base = MagicMock()
    result = collect_target(
        session,
        target,
        base_session=base,
        assume_role_fn=_no_assume_role,
        scan_fn=_scan_factory([_make_db()]),
    )

    assert result.resources_written == 1
    assert result.facts_written == 4  # engine, engine_version, instance_class, vcpu
    assert result.observations_written == 1
    assert result.errors == []

    # DB-level asserts
    resources = session.query(ResourceORM).all()
    assert len(resources) == 1
    assert resources[0].native_id == _make_db()["DBInstanceArn"]

    facts = session.query(FactORM).all()
    keys = {f.key for f in facts}
    assert keys == {"engine", "engine_version", "instance_class", "vcpu"}
    assert any(f.value == "postgres" for f in facts)
    assert any(f.value == 4 for f in facts)  # m5.xlarge vCPU

    obs = session.query(ObservationORM).all()
    assert len(obs) == 1
    assert obs[0].source == "aws_rds"


def test_collect_updates_last_seen_at_on_existing_resource(session: Session) -> None:
    # First scan
    target = TargetAccount(aws_account_id="111111111111", regions=("eu-west-1",))
    result1 = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan_factory([_make_db()]),
    )
    assert result1.resources_written == 1

    first = session.query(ResourceORM).one()
    first_seen = first.first_seen_at
    last_seen = first.last_seen_at

    # Second scan: same DB, should update last_seen_at
    result2 = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan_factory([_make_db()]),
    )
    assert result2.resources_written == 1
    assert session.query(ResourceORM).count() == 1

    second = session.query(ResourceORM).one()
    assert second.first_seen_at == first_seen  # unchanged
    assert second.last_seen_at >= last_seen  # bumped


def test_collect_dry_run_does_not_write_facts(session: Session) -> None:
    target = TargetAccount(aws_account_id="111111111111", regions=("eu-west-1",))
    result = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan_factory([_make_db()]),
        dry_run=True,
    )

    assert result.resources_written == 1  # counted
    assert result.facts_written == 0  # not written
    assert result.observations_written == 0

    assert session.query(ResourceORM).count() == 1  # resource written (we always upsert)
    assert session.query(FactORM).count() == 0
    assert session.query(ObservationORM).count() == 0


def test_collect_handles_multiple_regions(session: Session) -> None:
    target = TargetAccount(aws_account_id="111111111111", regions=("eu-west-1", "us-east-1"))
    result = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan_factory([_make_db()]),
    )

    assert result.resources_written == 2
    assert session.query(ResourceORM).count() == 2
    regions = {r.region for r in session.query(ResourceORM).all()}
    assert regions == {"eu-west-1", "us-east-1"}


def test_collect_continues_on_region_error(session: Session) -> None:
    """A failed region should not abort the rest of the scan."""

    def _flaky_scan(session, regions):
        for region in regions:
            if region == "eu-west-1":
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                    "DescribeDBInstances",
                )
            yield {"_region": region, **_make_db()}

    target = TargetAccount(aws_account_id="111111111111", regions=("eu-west-1", "us-east-1"))
    result = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_flaky_scan,
    )

    assert result.resources_written == 1
    assert len(result.errors) == 1
    assert "eu-west-1" in result.errors[0]


def test_collect_targets_continues_on_assume_role_failure(session: Session) -> None:
    def _bad_assume(base_session, target):
        # Realistic: short-circuit when no role (use base session), only fail on real roles.
        if target.role_arn is None:
            return base_session
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "trust policy rejects"}},
            "AssumeRole",
        )

    targets = [
        TargetAccount(aws_account_id="111111111111", role_arn="arn:bad", regions=("eu-west-1",)),
        TargetAccount(aws_account_id="222222222222", regions=("us-east-1",)),
    ]

    def _scan(session, regions):
        for r in regions:
            yield {"_region": r, **_make_db()}

    results = collect_targets(
        session,
        targets,
        base_session=MagicMock(),
        assume_role_fn=_bad_assume,
        scan_fn=_scan,
    )

    assert len(results) == 2
    assert "assume_role" in results[0].errors[0]
    assert results[0].resources_written == 0
    assert results[1].resources_written == 1  # no role_arn, uses base session


# ---------------------------------------------------------------------------
# Retirement: a successful scan retires resources not seen in the latest run.
# ---------------------------------------------------------------------------


def _scan_factory_with(arns: list[str]):
    """Build a scan_fn that yields one DB instance per (region, arn) pair."""

    def _scan(s, regions):
        for region in regions:
            for arn in arns:
                yield {"_region": region, **_make_db(arn=arn)}

    return _scan


def test_collect_retires_resources_not_seen_in_latest_scan(session: Session) -> None:
    """After a successful scan, the resources not present in the scan
    are retired (this is the GTM promise: 'we never claim a resource is
    alive without proof')."""
    # Scan #1: see arn:1 and arn:2 in eu-west-1
    target = TargetAccount(aws_account_id="111111111111", regions=("eu-west-1",))
    collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan_factory_with(["arn:1", "arn:2"]),
    )
    r1 = session.query(ResourceORM).filter_by(native_id="arn:1").one()
    r2 = session.query(ResourceORM).filter_by(native_id="arn:2").one()
    assert r1.retired_at is None
    assert r2.retired_at is None

    # Backdate r1's last_seen_at to "long ago" so a fresh scan will see it as stale.
    r1.last_seen_at = datetime.now(tz=UTC) - timedelta(days=7)
    session.commit()

    # Scan #2: only arn:2 is found (arn:1 was deleted in AWS).
    collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan_factory_with(["arn:2"]),
    )

    session.refresh(r1)
    session.refresh(r2)
    assert r1.retired_at is not None, "arn:1 was not in the latest scan, should be retired"
    assert r2.retired_at is None, "arn:2 was just seen, should still be active"


def test_collect_resurrects_resource_that_comes_back(session: Session) -> None:
    """A resource that was retired but reappears in a later scan is
    resurrected (retired_at cleared, last_seen_at bumped)."""
    target = TargetAccount(aws_account_id="111111111111", regions=("eu-west-1",))

    # Scan #1: arn:1
    collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan_factory_with(["arn:1"]),
    )
    r1 = session.query(ResourceORM).filter_by(native_id="arn:1").one()
    first_seen = r1.first_seen_at

    # Manually retire it
    r1.retired_at = datetime.now(tz=UTC)
    session.commit()
    r1_id = r1.id

    # Scan #2: arn:1 reappears
    collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan_factory_with(["arn:1"]),
    )

    # The row should still be unique (no duplicate), retired_at cleared.
    all_resources = session.query(ResourceORM).filter_by(native_id="arn:1").all()
    assert len(all_resources) == 1
    resurrected = all_resources[0]
    assert resurrected.id == r1_id
    assert resurrected.retired_at is None
    assert resurrected.first_seen_at == first_seen


def test_collect_uses_force_to_override_stuck_run(session: Session) -> None:
    """force=True lets a new scan start even when the previous one is stuck."""
    from constat_api.repositories import accounts as accounts_repo
    from constat_api.repositories import source_runs as source_runs_repo

    target = TargetAccount(aws_account_id="111111111111", regions=("eu-west-1",))

    # Pre-existing stuck run
    acc = accounts_repo.get_or_create(session, "111111111111")
    source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    session.commit()

    # Without force: scan is skipped
    result = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan_factory([_make_db()]),
    )
    assert result.resources_written == 0
    assert any("in progress" in e for e in result.errors)

    # With force: stuck run is aborted, scan runs.
    result2 = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan_factory([_make_db()]),
        force=True,
    )
    assert result2.resources_written == 1
    assert result2.errors == []
