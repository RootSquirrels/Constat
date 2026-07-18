"""Insights repository."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from constat_core.models import Insight, Severity
from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.orm import InsightORM


def _orm_to_pydantic(orm: InsightORM) -> Insight:
    return Insight(
        id=orm.id,
        rule_name=orm.rule_name,
        resource_id=orm.resource_id,
        account_id=str(orm.account_id) if orm.account_id else None,
        severity=Severity(orm.severity),
        title=orm.title,
        payload=orm.payload,
        computed_at=orm.computed_at,
        ack_status=orm.ack_status,
        ack_at=orm.ack_at,
        ack_by=orm.ack_by,
    )


def list_insights(
    session: Session,
    *,
    rule_name: str | None = None,
    severity: Severity | None = None,
    account_id: UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Insight]:
    """List current insights, newest first. Filters are optional."""
    stmt = select(InsightORM).order_by(InsightORM.computed_at.desc())
    if rule_name is not None:
        stmt = stmt.where(InsightORM.rule_name == rule_name)
    if severity is not None:
        stmt = stmt.where(InsightORM.severity == severity.value)
    if account_id is not None:
        stmt = stmt.where(InsightORM.account_id == account_id)
    stmt = stmt.limit(limit).offset(offset)
    return [_orm_to_pydantic(row) for row in session.execute(stmt).scalars()]


def get_insight(session: Session, insight_id: UUID) -> Insight | None:
    orm = session.get(InsightORM, insight_id)
    return _orm_to_pydantic(orm) if orm else None


def insert_insight(session: Session, insight: Insight) -> Insight:
    """Insert one insight. The caller owns the transaction."""
    orm = InsightORM(
        id=insight.id or uuid4(),
        rule_name=insight.rule_name,
        resource_id=insight.resource_id,
        account_id=UUID(insight.account_id) if insight.account_id else None,
        severity=insight.severity.value,
        title=insight.title,
        payload=insight.payload,
        computed_at=insight.computed_at,
        # ack_* fields are operator-driven, never set on insert.
        ack_status=None,
        ack_at=None,
        ack_by=None,
    )
    session.add(orm)
    session.flush()
    return _orm_to_pydantic(orm)


def delete_insights_for_rule(session: Session, rule_name: str) -> int:
    """Delete all insights for a rule. Returns the number of rows deleted.

    Audit F-03: the runner uses delete-and-replace semantics — each run
    starts by clearing the rule's previous insights so re-runs don't
    accumulate duplicates. The caller owns the transaction.
    """
    from sqlalchemy import delete as sa_delete

    stmt = sa_delete(InsightORM).where(InsightORM.rule_name == rule_name)
    result = session.execute(stmt)
    return int(result.rowcount or 0)


def count_insights(session: Session, *, rule_name: str | None = None) -> int:
    from sqlalchemy import func as sa_func

    stmt = select(sa_func.count(InsightORM.id))
    if rule_name is not None:
        stmt = stmt.where(InsightORM.rule_name == rule_name)
    return int(session.execute(stmt).scalar_one())


# Acknowledged ack_status values. Defined once so the router and
# tests share the truth source. Pydantic-side validation is in the
# PATCH body model; this list is the canonical set.
ACK_STATUSES: frozenset[str] = frozenset(
    {"acknowledged", "in_progress", "resolved", "dismissed"}
)


def update_ack(
    session: Session,
    insight_id: UUID,
    *,
    ack_status: str,
    ack_by: str | None = None,
    ack_at: datetime | None = None,
) -> Insight | None:
    """Update the operator-acknowledgment fields on one insight.

    Server-set semantics: `ack_at` is set to `datetime.now(UTC)` if
    the caller doesn't pass it. The caller is the PATCH endpoint,
    which is the only path that should ever write these fields.

    Returns the updated Insight, or None if the insight doesn't exist
    (the row is not visible across tenants, so None also covers
    "wrong tenant").
    """
    from datetime import UTC, datetime

    from constat_api.orm import InsightORM

    if ack_status not in ACK_STATUSES:
        raise ValueError(
            f"invalid ack_status {ack_status!r}; must be one of {sorted(ACK_STATUSES)}"
        )

    orm = session.get(InsightORM, insight_id)
    if orm is None:
        return None

    orm.ack_status = ack_status
    orm.ack_by = ack_by
    orm.ack_at = ack_at if ack_at is not None else datetime.now(tz=UTC)
    session.flush()
    return _orm_to_pydantic(orm)
