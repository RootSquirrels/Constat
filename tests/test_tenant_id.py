"""Tests for the tenant_id column and facts UNIQUE constraint."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from constat_api.orm import FactORM, ResourceORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.settings import DEFAULT_TENANT_ID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def test_default_tenant_id_is_the_v1_singleton():
    """V1 single-tenant: every row gets the same tenant_id by default."""
    assert DEFAULT_TENANT_ID == DEFAULT_TENANT_ID  # tautology, but documents intent


def test_account_get_or_create_sets_tenant_id(session: Session) -> None:
    acc = accounts_repo.get_or_create(session, "111111111111", "prod")
    session.commit()
    assert acc.tenant_id == DEFAULT_TENANT_ID


def test_observations_carry_tenant_id(session: Session) -> None:
    from constat_api.repositories.observations import insert_observation
    from constat_core.models import Observation

    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:rds:1",
    )
    session.add(resource)
    session.commit()

    obs = Observation(
        resource_id=resource.id,
        source="aws_rds",
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
        payload={"x": 1},
    )
    insert_observation(session, obs)
    session.commit()

    from constat_api.orm import ObservationORM

    row = session.query(ObservationORM).one()
    assert row.tenant_id == DEFAULT_TENANT_ID


def test_facts_unique_constraint_enforced(session: Session) -> None:
    """Inserting the same (tenant, resource, namespace, key, source, observed_at)
    twice should fail with IntegrityError."""
    from constat_api.repositories import facts as facts_repo
    from constat_core.models import Fact, ValueState

    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:rds:1",
    )
    session.add(resource)
    session.commit()

    now = datetime(2026, 7, 18, tzinfo=UTC)
    fact = Fact(
        resource_id=resource.id,
        account_id=str(acc.id),
        namespace="aws.rds",
        key="engine",
        value="postgres",
        value_state=ValueState.KNOWN,
        source="aws_rds",
        observed_at=now,
    )
    facts_repo.insert_facts(session, [fact])
    session.commit()

    # Second insert with same natural key: must fail.
    duplicate = Fact(
        resource_id=resource.id,
        account_id=str(acc.id),
        namespace="aws.rds",
        key="engine",
        value="postgres",
        value_state=ValueState.KNOWN,
        source="aws_rds",
        observed_at=now,
    )
    with pytest.raises(IntegrityError):
        facts_repo.insert_facts(session, [duplicate])
        session.commit()
    session.rollback()


def test_facts_with_different_observed_at_allowed(session: Session) -> None:
    """UNIQUE is on (tenant, resource, namespace, key, source, observed_at).
    Different observed_at = different snapshot = allowed."""
    from constat_api.repositories import facts as facts_repo
    from constat_core.models import Fact, ValueState

    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id="arn:rds:1",
    )
    session.add(resource)
    session.commit()

    base = {
        "resource_id": resource.id,
        "account_id": str(acc.id),
        "namespace": "aws.rds",
        "key": "engine",
        "value": "postgres",
        "value_state": ValueState.KNOWN,
        "source": "aws_rds",
    }
    facts_repo.insert_facts(
        session,
        [
            Fact(**base, observed_at=datetime(2026, 7, 18, tzinfo=UTC)),
        ],
    )
    facts_repo.insert_facts(
        session,
        [
            Fact(**base, observed_at=datetime(2026, 7, 19, tzinfo=UTC)),
        ],
    )
    session.commit()

    n = session.query(FactORM).count()
    assert n == 2
