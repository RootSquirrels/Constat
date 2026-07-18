"""Observations repository. Append-only by design."""

from __future__ import annotations

from uuid import uuid4

from constat_core.models import Observation
from sqlalchemy.orm import Session

from constat_api.orm import ObservationORM


def insert_observation(session: Session, observation: Observation) -> Observation:
    orm = ObservationORM(
        id=observation.id or uuid4(),
        resource_id=observation.resource_id,
        source=observation.source,
        observed_at=observation.observed_at,
        payload=observation.payload,
    )
    session.add(orm)
    session.flush()
    return Observation(
        id=orm.id,
        resource_id=orm.resource_id,
        source=orm.source,
        observed_at=orm.observed_at,
        payload=orm.payload,
    )


def count_observations(session: Session) -> int:
    from sqlalchemy import func as sa_func
    from sqlalchemy import select

    return int(session.execute(select(sa_func.count(ObservationORM.id))).scalar_one())
