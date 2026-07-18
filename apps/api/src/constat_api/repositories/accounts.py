"""Accounts repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.orm import AccountORM


def get_by_external_id(session: Session, external_id: str) -> AccountORM | None:
    return session.execute(
        select(AccountORM).where(AccountORM.external_id == external_id)
    ).scalar_one_or_none()


def get_or_create(session: Session, external_id: str, name: str | None = None) -> AccountORM:
    """Find an account by external_id, or create it. The caller owns the transaction."""
    acc = get_by_external_id(session, external_id)
    if acc is not None:
        return acc
    acc = AccountORM(external_id=external_id, name=name or f"account-{external_id}")
    session.add(acc)
    session.flush()
    return acc
