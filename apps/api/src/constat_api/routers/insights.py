"""Insights HTTP endpoints."""

from __future__ import annotations

from uuid import UUID

from constat_core.models import Insight, Severity
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from constat_api.db import get_db
from constat_api.repositories import insights as repo

router = APIRouter(prefix="/insights", tags=["insights"])


@router.get("", response_model=list[Insight])
def list_insights_endpoint(
    rule_name: str | None = Query(default=None),
    severity: Severity | None = Query(default=None),
    account_id: UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
) -> list[Insight]:
    return repo.list_insights(
        session,
        rule_name=rule_name,
        severity=severity,
        account_id=account_id,
        limit=limit,
        offset=offset,
    )


@router.get("/{insight_id}", response_model=Insight)
def get_insight_endpoint(insight_id: UUID, session: Session = Depends(get_db)) -> Insight:
    insight = repo.get_insight(session, insight_id)
    if insight is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="insight not found")
    return insight


@router.post("", response_model=Insight, status_code=status.HTTP_201_CREATED)
def create_insight_endpoint(insight: Insight, session: Session = Depends(get_db)) -> Insight:
    """Insert one insight. Used by tests + ingestion workers; not for public UI yet."""
    return repo.insert_insight(session, insight)
