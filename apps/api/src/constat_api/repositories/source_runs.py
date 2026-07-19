"""SourceRun repository: lifecycle for scope-completeness tracking.

A SourceRun proves "I scanned account X, region Y, type Z with source S
and found N resources (or failed)". When status='success', absence of a
resource in that scope is PROVEN, not guessed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from constat_api.orm import SourceRunORM
from constat_api.tenant import tenant_or_default

# Default threshold for "stuck" run detection. A run is considered stuck
# if it's been in status='running' for longer than this. Two hours is
# generous: a healthy RDS scan across all default regions takes < 5 min.
DEFAULT_STUCK_RUN_THRESHOLD = timedelta(hours=2)


def start_run(
    session: Session,
    *,
    account_id: UUID,
    region: str,
    resource_type: str,
    source: str,
    force: bool = False,
    job_id: UUID | None = None,
) -> SourceRunORM | None:
    """Start a new run. Returns None if one is already active for this scope.

    Args:
        force: when True and a run is already active, mark it as 'failed'
            with an explanatory error and start a new one. Use this after
            `cleanup_stuck_runs` has failed to free the scope, or when
            you know the previous worker is dead (OOM, SIGKILL).
        job_id: the collect_jobs row this run belongs to (async collection,
            migration 0015). None for CLI / ad-hoc runs.

    The caller should handle None: either skip the scan (someone else is
    scanning) or treat it as a duplicate attempt.
    """
    if force:
        _abort_active_run(session, account_id, region, resource_type, source)

    run = SourceRunORM(
        id=uuid4(),
        tenant_id=tenant_or_default(session),
        account_id=account_id,
        region=region,
        resource_type=resource_type,
        source=source,
        status="running",
        job_id=job_id,
    )
    session.add(run)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    return run


def _abort_active_run(
    session: Session,
    account_id: UUID,
    region: str,
    resource_type: str,
    source: str,
) -> None:
    """Mark any active run in this scope as 'failed' so a new one can start.

    Idempotent: no-op if no active run.
    """
    stmt = select(SourceRunORM).where(
        SourceRunORM.account_id == account_id,
        SourceRunORM.region == region,
        SourceRunORM.resource_type == resource_type,
        SourceRunORM.source == source,
        SourceRunORM.status == "running",
    )
    active = session.execute(stmt).scalars().all()
    now = datetime.now(tz=UTC)
    for run in active:
        run.finished_at = now
        run.status = "failed"
        run.error = "aborted: superseded by force-start"
    if active:
        session.flush()


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


def cleanup_stuck_runs(
    session: Session,
    *,
    threshold: timedelta = DEFAULT_STUCK_RUN_THRESHOLD,
) -> int:
    """Mark runs in status='running' for longer than `threshold` as 'failed'.

    Returns the number of stuck runs cleaned up. Run this from a periodic
    job (cron / Fargate task / startup hook) to recover from worker crashes.

    Why: a worker that dies (OOM, SIGKILL, network partition mid-page) leaves
    its source_runs row stuck in status='running'. The partial unique index
    in migration 0005 then blocks all subsequent scans for that scope until
    manual intervention. This function is the safety net.
    """
    cutoff = datetime.now(tz=UTC) - threshold
    stmt = select(SourceRunORM).where(
        SourceRunORM.status == "running",
        SourceRunORM.started_at < cutoff,
    )
    stuck = session.execute(stmt).scalars().all()
    now = datetime.now(tz=UTC)
    for run in stuck:
        run.finished_at = now
        run.status = "failed"
        run.error = f"stuck_run_cleanup: started_at={run.started_at.isoformat()}"
    if stuck:
        session.commit()
    return len(stuck)


def _age_since(ts: datetime) -> timedelta:
    """Age of a timestamp relative to now, tolerant of naive datetimes.

    sqlite drops tzinfo on DateTime(timezone=True) columns, so a timestamp
    read back may be naive. Compare naive-with-naive in that case.
    """
    now = datetime.now(tz=UTC)
    if ts.tzinfo is None:
        return now.replace(tzinfo=None) - ts
    return now - ts


def latest_successful_run(
    session: Session,
    *,
    account_id: UUID,
    region: str,
    resource_type: str,
    source: str,
    max_age: timedelta | None = None,
) -> SourceRunORM | None:
    """The most recent successful run for a (account, region, type, source).

    If None, no successful scan has proven this scope complete.

    Args:
        max_age: freshness window (audit F-02). When set, a successful run
            older than max_age no longer proves the scope and None is
            returned. The age check happens in Python (not SQL) so the
            comparison is dialect-safe (sqlite returns naive datetimes).
            Default None keeps the historical "any success proves" behavior.
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
    run = session.execute(stmt).scalar_one_or_none()
    if run is None or max_age is None:
        return run
    if run.finished_at is None or _age_since(run.finished_at) > max_age:
        return None
    return run


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
