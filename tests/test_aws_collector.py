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

from tests.conftest import make_rds_db_dict


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
        scan_fn=_scan_factory([make_rds_db_dict()]),
    )

    assert result.resources_written == 1
    assert result.facts_written == 5  # engine, engine_version, instance_class, vcpu, region
    assert result.observations_written == 1
    assert result.errors == []

    # DB-level asserts
    resources = session.query(ResourceORM).all()
    assert len(resources) == 1
    assert resources[0].native_id == make_rds_db_dict()["DBInstanceArn"]

    facts = session.query(FactORM).all()
    keys = {f.key for f in facts}
    assert keys == {"engine", "engine_version", "instance_class", "vcpu", "region"}
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
        scan_fn=_scan_factory([make_rds_db_dict()]),
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
        scan_fn=_scan_factory([make_rds_db_dict()]),
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
        scan_fn=_scan_factory([make_rds_db_dict()]),
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
        scan_fn=_scan_factory([make_rds_db_dict()]),
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
            yield {"_region": region, **make_rds_db_dict()}

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
            yield {"_region": r, **make_rds_db_dict()}

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
                yield {"_region": region, **make_rds_db_dict(arn=arn)}

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
        scan_fn=_scan_factory([make_rds_db_dict()]),
    )
    assert result.resources_written == 0
    assert any("in progress" in e for e in result.errors)

    # With force: stuck run is aborted, scan runs.
    result2 = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan_factory([make_rds_db_dict()]),
        force=True,
    )
    assert result2.resources_written == 1
    assert result2.errors == []


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_collect_circuit_breaker_skips_after_consecutive_failures(
    session: Session,
) -> None:
    """After max_consecutive_region_errors consecutive failures, the rest
    of the regions are skipped. A single success in between resets the
    counter."""
    target = TargetAccount(
        aws_account_id="111111111111",
        regions=("r1", "r2", "r3", "r4", "r5"),
    )

    def _scan(s, regions):
        for region in regions:
            if region in ("r1", "r2"):
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                    "DescribeDBInstances",
                )
            yield {"_region": region, **make_rds_db_dict()}

    result = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan,
        max_consecutive_region_errors=2,
    )

    # r1 and r2 fail (2 errors -> trip the breaker).
    # r3, r4, r5 are skipped.
    assert result.resources_written == 0
    assert result.regions_skipped_by_breaker == ["r3", "r4", "r5"]
    # 2 real errors + 3 skipped-by-breaker notes
    real_errors = [e for e in result.errors if "circuit breaker" not in e]
    breaker_notes = [e for e in result.errors if "circuit breaker" in e]
    assert len(real_errors) == 2
    assert len(breaker_notes) == 3


def test_collect_circuit_breaker_resets_on_success(session: Session) -> None:
    """A success between two failures resets the counter. Pattern:
    r1 fail, r2 success, r3 fail, r4 fail, r5 success.

    r1 fail (consec=1)
    r2 success (consec=0) <- reset
    r3 fail (consec=1)
    r4 fail (consec=2) <- trip
    r5 skipped by breaker

    So we should see 2 successes (r2, r5 NOT seen — actually r5 is skipped
    because we tripped at r4, so only r2 succeeds). Let me re-think.

    Actually: the breaker checks at the top of the loop. After r4 fail
    (consec=2), the next iteration (r5) sees consec >= 2 and skips.

    So successes: r2 only. resources_written == 1. Regions skipped:
    r5. r3 and r4 did not get skipped because the counter was reset
    by r2. Without the reset, r3 would have been skipped too.
    """
    target = TargetAccount(
        aws_account_id="111111111111",
        regions=("r1", "r2", "r3", "r4", "r5"),
    )

    def _scan(s, regions):
        for region in regions:
            if region in ("r1", "r3", "r4"):
                raise ClientError(
                    {"Error": {"Code": "Throttling", "Message": "slow down"}},
                    "DescribeDBInstances",
                )
            yield {"_region": region, **make_rds_db_dict()}

    result = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan,
        max_consecutive_region_errors=2,
    )

    # r1 fail (consec=1), r2 success (reset), r3 fail (consec=1),
    # r4 fail (consec=2, trip), r5 skipped.
    assert result.resources_written == 1  # only r2 succeeded
    assert result.regions_skipped_by_breaker == ["r5"]
    # Sanity: r3 and r4 were NOT skipped (the counter was reset by r2).
    skipped = result.regions_skipped_by_breaker
    assert "r3" not in skipped
    assert "r4" not in skipped


def test_collect_circuit_breaker_disabled_when_max_is_high(session: Session) -> None:
    """max_consecutive_region_errors=0 means: never trip (per-region errors
    don't accumulate toward a breaker threshold)."""
    target = TargetAccount(
        aws_account_id="111111111111",
        regions=("r1", "r2", "r3"),
    )

    def _scan(s, regions):
        for region in regions:
            if region in ("r1", "r2"):
                raise ClientError(
                    {"Error": {"Code": "AccessDenied"}},
                    "DescribeDBInstances",
                )
            yield {"_region": region, **make_rds_db_dict()}

    result = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan,
        max_consecutive_region_errors=100,  # high enough to never trip
    )

    assert result.resources_written == 1  # r3 succeeded
    assert result.regions_skipped_by_breaker == []


def test_collect_circuit_breaker_trips_at_threshold_one(session: Session) -> None:
    """With threshold=1, the very first error trips the breaker."""
    target = TargetAccount(
        aws_account_id="111111111111",
        regions=("r1", "r2", "r3"),
    )

    def _scan(s, regions):
        for region in regions:
            if region == "r1":
                raise ClientError(
                    {"Error": {"Code": "AccessDenied"}},
                    "DescribeDBInstances",
                )
            yield {"_region": region, **make_rds_db_dict()}

    result = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan,
        max_consecutive_region_errors=1,
    )

    assert result.resources_written == 0
    assert result.regions_skipped_by_breaker == ["r2", "r3"]
