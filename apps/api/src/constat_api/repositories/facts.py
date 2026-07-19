"""Facts repository.

V1 design: facts is a current-state table (one row per natural key).
observed_at is the timestamp of the most recent observation, NOT part of the key.
last_source_run_id chains the fact to the run that observed it last, so the
runner can verify scope-completeness.

For history, use `observations` (raw payloads, append-only) and `source_runs`
(when did we scan). If we ever need per-fact history, add `fact_history` then.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from constat_core.models import Fact, ValueState
from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.orm import FactORM
from constat_api.tenant import tenant_or_default


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
    """List current facts for one resource. If observed_at is given, only the latest <= that point."""
    stmt = select(FactORM).where(FactORM.resource_id == resource_id)
    if observed_at is not None:
        stmt = stmt.where(FactORM.observed_at <= observed_at)
    stmt = stmt.order_by(FactORM.namespace, FactORM.key, FactORM.observed_at.desc())
    return [_orm_to_pydantic(row) for row in session.execute(stmt).scalars()]


def list_facts_for_resources(session: Session, resource_ids: list[UUID]) -> list[Fact]:
    """List current facts for many resources in one query (audit F-16).

    Replaces the per-resource N+1 pattern in the runner: fetch once,
    group by resource_id in memory at the call site.
    """
    if not resource_ids:
        return []
    stmt = (
        select(FactORM)
        .where(FactORM.resource_id.in_(resource_ids))
        .order_by(FactORM.namespace, FactORM.key, FactORM.observed_at.desc())
    )
    return [_orm_to_pydantic(row) for row in session.execute(stmt).scalars()]


def list_facts_for_account(session: Session, account_id: UUID) -> list[Fact]:
    """List current facts scoped to an account (no resource_id)."""
    stmt = (
        select(FactORM)
        .where(FactORM.account_id == account_id, FactORM.resource_id.is_(None))
        .order_by(FactORM.namespace, FactORM.key)
    )
    return [_orm_to_pydantic(row) for row in session.execute(stmt).scalars()]


def insert_facts(session: Session, facts: list[Fact]) -> int:
    """Bulk-insert facts. Caller is responsible for uniqueness (use upsert_facts
    in production code paths). Returns the number of rows inserted."""
    # Stamped once per batch, not per row (RLS WITH CHECK rejects the ORM
    # default under a non-default tenant).
    tenant_id = tenant_or_default(session)
    orm_rows = [
        FactORM(
            id=f.id or uuid4(),
            tenant_id=tenant_id,
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


def upsert_facts(
    session: Session,
    facts: list[Fact],
    *,
    source_run_id: UUID | None = None,
) -> tuple[int, int]:
    """Insert or update facts by natural key (tenant, resource, namespace, key, source).

    On update: bumps value, value_state, observed_at, last_source_run_id.
    On insert: sets last_source_run_id.

    Returns (inserted, updated) counts.
    """
    inserted = 0
    updated = 0
    # Stamped once per batch, not per row (RLS WITH CHECK rejects the ORM
    # default under a non-default tenant).
    tenant_id = tenant_or_default(session)

    for f in facts:
        existing = session.execute(
            select(FactORM).where(
                FactORM.resource_id == f.resource_id,
                FactORM.namespace == f.namespace,
                FactORM.key == f.key,
                FactORM.source == f.source,
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.value = f.value
            existing.value_state = f.value_state.value
            existing.observed_at = f.observed_at
            existing.last_source_run_id = source_run_id
            updated += 1
        else:
            session.add(
                FactORM(
                    id=f.id or uuid4(),
                    tenant_id=tenant_id,
                    resource_id=f.resource_id,
                    account_id=UUID(f.account_id) if f.account_id else None,
                    namespace=f.namespace,
                    key=f.key,
                    value=f.value,
                    value_state=f.value_state.value,
                    source=f.source,
                    observed_at=f.observed_at,
                    last_source_run_id=source_run_id,
                )
            )
            inserted += 1

    session.flush()
    return inserted, updated
