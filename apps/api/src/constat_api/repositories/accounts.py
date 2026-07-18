"""Accounts repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.orm import AccountORM
from constat_api.tenant import current_tenant


def get_by_external_id(session: Session, external_id: str) -> AccountORM | None:
    """Find an account by external_id, scoped to the session's tenant.

    external_id is unique per (tenant_id, external_id) only (migration
    0011, audit F-12), so an unscoped lookup could match another tenant's
    account. When the session has no tenant bound (shouldn't happen in
    the API; possible in tests), fall back to the unscoped lookup.
    """
    stmt = select(AccountORM).where(AccountORM.external_id == external_id)
    tenant_id = current_tenant(session)
    if tenant_id is not None:
        stmt = stmt.where(AccountORM.tenant_id == tenant_id)
    return session.execute(stmt).scalar_one_or_none()


def get_or_create(session: Session, external_id: str, name: str | None = None) -> AccountORM:
    """Find an account by external_id, or create it. The caller owns the transaction."""
    acc = get_by_external_id(session, external_id)
    if acc is not None:
        return acc
    acc = AccountORM(external_id=external_id, name=name or f"account-{external_id}")
    # New rows belong to the session's tenant when one is bound (the ORM
    # default is the V1 default tenant, which would fail RLS WITH CHECK
    # under any other tenant).
    tenant_id = current_tenant(session)
    if tenant_id is not None:
        acc.tenant_id = tenant_id
    session.add(acc)
    session.flush()
    return acc


def list_accounts(session: Session, *, limit: int = 100, offset: int = 0) -> list[AccountORM]:
    """List accounts, newest first. Used by /accounts (UX/ops P2 item 10)."""
    return list(
        session.execute(
            select(AccountORM).order_by(AccountORM.created_at.desc()).limit(limit).offset(offset)
        ).scalars()
    )
