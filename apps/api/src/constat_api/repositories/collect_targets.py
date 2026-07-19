"""CollectTarget repository: the persisted scan targets (roadmap 1.3).

One row per (tenant, AWS account). The CSV import upserts; the collect
endpoint reads the full rows (secrets included) when the request body
carries no explicit targets.

external_id is a shared secret (F-06): `list_targets` DEFERS the column
by default so a read path cannot leak it by accident — only the collect
path (`with_secrets=True`) selects it. Rotation = upsert with the new
value.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from constat_api.orm import CollectTargetORM
from constat_api.tenant import current_tenant


def upsert(
    session: Session,
    *,
    aws_account_id: str,
    role_arn: str,
    external_id: str,
    name: str | None = None,
    regions: list[str] | None = None,
    resource_types: list[str] | None = None,
) -> tuple[CollectTargetORM, bool]:
    """Insert or update the target for this AWS account.

    Returns (row, created): created=True on insert, False on update.
    Upsert semantics make the CSV import idempotent — re-importing the
    same file updates in place instead of duplicating (UNIQUE(tenant_id,
    aws_account_id) is the guard). The caller owns the transaction.

    NOTE: external_id rotates here — an upsert silently overwrites the
    old secret. That is deliberate (rotation = re-import), so callers
    must never treat "row exists" as "secret unchanged".
    """
    existing = get(session, aws_account_id)
    if existing is not None:
        existing.role_arn = role_arn
        existing.external_id = external_id
        existing.name = name
        existing.regions = regions
        existing.resource_types = resource_types
        existing.updated_at = datetime.now(tz=UTC)
        session.flush()
        return existing, False
    row = CollectTargetORM(
        aws_account_id=aws_account_id,
        role_arn=role_arn,
        external_id=external_id,
        name=name,
        regions=regions,
        resource_types=resource_types,
    )
    # New rows belong to the session's tenant when one is bound (same
    # discipline as the accounts repo — the ORM default would fail the
    # RLS WITH CHECK under any other tenant).
    tenant_id = current_tenant(session)
    if tenant_id is not None:
        row.tenant_id = tenant_id
    session.add(row)
    session.flush()
    return row, True


def get(session: Session, aws_account_id: str) -> CollectTargetORM | None:
    """Find a target by AWS account id, scoped to the session's tenant.

    Returns the FULL row (external_id included) — this is the collect
    path's lookup, not a read-API path.
    """
    stmt = select(CollectTargetORM).where(CollectTargetORM.aws_account_id == aws_account_id)
    tenant_id = current_tenant(session)
    if tenant_id is not None:
        stmt = stmt.where(CollectTargetORM.tenant_id == tenant_id)
    return session.execute(stmt).scalar_one_or_none()


def list_targets(session: Session, *, with_secrets: bool = False) -> list[CollectTargetORM]:
    """List all targets, ordered by AWS account id.

    with_secrets=False (the read-API default): external_id is deferred —
    NOT selected from the DB — so serializing the rows cannot leak the
    secret. with_secrets=True is for the collect path, which needs the
    secret to AssumeRole.
    """
    stmt = select(CollectTargetORM).order_by(CollectTargetORM.aws_account_id.asc())
    if not with_secrets:
        stmt = stmt.options(defer(CollectTargetORM.external_id))
    return list(session.execute(stmt).scalars())


def delete(session: Session, aws_account_id: str) -> bool:
    """Delete the target for this AWS account (offboarding). False if absent."""
    row = get(session, aws_account_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True
