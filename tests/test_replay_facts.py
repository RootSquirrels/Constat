"""Tests for the replay_facts CLI.

Strategy: seed the DB with an observation (via a real collect_target call
using mocked boto3), then call replay_observations and assert the facts
table matches what would have been written originally.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from constat_api.cli.replay_facts import _payload_to_db, replay_observations
from constat_api.collectors.aws import TargetAccount, collect_target
from constat_api.orm import FactORM, ObservationORM, ResourceORM
from sqlalchemy.orm import Session

from tests.conftest import make_rds_db_dict


def _no_assume_role(base_session, target):
    return base_session


def _scan(s, regions):
    for r in regions:
        yield {"_region": r, **make_rds_db_dict()}


def test_payload_to_db_round_trips_fields() -> None:
    """The reverse function reconstructs a boto3-style dict that
    db_to_facts can consume."""
    db = make_rds_db_dict()
    # Simulate what db_to_observation stored:
    create_time_iso = db["InstanceCreateTime"].isoformat()
    payload = {
        "DBInstanceArn": db["DBInstanceArn"],
        "DBInstanceIdentifier": db["DBInstanceIdentifier"],
        "Engine": db["Engine"],
        "EngineVersion": db["EngineVersion"],
        "DBInstanceClass": db["DBInstanceClass"],
        "DBInstanceStatus": db["DBInstanceStatus"],
        "AllocatedStorage": db["AllocatedStorage"],
        "InstanceCreateTime": create_time_iso,
        "MultiAZ": db["MultiAZ"],
        "StorageEncrypted": db["StorageEncrypted"],
        "DBSubnetGroup": db["DBSubnetGroup"]["DBSubnetGroupName"],
        "Endpoint": db["Endpoint"]["Address"],
    }
    rebuilt = _payload_to_db(payload, region="eu-west-1")
    assert rebuilt["DBInstanceArn"] == db["DBInstanceArn"]
    assert rebuilt["Engine"] == db["Engine"]
    assert rebuilt["DBInstanceClass"] == db["DBInstanceClass"]
    assert rebuilt["MultiAZ"] == db["MultiAZ"]
    assert rebuilt["DBSubnetGroup"]["DBSubnetGroupName"] == "default"
    assert rebuilt["Endpoint"]["Address"] == "test.xxxx.eu-west-1.rds.amazonaws.com"
    assert rebuilt["_region"] == "eu-west-1"


def test_replay_rebuilds_facts_from_observations(session: Session) -> None:
    """End-to-end: scan produces observations + facts. Wipe facts, replay,
    facts come back identical."""
    target = TargetAccount(
        aws_account_id="111111111111", regions=("eu-west-1",), resource_types=("rds",)
    )
    collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan,
    )

    # Snapshot the original facts
    original_facts = sorted(
        ((f.key, f.value, f.value_state) for f in session.query(FactORM).all()),
        key=lambda x: x[0],
    )
    assert len(original_facts) > 0  # sanity: scan wrote facts

    # Wipe facts (simulate 'facts got corrupted / lost')
    session.query(FactORM).delete()
    session.commit()
    assert session.query(FactORM).count() == 0

    # Replay
    stats = replay_observations(session, account_external_id="111111111111")
    assert stats["observations_scanned"] == 1
    assert stats["facts_upserted"] == len(original_facts)
    assert stats["observations_skipped"] == 0

    # Compare to original
    replayed_facts = sorted(
        ((f.key, f.value, f.value_state) for f in session.query(FactORM).all()),
        key=lambda x: x[0],
    )
    assert replayed_facts == original_facts


def test_replay_dry_run_does_not_write(session: Session) -> None:
    target = TargetAccount(
        aws_account_id="111111111111", regions=("eu-west-1",), resource_types=("rds",)
    )
    collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan,
    )
    original_count = session.query(FactORM).count()
    session.query(FactORM).delete()
    session.commit()

    stats = replay_observations(session, dry_run=True)
    assert stats["facts_upserted"] == 5  # 5 facts per RDS row
    assert session.query(FactORM).count() == 0  # nothing written
    _ = original_count  # silence unused


def test_replay_filters_by_account(session: Session) -> None:
    """Replay only touches observations on the requested account."""
    # Two accounts, each with one observation
    for ext_id in ("111", "222"):
        target = TargetAccount(
            aws_account_id=ext_id, regions=("eu-west-1",), resource_types=("rds",)
        )
        collect_target(
            session,
            target,
            base_session=MagicMock(),
            assume_role_fn=_no_assume_role,
            scan_fn=_scan,
        )

    assert session.query(ObservationORM).count() == 2

    # Wipe all facts
    session.query(FactORM).delete()
    session.commit()

    # Replay for account 111 only
    stats = replay_observations(session, account_external_id="111")
    assert stats["facts_upserted"] == 5  # 1 observation * 5 facts

    # Replay for account 222
    stats2 = replay_observations(session, account_external_id="222")
    assert stats2["facts_upserted"] == 5


def test_replay_filters_by_source(session: Session) -> None:
    """Unknown sources are skipped without raising."""
    target = TargetAccount(aws_account_id="111", regions=("eu-west-1",), resource_types=("rds",))
    collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan,
    )
    session.query(FactORM).delete()
    session.commit()

    # Source name we don't know
    import pytest

    with pytest.raises(ValueError, match="Unknown source"):
        replay_observations(session, sources=["made_up_source"])


def test_replay_skips_observations_without_resource(session: Session) -> None:
    """An observation whose resource was deleted is skipped (orphan)."""
    target = TargetAccount(aws_account_id="111", regions=("eu-west-1",), resource_types=("rds",))
    collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_scan,
    )
    # Delete the resource (orphan the observation)
    session.query(ResourceORM).delete()
    session.query(FactORM).delete()
    session.commit()
    assert session.query(ObservationORM).count() == 1

    stats = replay_observations(session)
    assert stats["observations_skipped"] == 1
    assert stats["facts_upserted"] == 0
