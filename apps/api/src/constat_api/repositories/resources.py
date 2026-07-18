"""Resources repository.

Natural key: (account_id, region, resource_type, native_id).
Upsert by that key: if exists, bump `last_seen_at`; if not, insert.

Retirement: `retired_at` is set when TWO consecutive successful scans
both missed the resource (see `retire_stale_resources`, F-08). Until
then, the resource is "active" (retired_at IS NULL).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.orm import ResourceORM, SourceRunORM

# F-08: a resource is retired only after this many CONSECUTIVE successful
# scans in the same scope both missed it. One scan is not proof of
# deletion: a transient collection gap (partial page, throttled call)
# would otherwise "delete" live resources — the F-01 failure mode.
CONSECUTIVE_SCANS_FOR_RETIREMENT = 2


def upsert_resource(
    session: Session,
    account_id: UUID,
    *,
    region: str,
    resource_type: str,
    native_id: str,
) -> ResourceORM:
    """Find by natural key or create. Bumps last_seen_at on update.

    Handles the "came back from the dead" case: if a row exists for this
    natural key but is retired (retired_at IS NOT NULL), the resource
    reappeared in the latest scan. We resurrect it (clear retired_at,
    bump last_seen_at) instead of creating a duplicate.

    `first_seen_at` is set once on creation; subsequent calls preserve it.
    """
    now = datetime.now(tz=UTC)
    existing = session.execute(
        select(ResourceORM).where(
            ResourceORM.account_id == account_id,
            ResourceORM.region == region,
            ResourceORM.resource_type == resource_type,
            ResourceORM.native_id == native_id,
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Resurrection: clear retired_at, bump last_seen_at. We don't
        # reset first_seen_at — the resource was first seen on the
        # original date, that's the historical truth.
        if existing.retired_at is not None:
            existing.retired_at = None
        existing.last_seen_at = now
        return existing

    new = ResourceORM(
        account_id=account_id,
        region=region,
        resource_type=resource_type,
        native_id=native_id,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(new)
    session.flush()
    return new


def get_resource(session: Session, resource_id: UUID) -> ResourceORM | None:
    return session.get(ResourceORM, resource_id)


def count_resources(session: Session, account_id: UUID | None = None) -> int:
    from sqlalchemy import func as sa_func

    stmt = select(sa_func.count(ResourceORM.id))
    if account_id is not None:
        stmt = stmt.where(ResourceORM.account_id == account_id)
    return int(session.execute(stmt).scalar_one())


def _recent_successful_runs(
    session: Session,
    *,
    account_id: UUID,
    region: str,
    resource_type: str,
    source: str,
    limit: int,
) -> list[SourceRunORM]:
    """The `limit` most recent successful runs for the scope, newest first.

    Lives here (not in repositories/source_runs.py) because retirement is
    its only consumer; ordered by started_at so "the two latest scans"
    means what an operator would expect.
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
        .order_by(SourceRunORM.started_at.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def retire_stale_resources(
    session: Session,
    *,
    account_id: UUID,
    region: str,
    resource_type: str,
    source: str,
) -> int:
    """Mark resources in scope as retired when TWO consecutive successful
    scans both proved they're gone.

    Returns the number of resources newly retired. Call this AFTER a
    successful scan in the same scope. We look up the two most recent
    successful runs; a resource is retired only when its `last_seen_at`
    predates the started_at of BOTH runs — i.e. it was unseen in the two
    latest complete scans (F-08). If fewer than 2 successful runs exist
    for the scope, nothing is retired: one scan is not proof of deletion.

    This is the only path that sets `retired_at`. Without it, the
    GTM promise "we never claim a resource is alive without proof" is
    unkept: a DB deleted last week would still appear active.

    Idempotent: re-running on the same scope after a successful scan
    retires 0 additional rows.
    """
    runs = _recent_successful_runs(
        session,
        account_id=account_id,
        region=region,
        resource_type=resource_type,
        source=source,
        limit=CONSECUTIVE_SCANS_FOR_RETIREMENT,
    )
    if len(runs) < CONSECUTIVE_SCANS_FOR_RETIREMENT or any(r.started_at is None for r in runs):
        # Not enough proof the scope was scanned completely, twice. Don't
        # retire anything (the runner will emit INCONCLUSIVE for these
        # resources).
        return 0

    # Unseen in BOTH runs <=> last_seen_at < min(started_at of the two).
    # runs is newest-first, so the oldest of the two is the last element.
    #
    # We compare against run.started_at, not run.finished_at. Reason:
    # start_run is called BEFORE the per-resource upsert_resource calls,
    # so run.started_at < any resource's last_seen_at when it was just
    # seen in that run. The "stale" condition is therefore
    # `last_seen_at < oldest started_at`: the resource's last observation
    # predates both runs, so neither saw it.
    oldest_started_at = runs[-1].started_at
    stmt = select(ResourceORM).where(
        ResourceORM.account_id == account_id,
        ResourceORM.region == region,
        ResourceORM.resource_type == resource_type,
        ResourceORM.retired_at.is_(None),
        ResourceORM.last_seen_at < oldest_started_at,
    )
    stale = session.execute(stmt).scalars().all()
    now = datetime.now(tz=UTC)
    for r in stale:
        r.retired_at = now
    if stale:
        session.flush()
    return len(stale)
