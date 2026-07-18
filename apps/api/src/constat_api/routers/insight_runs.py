"""Insight run history endpoint.

Lists past insight_runs for audit ('who ran what when'). Filter by rule
and status. Returns the most recent N runs.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.db import get_db
from constat_api.orm import InsightRunORM

router = APIRouter(prefix="/insight-runs", tags=["insight-runs"])


class InsightRunOut(BaseModel):
    id: str
    rule_name: str
    status: str
    started_at: str  # ISO 8601
    finished_at: str | None
    resources_scanned: int | None
    insights_emitted: int | None
    error: str | None


@router.get("", response_model=list[InsightRunOut])
def list_insight_runs(
    rule_name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_db),
) -> list[InsightRunOut]:
    stmt = select(InsightRunORM).order_by(InsightRunORM.started_at.desc())
    if rule_name is not None:
        stmt = stmt.where(InsightRunORM.rule_name == rule_name)
    if status is not None:
        stmt = stmt.where(InsightRunORM.status == status)
    stmt = stmt.limit(limit)

    rows = session.execute(stmt).scalars().all()
    return [
        InsightRunOut(
            id=str(r.id),
            rule_name=r.rule_name,
            status=r.status,
            started_at=r.started_at.isoformat() if r.started_at else "",
            finished_at=r.finished_at.isoformat() if r.finished_at else None,
            resources_scanned=r.resources_scanned,
            insights_emitted=r.insights_emitted,
            error=r.error,
        )
        for r in rows
    ]
