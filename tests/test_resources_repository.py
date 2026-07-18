"""Tests for the resources repository."""

from __future__ import annotations

from constat_api.orm import AccountORM
from constat_api.repositories import resources as resources_repo
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
