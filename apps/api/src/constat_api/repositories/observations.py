"""Observations repository. Append-only by design."""

from __future__ import annotations

from uuid import UUID, uuid4

from constat_core.models import Observation
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from constat_api.orm import ObservationORM
from constat_api.tenant import tenant_or_default


def insert_observation(
    session: Session,
    observation: Observation,
    *,
    source_run_id: UUID | None = None,
) -> Observation:
    """Append an observation, optionally chained to its source run.

    `source_run_id` is set by the collector. Without it, the observation is
    not chained to a scope-completeness proof (acceptable for synthetic/test
    data, never for production data).
    """
    orm = ObservationORM(
        id=observation.id or uuid4(),
        tenant_id=tenant_or_default(session),
        resource_id=observation.resource_id,
        source=observation.source,
        observed_at=observation.observed_at,
        payload=observation.payload,
        source_run_id=source_run_id,
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
    return int(session.execute(select(func.count(ObservationORM.id))).scalar_one())
