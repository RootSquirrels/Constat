"""Inconclusive repository.

A 'we don't know' record. Distinct from insights (which are gaps we know about).
"""

from __future__ import annotations

from uuid import UUID, uuid4

from constat_core.models import Inconclusive
from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.orm import InconclusiveORM


def _orm_to_pydantic(orm: InconclusiveORM) -> Inconclusive:
    return Inconclusive(
        id=orm.id,
        rule_name=orm.rule_name,
        resource_id=orm.resource_id,
        account_id=str(orm.account_id) if orm.account_id else None,
        missing_facts=orm.missing_facts,
        reason=orm.reason,
        computed_at=orm.computed_at,
    )


def list_inconclusive(
    session: Session,
    *,
    rule_name: str | None = None,
    account_id: UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Inconclusive]:
    stmt = select(InconclusiveORM).order_by(InconclusiveORM.computed_at.desc())
    if rule_name is not None:
        stmt = stmt.where(InconclusiveORM.rule_name == rule_name)
    if account_id is not None:
        stmt = stmt.where(InconclusiveORM.account_id == account_id)
    stmt = stmt.limit(limit).offset(offset)
    return [_orm_to_pydantic(row) for row in session.execute(stmt).scalars()]


def insert_inconclusive(session: Session, item: Inconclusive) -> Inconclusive:
    orm = InconclusiveORM(
        id=item.id or uuid4(),
        rule_name=item.rule_name,
        resource_id=item.resource_id,
        account_id=UUID(item.account_id) if item.account_id else None,
        missing_facts=item.missing_facts,
        reason=item.reason,
        computed_at=item.computed_at,
    )
    session.add(orm)
    session.flush()
    return _orm_to_pydantic(orm)


def count_inconclusive(session: Session, *, rule_name: str | None = None) -> int:
    from sqlalchemy import func as sa_func

    stmt = select(sa_func.count(InconclusiveORM.id))
    if rule_name is not None:
        stmt = stmt.where(InconclusiveORM.rule_name == rule_name)
    return int(session.execute(stmt).scalar_one())


def delete_older_than(session: Session, *, older_than_days: int) -> int:
    """Delete inconclusive records older than N days.

    UX/ops P2 item 8: the inconclusive table grows without bound. A
    "missing fact" listed 6 months ago is no longer actionable. Schedule
    this from cron / k8s CronJob / Task Scheduler (see the ops doc).

    Returns the number of rows deleted. The caller owns the transaction.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import delete as sa_delete

    if older_than_days < 0:
        raise ValueError(f"older_than_days must be >= 0, got {older_than_days}")

    cutoff = datetime.now(tz=UTC) - timedelta(days=older_than_days)
    stmt = sa_delete(InconclusiveORM).where(InconclusiveORM.computed_at < cutoff)
    result = session.execute(stmt)
    return int(result.rowcount or 0)
