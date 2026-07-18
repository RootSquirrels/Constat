"""Tests for the facts current-state design + source_run_id chain.

V1 design: facts is current-state (one row per natural key, observed_at is
the timestamp of the most recent observation, not part of the key). facts
and observations both link to source_runs via FK.
"""

from __future__ import annotations

from datetime import UTC, datetime

from constat_api.orm import AccountORM, FactORM, ObservationORM, ResourceORM, SourceRunORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import observations as obs_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.settings import DEFAULT_TENANT_ID
from constat_core.models import Fact, Observation, ValueState
from sqlalchemy.orm import Session


def _bootstrap(session: Session) -> tuple[AccountORM, ResourceORM, SourceRunORM]:
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
    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    source_runs_repo.finish_run(session, run, status="success", resources_found=1)
    session.commit()
    return acc, resource, run


def _fact(resource_id, key, value="postgres", observed_at=None, account_id=None) -> Fact:
    return Fact(
        resource_id=resource_id,
        account_id=account_id,
        namespace="aws.rds",
        key=key,
        value=value,
        value_state=ValueState.KNOWN,
        source="aws_rds",
        observed_at=observed_at or datetime(2026, 7, 18, tzinfo=UTC),
    )


# ---- Current-state UNIQUE ---------------------------------------------------


def test_same_natural_key_upserts_not_duplicates(session: Session) -> None:
    """With observed_at out of the UNIQUE, same natural key always upserts.
    Two facts with the same (resource, namespace, key, source) must collapse
    into one row, with the latest observed_at and value."""
    acc, resource, run = _bootstrap(session)

    facts_repo.upsert_facts(
        session,
        [_fact(resource.id, "engine", "postgres", account_id=str(acc.id))],
        source_run_id=run.id,
    )
    session.commit()
    later = datetime(2026, 7, 19, tzinfo=UTC)
    facts_repo.upsert_facts(
        session,
        [_fact(resource.id, "engine", "postgres15", observed_at=later, account_id=str(acc.id))],
        source_run_id=run.id,
    )
    session.commit()

    rows = session.query(FactORM).all()
    assert len(rows) == 1
    assert rows[0].value == "postgres15"
    # The DB strips tzinfo; compare on the naive datetime.
    assert rows[0].observed_at.replace(tzinfo=None) == later.replace(tzinfo=None)
    assert rows[0].last_source_run_id == run.id


def test_upsert_returns_inserted_and_updated_counts(session: Session) -> None:
    acc, resource, run = _bootstrap(session)
    inserted, updated = facts_repo.upsert_facts(
        session,
        [
            _fact(resource.id, "engine", account_id=str(acc.id)),
            _fact(resource.id, "version", account_id=str(acc.id)),
        ],
        source_run_id=run.id,
    )
    session.commit()
    assert inserted == 2
    assert updated == 0

    inserted, updated = facts_repo.upsert_facts(
        session,
        [_fact(resource.id, "engine", "postgres14", account_id=str(acc.id))],
        source_run_id=run.id,
    )
    session.commit()
    assert inserted == 0
    assert updated == 1


def test_upsert_with_no_source_run_allowed(session: Session) -> None:
    """Synthetic / test data may not have a source_run. The chain is optional."""
    _acc, resource, _ = _bootstrap(session)
    inserted, _ = facts_repo.upsert_facts(
        session, [_fact(resource.id, "engine")], source_run_id=None
    )
    session.commit()
    assert inserted == 1
    assert session.query(FactORM).one().last_source_run_id is None


# ---- source_run_id chain ---------------------------------------------------


def test_observation_links_to_source_run(session: Session) -> None:
    _acc, resource, run = _bootstrap(session)
    obs = Observation(
        resource_id=resource.id,
        source="aws_rds",
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
        payload={"x": 1},
    )
    obs_repo.insert_observation(session, obs, source_run_id=run.id)
    session.commit()

    row = session.query(ObservationORM).one()
    assert row.source_run_id == run.id


def test_observation_without_source_run_allowed(session: Session) -> None:
    _acc, resource, _ = _bootstrap(session)
    obs = Observation(
        resource_id=resource.id,
        source="aws_rds",
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
        payload={"x": 1},
    )
    obs_repo.insert_observation(session, obs)  # no source_run_id
    session.commit()

    row = session.query(ObservationORM).one()
    assert row.source_run_id is None


def test_fact_links_to_most_recent_source_run(session: Session) -> None:
    """When the same fact is upserted by two different runs, the link updates."""
    acc, resource, run1 = _bootstrap(session)
    run2 = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    session.commit()

    facts_repo.upsert_facts(session, [_fact(resource.id, "engine")], source_run_id=run1.id)
    session.commit()
    later = datetime(2026, 7, 19, tzinfo=UTC)
    facts_repo.upsert_facts(
        session,
        [_fact(resource.id, "engine", observed_at=later)],
        source_run_id=run2.id,
    )
    session.commit()

    row = session.query(FactORM).one()
    assert row.last_source_run_id == run2.id
    assert row.last_source_run_id != run1.id


# ---- List semantics --------------------------------------------------------


def test_list_facts_for_resource_returns_current(session: Session) -> None:
    """The list returns current facts (one per natural key), not history."""
    acc, resource, run = _bootstrap(session)
    facts_repo.upsert_facts(
        session,
        [
            _fact(resource.id, "engine", "postgres", account_id=str(acc.id)),
            _fact(resource.id, "version", "14.7", account_id=str(acc.id)),
        ],
        source_run_id=run.id,
    )
    session.commit()

    listed = facts_repo.list_facts_for_resource(session, resource.id)
    assert len(listed) == 2
    keys = {f.key for f in listed}
    assert keys == {"engine", "version"}


def test_list_facts_for_resource_observes_observed_at_cutoff(session: Session) -> None:
    """V1 is current-state: only the latest fact per natural key exists.
    A cutoff before the latest observation returns nothing for that fact.
    For history queries, use observations (raw payloads) + source_runs."""
    acc, resource, run = _bootstrap(session)
    t1 = datetime(2026, 7, 18, tzinfo=UTC)
    t2 = datetime(2026, 7, 19, tzinfo=UTC)
    facts_repo.upsert_facts(
        session,
        [_fact(resource.id, "engine", "v1", t1, account_id=str(acc.id))],
        source_run_id=run.id,
    )
    session.commit()
    facts_repo.upsert_facts(
        session,
        [_fact(resource.id, "engine", "v2", t2, account_id=str(acc.id))],
        source_run_id=run.id,
    )
    session.commit()

    # At t1: v1 was current. But v2 overwrote it (current-state design).
    # So list(observed_at=t1) returns 0 -- we only have v2 now.
    listed = facts_repo.list_facts_for_resource(session, resource.id, observed_at=t1)
    assert len(listed) == 0

    # No cutoff: we see v2.
    listed_all = facts_repo.list_facts_for_resource(session, resource.id)
    assert len(listed_all) == 1
    assert listed_all[0].value == "v2"
