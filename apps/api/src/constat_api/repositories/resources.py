"""Resources repository.

Natural key: (account_id, region, resource_type, native_id).
Upsert by that key: if exists, bump `last_seen_at`; if not, insert.

Retirement: `retired_at` is set when a successful scan proves the
resource is gone (see `retire_stale_resources`). Until then, the
resource is "active" (retired_at IS NULL).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.orm import ResourceORM
from constat_api.repositories import source_runs as source_runs_repo


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


def retire_stale_resources(
    session: Session,
    *,
    account_id: UUID,
    region: str,
    resource_type: str,
    source: str,
) -> int:
    """Mark resources in scope as retired when a successful scan proved they're gone.

    Returns the number of resources newly retired. Call this AFTER a
    successful scan in the same scope: the latest successful run is the
    proof that this scope was scanned. Any active resource in the scope
    with `last_seen_at < latest_run.finished_at` was not seen in the
    latest run -> it's gone (or moved) -> retire it.

    This is the only path that sets `retired_at`. Without it, the
    GTM promise "we never claim a resource is alive without proof" is
    unkept: a DB deleted last week would still appear active.

    Idempotent: re-running on the same scope after a successful scan
    retires 0 additional rows.
    """
    run = source_runs_repo.latest_successful_run(
        session,
        account_id=account_id,
        region=region,
        resource_type=resource_type,
        source=source,
    )
    if run is None or run.started_at is None:
        # No proof the scope was ever complete. Don't retire anything
        # (the runner will emit INCONCLUSIVE for these resources).
        return 0

    # We compare against run.started_at, not run.finished_at. Reason:
    # start_run is called BEFORE the per-resource upsert_resource calls,
    # so run.started_at < any resource's last_seen_at when it was just
    # seen in this run. The "stale" condition is therefore
    # `last_seen_at < run.started_at`: the resource's last observation
    # predates this run, so it wasn't seen.
    stmt = select(ResourceORM).where(
        ResourceORM.account_id == account_id,
        ResourceORM.region == region,
        ResourceORM.resource_type == resource_type,
        ResourceORM.retired_at.is_(None),
        ResourceORM.last_seen_at < run.started_at,
    )
    stale = session.execute(stmt).scalars().all()
    now = datetime.now(tz=UTC)
    for r in stale:
        r.retired_at = now
    if stale:
        session.flush()
    return len(stale)
