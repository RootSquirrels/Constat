"""SourceRun repository: lifecycle for scope-completeness tracking.

A SourceRun proves "I scanned account X, region Y, type Z with source S
and found N resources (or failed)". When status='success', absence of a
resource in that scope is PROVEN, not guessed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from constat_api.orm import SourceRunORM
from constat_api.settings import DEFAULT_TENANT_ID


def start_run(
    session: Session,
    *,
    account_id: UUID,
    region: str,
    resource_type: str,
    source: str,
) -> SourceRunORM | None:
    """Start a new run. Returns None if one is already active for this scope.

    The caller should handle None: either skip the scan (someone else is
    scanning) or treat it as a duplicate attempt.
    """
    run = SourceRunORM(
        id=uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        account_id=account_id,
        region=region,
        resource_type=resource_type,
        source=source,
        status="running",
    )
    session.add(run)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    return run


def finish_run(
    session: Session,
    run: SourceRunORM,
    *,
    status: str,
    resources_found: int | None = None,
    error: str | None = None,
) -> None:
    """Mark a run as done. status in {'success', 'failed', 'partial'}."""
    run.finished_at = datetime.now(tz=UTC)
    run.status = status
    if resources_found is not None:
        run.resources_found = resources_found
    if error is not None:
        run.error = error
    session.flush()


def latest_successful_run(
    session: Session,
    *,
    account_id: UUID,
    region: str,
    resource_type: str,
    source: str,
) -> SourceRunORM | None:
    """The most recent successful run for a (account, region, type, source).

    If None, no successful scan has proven this scope complete.
    """
    stmt = (
        select(SourceRunORM)
        .where(
            SourceRunORM.account_id == account_id,
            SourceRunORM.region == region,
            SourceRunORM.resource_type == resource_type,
            SourceRunORM.source == source,
            SourceRunORM.status == "success",
        )
        .order_by(SourceRunORM.finished_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def list_runs(
    session: Session,
    *,
    account_id: UUID | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[SourceRunORM]:
    stmt = select(SourceRunORM).order_by(SourceRunORM.started_at.desc())
    if account_id is not None:
        stmt = stmt.where(SourceRunORM.account_id == account_id)
    if status is not None:
        stmt = stmt.where(SourceRunORM.status == status)
    stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars())
