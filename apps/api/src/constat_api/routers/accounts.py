"""Accounts HTTP endpoints.

Lists the AWS accounts / FOCUS BillingAccountIds that have been
observed (via the AWS collector or the FOCUS ingestion path). Each
account is identified by its 12-digit AWS account ID. The list is
read-only and powers the /accounts page.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from constat_api.auth import verify_api_key
from constat_api.db import get_db
from constat_api.repositories import accounts as accounts_repo

router = APIRouter(
    prefix="/accounts",
    tags=["accounts"],
    dependencies=[Depends(verify_api_key)],
)


class AccountOut(BaseModel):
    id: str
    external_id: str
    name: str | None
    created_at: str  # ISO 8601


@router.get("", response_model=list[AccountOut])
def list_accounts_endpoint(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
) -> list[AccountOut]:
    rows = accounts_repo.list_accounts(session, limit=limit, offset=offset)
    return [
        AccountOut(
            id=str(r.id),
            external_id=r.external_id,
            name=r.name,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]
