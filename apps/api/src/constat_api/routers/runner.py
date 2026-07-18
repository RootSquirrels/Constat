"""Insight runner HTTP endpoint.

Triggers the same path as the CLI but in-process. V1: synchronous call
(blocks for the duration of the scan). V2: queue + background worker.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from constat_api.db import get_db
from constat_api.insights.runner import run_rds_eol

router = APIRouter(prefix="/insights", tags=["insights-runner"])


class RunRequest(BaseModel):
    rule: str = "rds_eol"


class RunResultOut(BaseModel):
    rule_name: str
    resources_scanned: int
    insights_emitted: int
    inconclusive_emitted: int
    errors: list[str]


@router.post("/run", response_model=RunResultOut)
def run_insights_endpoint(
    body: RunRequest,
    today: date | None = Query(
        default=None, description="Override 'today' for deterministic EOL/pricing calc (ISO date)."
    ),
    session: Session = Depends(get_db),
) -> RunResultOut:
    if body.rule != "rds_eol":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown rule: {body.rule} (V1 supports: rds_eol)",
        )
    result = run_rds_eol(session, today=today)
    return RunResultOut(
        rule_name=result.rule_name,
        resources_scanned=result.resources_scanned,
        insights_emitted=result.insights_emitted,
        inconclusive_emitted=result.inconclusive_emitted,
        errors=result.errors,
    )
