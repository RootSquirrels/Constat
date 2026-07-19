"""CollectJob repository: one row per accepted async POST /collect/aws.

The job row is write-once: progress is derived from the source_runs that
carry the job_id, never updated on the job row itself. That keeps the
worker write path unchanged (it only writes source_runs) and means a
crashed worker cannot leave the job row in a lying state.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.orm import CollectJobORM, SourceRunORM
from constat_api.tenant import current_tenant


def create_job(
    session: Session,
    *,
    actor: str,
    total_items: int,
    summary: dict[str, Any],
) -> CollectJobORM:
    """Insert the job row for an accepted collect request.

    `summary` must be counts only (accounts / regions / resource_types) —
    the same non-PII discipline as audit_events metadata.
    """
    job = CollectJobORM(actor=actor, total_items=total_items, summary=summary)
    tenant_id = current_tenant(session)
    if tenant_id is not None:
        job.tenant_id = tenant_id
    session.add(job)
    session.flush()
    return job


def get_job(session: Session, job_id: UUID) -> CollectJobORM | None:
    """Fetch a job by id, or None (the API maps None to 404)."""
    return session.execute(
        select(CollectJobORM).where(CollectJobORM.job_id == job_id)
    ).scalar_one_or_none()


def list_runs_for_job(session: Session, job_id: UUID) -> list[SourceRunORM]:
    """All source_runs written for this job, oldest first.

    One work item (account x region) produces one run per scanned
    resource type, so runs outnumber items when resource_types is set.
    """
    stmt = (
        select(SourceRunORM)
        .where(SourceRunORM.job_id == job_id)
        .order_by(SourceRunORM.started_at.asc())
    )
    return list(session.execute(stmt).scalars())
