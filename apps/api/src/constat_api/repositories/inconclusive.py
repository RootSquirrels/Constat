"""Inconclusive repository.

A 'we don't know' record. Distinct from insights (which are gaps we know about).

Roadmap 2.5: the queue carries operator workflow fields (owner, due_date,
status) written by PATCH /inconclusives/{id} only.
"""

from __future__ import annotations

from typing import Any
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
        owner=orm.owner,
        due_date=orm.due_date,
        status=orm.status,
    )


# Triage statuses of the inconclusive work queue (migration 0018 CHECK).
# Defined once so the router and tests share the truth source.
WORKFLOW_STATUSES: frozenset[str] = frozenset({"open", "acknowledged", "resolved"})

# Sort keys accepted by list_inconclusive. There is deliberately no
# "impact" sort: inconclusive records carry no amounts, so the honest
# orderings are by age (computed_at) or by rule (group the queue).
SORTS: frozenset[str] = frozenset({"computed_at", "rule_name"})


def list_inconclusive(
    session: Session,
    *,
    rule_name: str | None = None,
    account_id: UUID | None = None,
    status: str | None = None,
    sort: str = "computed_at",
    limit: int = 100,
    offset: int = 0,
) -> list[Inconclusive]:
    """List the queue. Default order: newest first (computed_at desc).

    sort='rule_name' groups by rule (asc), newest first inside each rule.
    The router validates `status`/`sort` before calling; the repo raises
    ValueError as defense in depth.
    """
    stmt = select(InconclusiveORM)
    if sort == "computed_at":
        stmt = stmt.order_by(InconclusiveORM.computed_at.desc())
    elif sort == "rule_name":
        stmt = stmt.order_by(InconclusiveORM.rule_name, InconclusiveORM.computed_at.desc())
    else:
        raise ValueError(f"invalid sort {sort!r}; must be one of {sorted(SORTS)}")
    if rule_name is not None:
        stmt = stmt.where(InconclusiveORM.rule_name == rule_name)
    if account_id is not None:
        stmt = stmt.where(InconclusiveORM.account_id == account_id)
    if status is not None:
        if status not in WORKFLOW_STATUSES:
            raise ValueError(
                f"invalid status {status!r}; must be one of {sorted(WORKFLOW_STATUSES)}"
            )
        stmt = stmt.where(InconclusiveORM.status == status)
    stmt = stmt.limit(limit).offset(offset)
    return [_orm_to_pydantic(row) for row in session.execute(stmt).scalars()]


def get_inconclusive(session: Session, inconclusive_id: UUID) -> Inconclusive | None:
    orm = session.get(InconclusiveORM, inconclusive_id)
    return _orm_to_pydantic(orm) if orm else None


def insert_inconclusive(session: Session, item: Inconclusive) -> Inconclusive:
    orm = InconclusiveORM(
        id=item.id or uuid4(),
        rule_name=item.rule_name,
        resource_id=item.resource_id,
        account_id=UUID(item.account_id) if item.account_id else None,
        missing_facts=item.missing_facts,
        reason=item.reason,
        computed_at=item.computed_at,
        owner=item.owner,
        due_date=item.due_date,
        status=item.status,
    )
    session.add(orm)
    session.flush()
    return _orm_to_pydantic(orm)


def update_workflow(
    session: Session, inconclusive_id: UUID, fields: dict[str, Any]
) -> Inconclusive | None:
    """Partial update of the workflow fields on one record.

    `fields` carries only the keys the caller explicitly provided
    (owner / due_date / status) — None is a real value here (it clears
    the field), so absence-vs-null is decided by the caller. Returns the
    updated record, or None when the id doesn't exist (or isn't visible
    to this tenant). The PATCH endpoint is the only writer.
    """
    allowed = {"owner", "due_date", "status"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown workflow fields: {sorted(unknown)}")
    if "status" in fields and fields["status"] not in WORKFLOW_STATUSES:
        raise ValueError(
            f"invalid status {fields['status']!r}; must be one of {sorted(WORKFLOW_STATUSES)}"
        )
    orm = session.get(InconclusiveORM, inconclusive_id)
    if orm is None:
        return None
    if "owner" in fields:
        orm.owner = fields["owner"]
    if "due_date" in fields:
        orm.due_date = fields["due_date"]
    if "status" in fields:
        orm.status = fields["status"]
    session.flush()
    return _orm_to_pydantic(orm)


def count_inconclusive(session: Session, *, rule_name: str | None = None) -> int:
    from sqlalchemy import func as sa_func

    stmt = select(sa_func.count(InconclusiveORM.id))
    if rule_name is not None:
        stmt = stmt.where(InconclusiveORM.rule_name == rule_name)
    return int(session.execute(stmt).scalar_one())


def delete_inconclusive_for_rule(session: Session, rule_name: str) -> int:
    """Delete all inconclusive records for a rule. Returns rows deleted.

    Audit F-03: the runner uses delete-and-replace semantics — each run
    starts by clearing the rule's previous records so re-runs don't
    accumulate duplicates. The caller owns the transaction.
    """
    from sqlalchemy import delete as sa_delete

    stmt = sa_delete(InconclusiveORM).where(InconclusiveORM.rule_name == rule_name)
    result = session.execute(stmt)
    return int(result.rowcount or 0)


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
