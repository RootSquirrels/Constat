"""Insights HTTP endpoints."""

from __future__ import annotations

from uuid import UUID

from constat_core.models import Insight, Severity
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from constat_api.auth import _get_settings, verify_api_key
from constat_api.db import get_db
from constat_api.repositories import insights as repo
from constat_api.settings import Settings

router = APIRouter(
    prefix="/insights",
    tags=["insights"],
    dependencies=[Depends(verify_api_key)],
)


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
def create_insight_endpoint(
    insight: Insight,
    session: Session = Depends(get_db),
    cfg: Settings = Depends(_get_settings),
) -> Insight:
    """Manual insight insertion — tests and local demos only (F-10).

    Any API-key holder could otherwise forge an insight without
    provenance, so this is gated behind CONSTAT_ENABLE_MANUAL_INSIGHTS
    (default off). Real insights are written by the rule runner. When
    enabled, the payload is stamped source="manual" so these rows stay
    distinguishable from rule-produced ones.
    """
    if not cfg.enable_manual_insights:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="manual insight creation is disabled "
            "(set CONSTAT_ENABLE_MANUAL_INSIGHTS=1 to enable)",
        )
    insight.payload = {**insight.payload, "source": "manual"}
    return repo.insert_insight(session, insight)
