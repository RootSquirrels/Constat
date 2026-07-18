"""Inconclusive HTTP endpoints.

Returns the 'we don't know' records. Parallel to /insights: a complete
picture of fleet coverage requires both endpoints.
"""

from __future__ import annotations

from uuid import UUID

from constat_core.models import Inconclusive
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from constat_api.db import get_db
from constat_api.repositories import inconclusive as repo

router = APIRouter(prefix="/inconclusives", tags=["inconclusive"])


@router.get("", response_model=list[Inconclusive])
def list_inconclusive_endpoint(
    rule_name: str | None = Query(default=None),
    account_id: UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
) -> list[Inconclusive]:
    return repo.list_inconclusive(
        session,
        rule_name=rule_name,
        account_id=account_id,
        limit=limit,
        offset=offset,
    )


@router.get("/{inconclusive_id}", response_model=Inconclusive)
def get_inconclusive_endpoint(
    inconclusive_id: UUID, session: Session = Depends(get_db)
) -> Inconclusive:
    """O(1) lookup via repo.get_inconclusive. Replaces the previous small-N scan."""
    item = repo.get_inconclusive(session, inconclusive_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="inconclusive not found")
    return item


@router.post("", response_model=Inconclusive, status_code=status.HTTP_201_CREATED)
def create_inconclusive_endpoint(
    item: Inconclusive, session: Session = Depends(get_db)
) -> Inconclusive:
    """Insert one inconclusive. Used by tests + ingestion workers."""
    return repo.insert_inconclusive(session, item)
