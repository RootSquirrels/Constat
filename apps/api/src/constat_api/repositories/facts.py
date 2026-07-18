"""Facts repository.

For V1 we just append. Upsert by (resource_id, namespace, key) — "current fact"
— is a follow-up when the write pattern is clear.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from constat_core.models import Fact, ValueState
from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.orm import FactORM


def _orm_to_pydantic(orm: FactORM) -> Fact:
    return Fact(
        id=orm.id,
        resource_id=orm.resource_id,
        account_id=str(orm.account_id) if orm.account_id else None,
        namespace=orm.namespace,
        key=orm.key,
        value=orm.value,
        value_state=ValueState(orm.value_state),
        source=orm.source,
        observed_at=orm.observed_at,
        computed_at=orm.computed_at,
    )


def list_facts_for_resource(
    session: Session, resource_id: UUID, *, observed_at: datetime | None = None
) -> list[Fact]:
    """List facts for one resource. If observed_at is given, only the latest <= that point."""
    stmt = select(FactORM).where(FactORM.resource_id == resource_id)
    if observed_at is not None:
        stmt = stmt.where(FactORM.observed_at <= observed_at)
    stmt = stmt.order_by(FactORM.namespace, FactORM.key, FactORM.observed_at.desc())
    return [_orm_to_pydantic(row) for row in session.execute(stmt).scalars()]


def insert_facts(session: Session, facts: list[Fact]) -> int:
    """Bulk-insert facts. Returns the number of rows inserted."""
    orm_rows = [
        FactORM(
            id=f.id or uuid4(),
            resource_id=f.resource_id,
            account_id=UUID(f.account_id) if f.account_id else None,
            namespace=f.namespace,
            key=f.key,
            value=f.value,
            value_state=f.value_state.value,
            source=f.source,
            observed_at=f.observed_at,
        )
        for f in facts
    ]
    session.add_all(orm_rows)
    session.flush()
    return len(orm_rows)
