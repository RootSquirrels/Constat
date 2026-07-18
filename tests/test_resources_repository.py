"""Tests for the resources repository."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from constat_api.orm import AccountORM, SourceRunORM
from constat_api.repositories import resources as resources_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.settings import DEFAULT_TENANT_ID
from sqlalchemy.orm import Session


def _account(session: Session) -> AccountORM:
    acc = AccountORM(external_id="111111111111", name="test")
    session.add(acc)
    session.commit()
    return acc


def test_upsert_resource_creates_new(session: Session) -> None:
    acc = _account(session)
    r = resources_repo.upsert_resource(
        session,
        acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:aws:rds:eu-west-1:111111111111:db:test",
    )
    session.commit()
    assert r.id is not None
    assert r.first_seen_at is not None
    assert r.last_seen_at is not None


def test_upsert_resource_updates_existing(session: Session) -> None:
    acc = _account(session)
    r1 = resources_repo.upsert_resource(
        session,
        acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:1",
    )
    session.commit()
    first_seen = r1.first_seen_at
    last_seen = r1.last_seen_at

    r2 = resources_repo.upsert_resource(
        session,
        acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:1",
    )
    session.commit()

    assert r1.id == r2.id
    assert r2.first_seen_at == first_seen
    assert r2.last_seen_at >= last_seen


def test_upsert_resource_distinguishes_by_region(session: Session) -> None:
    acc = _account(session)
    resources_repo.upsert_resource(
        session,
        acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:1",
    )
    resources_repo.upsert_resource(
        session,
        acc.id,
        region="us-east-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:1",
    )
    session.commit()

    count = resources_repo.count_resources(session, acc.id)
    assert count == 2


# ---------------------------------------------------------------------------
# Resurrection: a retired resource seen again becomes active.
# ---------------------------------------------------------------------------


def test_upsert_resource_resurrects_retired(session: Session) -> None:
    """If the natural key matches a retired resource, the upsert
    resurrects it (clears retired_at, bumps last_seen_at) instead of
    creating a duplicate row. first_seen_at is preserved."""
    acc = _account(session)
    r1 = resources_repo.upsert_resource(
        session,
        acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:1",
    )
    session.commit()
    first_seen = r1.first_seen_at
    r1.retired_at = datetime.now(tz=UTC)
    session.commit()

    r2 = resources_repo.upsert_resource(
        session,
        acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:1",
    )
    session.commit()

    assert r1.id == r2.id  # same row, not a duplicate
    assert r2.retired_at is None
    assert r2.first_seen_at == first_seen  # historical truth preserved
    assert r2.last_seen_at is not None


# ---------------------------------------------------------------------------
# Retirement: two consecutive successful scans prove stale resources are gone.
# ---------------------------------------------------------------------------


def _complete_run(session, acc, *, region="eu-west-1") -> SourceRunORM:
    """Helper: create a SourceRun and mark it success right away."""
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region=region,
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    source_runs_repo.finish_run(session, run, status="success", resources_found=1)
    session.commit()
    return run


def test_retire_stale_resources_marks_unseen_as_retired(session: Session) -> None:
    """Two consecutive successful scans that both miss a resource retire
    it (F-08). A resource seen in either run stays active."""
    acc = _account(session)
    r1 = resources_repo.upsert_resource(
        session,
        acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:1",
    )
    r2 = resources_repo.upsert_resource(
        session,
        acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:2",
    )
    session.commit()

    # Backdate r1 (it'll be the "stale" one)
    r1.last_seen_at = datetime.now(tz=UTC) - timedelta(hours=2)
    # Bump r2 (just-seen)
    r2.last_seen_at = datetime.now(tz=UTC)
    session.commit()

    # Two successful scans now, both missing r1.
    _complete_run(session, acc)
    _complete_run(session, acc)

    retired = resources_repo.retire_stale_resources(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    assert retired == 1

    session.refresh(r1)
    session.refresh(r2)
    assert r1.retired_at is not None
    assert r2.retired_at is None


def test_retire_stale_resources_does_nothing_without_proof(session: Session) -> None:
    """No successful source_run = no proof the scope was scanned. Don't retire
    anything (the runner will emit INCONCLUSIVE for these resources)."""
    acc = _account(session)
    r = resources_repo.upsert_resource(
        session,
        acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:1",
    )
    r.last_seen_at = datetime.now(tz=UTC) - timedelta(days=30)
    session.commit()

    retired = resources_repo.retire_stale_resources(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    assert retired == 0
    session.refresh(r)
    assert r.retired_at is None


def test_retire_stale_resources_does_nothing_after_a_single_scan(session: Session) -> None:
    """F-08: ONE successful scan is not proof of deletion. Even a very
    stale resource survives the first successful scan of the scope."""
    acc = _account(session)
    r = resources_repo.upsert_resource(
        session,
        acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:1",
    )
    r.last_seen_at = datetime.now(tz=UTC) - timedelta(days=30)
    session.commit()

    _complete_run(session, acc)

    retired = resources_repo.retire_stale_resources(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    assert retired == 0
    session.refresh(r)
    assert r.retired_at is None


def test_retire_stale_resources_is_idempotent(session: Session) -> None:
    """A second call after two successful scans retires 0 rows."""
    acc = _account(session)
    r = resources_repo.upsert_resource(
        session,
        acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:1",
    )
    r.last_seen_at = datetime.now(tz=UTC) - timedelta(hours=2)
    session.commit()

    _complete_run(session, acc)
    _complete_run(session, acc)
    first = resources_repo.retire_stale_resources(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    assert first == 1

    second = resources_repo.retire_stale_resources(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    assert second == 0


# Keep the import of DEFAULT_TENANT_ID live for side-effect-free imports.
_ = DEFAULT_TENANT_ID
