"""Status / health snapshot endpoint.

The DAF / ops / pilot-customer entry-point: "how are we doing right
now?" One GET, one JSON, ~10 fields. Renders the /status page in the
web app and is the source of truth for any "what's our coverage?"
question.

Counts come from a single set of COUNT() queries; no scanning of
payloads. The latency is bounded by the index count, not the row
count.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from constat_api.audit import get_audit_db, record_read
from constat_api.auth import Principal, verify_api_key
from constat_api.db import get_db
from constat_api.orm import (
    AccountORM,
    InconclusiveORM,
    InsightORM,
    InsightRunORM,
    ResourceORM,
    SourceRunORM,
)

router = APIRouter(
    prefix="/status",
    tags=["status"],
    dependencies=[Depends(verify_api_key)],
)


class SeverityBreakdown(BaseModel):
    critical: int
    warning: int
    info: int


class LastRun(BaseModel):
    rule_name: str
    started_at: str  # ISO 8601
    finished_at: str | None
    status: str
    resources_scanned: int | None
    insights_emitted: int | None


class LastSourceRun(BaseModel):
    account_external_id: str | None
    region: str
    resource_type: str
    finished_at: str | None
    status: str
    resources_found: int | None


class StatusResponse(BaseModel):
    generated_at: str  # ISO 8601
    accounts: int
    resources_total: int
    resources_active: int
    insights_total: int
    insights_by_severity: SeverityBreakdown
    inconclusive_total: int
    last_insight_run: LastRun | None
    last_source_run: LastSourceRun | None
    source_run_freshness_seconds: int | None  # age of the most recent source_run


@router.get("", response_model=StatusResponse)
def get_status(
    session: Session = Depends(get_db),
    principal: Principal = Depends(verify_api_key),
    audit_session: Session = Depends(get_audit_db),
) -> StatusResponse:
    """Aggregate fleet-wide counts and the most recent runs.

    Latency: ~10ms on the pilot volume. Each count is one index scan.
    If this ever becomes slow, cache the response for 30s — the DAF
    does not need second-precision.
    """
    now = datetime.now(tz=UTC)

    # Counts.
    accounts = session.execute(select(func.count(AccountORM.id))).scalar_one()
    resources_total = session.execute(select(func.count(ResourceORM.id))).scalar_one()
    resources_active = session.execute(
        select(func.count(ResourceORM.id)).where(ResourceORM.retired_at.is_(None))
    ).scalar_one()
    insights_total = session.execute(select(func.count(InsightORM.id))).scalar_one()
    inconclusive_total = session.execute(select(func.count(InconclusiveORM.id))).scalar_one()

    # Severity breakdown for insights. One scan, three filter counts.
    severity_rows = session.execute(
        select(InsightORM.severity, func.count(InsightORM.id)).group_by(InsightORM.severity)
    ).all()
    by_sev: dict[str, int] = {s: 0 for s in ("critical", "warning", "info")}
    for sev, count in severity_rows:
        by_sev[sev] = count

    # Last insight run (any rule).
    last_run_row = session.execute(
        select(InsightRunORM).order_by(InsightRunORM.started_at.desc()).limit(1)
    ).scalar_one_or_none()

    # Last source_run across the fleet.
    last_sr_row = session.execute(
        select(SourceRunORM, AccountORM.external_id)
        .join(AccountORM, AccountORM.id == SourceRunORM.account_id)
        .order_by(SourceRunORM.started_at.desc())
        .limit(1)
    ).first()

    # Source-run freshness: how old is the most recent successful or
    # failed scan? null when we have never scanned anything (pilot day 1).
    freshness_seconds: int | None = None
    last_source_run: LastSourceRun | None = None
    if last_sr_row is not None:
        last_sr, ext = last_sr_row
        if last_sr.finished_at is not None:
            freshness_seconds = int((now - last_sr.finished_at).total_seconds())
        last_source_run = LastSourceRun(
            account_external_id=ext,
            region=last_sr.region,
            resource_type=last_sr.resource_type,
            finished_at=last_sr.finished_at.isoformat() if last_sr.finished_at else None,
            status=last_sr.status,
            resources_found=last_sr.resources_found,
        )

    # Read attribution (CISO 3.3): /status is the restitution view's
    # data source — fleet-wide counts in one payload.
    record_read(
        audit_session,
        actor=principal.audit_actor,
        tenant_id=principal.tenant_id,
        target_type="status",
        route="/status",
        row_count=1,
    )

    return StatusResponse(
        generated_at=now.isoformat(),
        accounts=accounts,
        resources_total=resources_total,
        resources_active=resources_active,
        insights_total=insights_total,
        insights_by_severity=SeverityBreakdown(**by_sev),
        inconclusive_total=inconclusive_total,
        last_insight_run=(
            LastRun(
                rule_name=last_run_row.rule_name,
                started_at=last_run_row.started_at.isoformat() if last_run_row.started_at else "",
                finished_at=last_run_row.finished_at.isoformat()
                if last_run_row.finished_at
                else None,
                status=last_run_row.status,
                resources_scanned=last_run_row.resources_scanned,
                insights_emitted=last_run_row.insights_emitted,
            )
            if last_run_row is not None
            else None
        ),
        last_source_run=last_source_run,
        source_run_freshness_seconds=freshness_seconds,
    )
