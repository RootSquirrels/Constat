"""Appeared/resolved insight history endpoint (roadmap 2.4).

Separate from routers/insights.py on purpose (parallel ownership): this
router carries only GET /insights/history, backed by the append-only
insight_events table. It is registered in main.py BEFORE the insights
router so "/history" wins over the "/{insight_id}" path parameter.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from constat_api.audit import get_audit_db, record_read
from constat_api.auth import Principal, verify_api_key
from constat_api.db import get_db
from constat_api.orm import InsightEventORM
from constat_api.repositories import insight_events as repo

router = APIRouter(
    prefix="/insights",
    tags=["insights"],
    dependencies=[Depends(verify_api_key)],
)


class InsightEventOut(BaseModel):
    """One appeared/resolved event. resource_id/account_id may be null
    (account-scoped rules, or the resource/run row is gone — history
    survives by design)."""

    id: UUID
    fingerprint: str
    rule_name: str
    resource_id: UUID | None
    account_id: str | None
    title: str
    event: str
    monthly_usd: float | None
    insight_run_id: UUID | None
    occurred_at: datetime


class InsightHistorySummary(BaseModel):
    """The "€ récupérés" seed: resolved_monthly_usd_total is the monthly
    run-rate of the gaps that closed in the filtered window."""

    appeared_count: int
    resolved_count: int
    resolved_monthly_usd_total: float


class InsightHistoryOut(BaseModel):
    events: list[InsightEventOut]
    summary: InsightHistorySummary


def _to_out(orm: InsightEventORM) -> InsightEventOut:
    return InsightEventOut(
        id=orm.id,
        fingerprint=orm.fingerprint,
        rule_name=orm.rule_name,
        resource_id=orm.resource_id,
        account_id=orm.account_id,
        title=orm.title,
        event=orm.event,
        monthly_usd=orm.monthly_usd,
        insight_run_id=orm.insight_run_id,
        occurred_at=orm.occurred_at,
    )


@router.get("/history", response_model=InsightHistoryOut)
def insight_history_endpoint(
    rule_name: str | None = Query(default=None),
    since: datetime | None = Query(
        default=None, description="Only events at or after this timestamp (ISO-8601)."
    ),
    event: str | None = Query(default=None, description="appeared | resolved"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
    principal: Principal = Depends(verify_api_key),
    audit_session: Session = Depends(get_audit_db),
) -> InsightHistoryOut:
    """List appeared/resolved events (newest first) + a summary.

    The summary is computed over the WHOLE filtered set, not the page:
    resolved_monthly_usd_total is the headline "money recovered" figure,
    the events list is the proof behind it.
    """
    if event is not None and event not in repo.EVENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid event {event!r}; must be one of {sorted(repo.EVENTS)}",
        )
    events = repo.list_events(
        session, rule_name=rule_name, since=since, event=event, limit=limit, offset=offset
    )
    summary = repo.summarize_events(session, rule_name=rule_name, since=since, event=event)
    record_read(
        audit_session,
        actor=principal.name,
        target_type="insight_events",
        route="/insights/history",
        filters={
            "rule_name": rule_name is not None,
            "since": since is not None,
            "event": event is not None,
        },
        row_count=len(events),
    )
    return InsightHistoryOut(
        events=[_to_out(e) for e in events],
        summary=InsightHistorySummary(**summary),
    )
