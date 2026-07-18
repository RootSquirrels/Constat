"""Resources repository.

Natural key: (account_id, region, resource_type, native_id).
Upsert by that key: if exists, bump `last_seen_at`; if not, insert.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.orm import ResourceORM


def upsert_resource(
    session: Session,
    account_id: UUID,
    *,
    region: str,
    resource_type: str,
    native_id: str,
) -> ResourceORM:
    """Find by natural key or create. Bumps last_seen_at on update.

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
