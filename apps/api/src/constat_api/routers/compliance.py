"""Security & compliance HTTP endpoints (V1, migration 0010).

Three new surfaces for the DORA / ISO 27001 questionnaire:

- GET /audit-events: the append-only "who did what when" log
- GET /pii-classifications: per-field sensitivity labels
- GET /retention-policies: current retention configuration
- POST /retention/run: manual trigger of the retention job

All require auth (P0#1 X-API-Key). The /audit-events and
/retention-policies endpoints are what the security team will
point to when answering "show me your access log" and "what's
your data retention policy?".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.auth import verify_api_key
from constat_api.db import get_db
from constat_api.orm import (
    AuditEventORM,
    PIIClassificationORM,
    RetentionPolicyORM,
)
from constat_api.retention import apply_all_enabled, seed_default_policies

router = APIRouter(
    prefix="/compliance",
    tags=["compliance"],
    dependencies=[Depends(verify_api_key)],
)


# ---------------------------------------------------------------------------
# /audit-events
# ---------------------------------------------------------------------------


class AuditEventOut(BaseModel):
    id: str
    occurred_at: str  # ISO 8601
    actor: str
    action: str
    target_type: str | None
    target_id: str | None
    metadata: dict[str, Any]


@router.get("/audit-events", response_model=list[AuditEventOut])
def list_audit_events(
    actor: str | None = Query(
        default=None, description="Filter by actor (e.g. 'system:retention')"
    ),
    action: str | None = Query(
        default=None, description="Filter by action (e.g. 'aws_scan_completed')"
    ),
    since: datetime | None = Query(
        default=None, description="Only events with occurred_at >= this."
    ),
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_db),
) -> list[AuditEventOut]:
    """List recent audit events. Most recent first.

    Use the date range to bound the response (a busy prospect will
    generate hundreds of events per day). The `actor` filter is
    useful for "show me everything the cleanup job did" or
    "everything the AWS collector did for account 111111111111".
    """
    stmt = select(AuditEventORM).order_by(AuditEventORM.occurred_at.desc())
    if actor is not None:
        stmt = stmt.where(AuditEventORM.actor == actor)
    if action is not None:
        stmt = stmt.where(AuditEventORM.action == action)
    if since is not None:
        stmt = stmt.where(AuditEventORM.occurred_at >= since)
    stmt = stmt.limit(limit)
    events = session.execute(stmt).scalars().all()
    return [
        AuditEventOut(
            id=str(e.id),
            occurred_at=e.occurred_at.isoformat() if e.occurred_at else "",
            actor=e.actor,
            action=e.action,
            target_type=e.target_type,
            target_id=e.target_id,
            metadata=e.metadata_json,
        )
        for e in events
    ]


# ---------------------------------------------------------------------------
# /pii-classifications
# ---------------------------------------------------------------------------


class PIIClassificationOut(BaseModel):
    id: int
    resource_type: str
    resource_id: str
    field_name: str
    sensitivity: str
    value_hash: str
    classified_at: str  # ISO 8601


@router.get("/pii-classifications", response_model=list[PIIClassificationOut])
def list_pii_classifications(
    resource_type: str | None = Query(default=None),
    resource_id: str | None = Query(default=None),
    sensitivity: str | None = Query(
        default=None,
        description="Filter by sensitivity (public, internal, confidential, restricted).",
    ),
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_db),
) -> list[PIIClassificationOut]:
    """List PII classifications. Filter by resource or sensitivity
    for the privacy questionnaire's "what data do you have on
    account X?" question.

    The endpoint returns the SHA-256 hash of the value, not the
    value itself. The value lives in the source row where the
    business logic needs it; here we only expose metadata.
    """
    stmt = select(PIIClassificationORM).order_by(PIIClassificationORM.classified_at.desc())
    if resource_type is not None:
        stmt = stmt.where(PIIClassificationORM.resource_type == resource_type)
    if resource_id is not None:
        stmt = stmt.where(PIIClassificationORM.resource_id == resource_id)
    if sensitivity is not None:
        stmt = stmt.where(PIIClassificationORM.sensitivity == sensitivity)
    stmt = stmt.limit(limit)
    rows = session.execute(stmt).scalars().all()
    return [
        PIIClassificationOut(
            id=r.id,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            field_name=r.field_name,
            sensitivity=r.sensitivity,
            value_hash=r.value_hash,
            classified_at=r.classified_at.isoformat() if r.classified_at else "",
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# /retention-policies + /retention/run
# ---------------------------------------------------------------------------


class RetentionPolicyOut(BaseModel):
    id: str
    table_name: str
    retention_days: int
    enabled: bool
    last_applied_at: str | None
    last_deleted_count: int | None
    updated_at: str


@router.get("/retention-policies", response_model=list[RetentionPolicyOut])
def list_retention_policies(
    session: Session = Depends(get_db),
) -> list[RetentionPolicyOut]:
    """List the configured retention policies. The privacy /
    compliance team points to this endpoint when asked "what's
    your data retention policy?"."""
    policies = (
        session.execute(select(RetentionPolicyORM).order_by(RetentionPolicyORM.table_name))
        .scalars()
        .all()
    )
    return [
        RetentionPolicyOut(
            id=str(p.id),
            table_name=p.table_name,
            retention_days=p.retention_days,
            enabled=p.enabled,
            last_applied_at=p.last_applied_at.isoformat() if p.last_applied_at else None,
            last_deleted_count=p.last_deleted_count,
            updated_at=p.updated_at.isoformat() if p.updated_at else "",
        )
        for p in policies
    ]


class RetentionRunResult(BaseModel):
    tables_processed: int
    total_deleted: int
    per_table: dict[str, int]


@router.post("/retention/run", response_model=RetentionRunResult)
def trigger_retention_run(
    session: Session = Depends(get_db),
) -> RetentionRunResult:
    """Run the retention job manually. Useful for the operator
    who wants to force a cleanup, or for the cron job (the CLI
    is the recommended entry point for scheduled runs).

    Auto-seeds the default policies on first call so the operator
    doesn't have to remember to run `python -m ... retention --seed`.
    """
    seeded = seed_default_policies(session)
    if seeded:
        # We just inserted; the apply_all_enabled below will pick
        # them up. No need to commit yet — apply_all_enabled does it.
        pass
    results = apply_all_enabled(session)
    total = sum(max(0, v) for v in results.values())
    return RetentionRunResult(
        tables_processed=len(results),
        total_deleted=total,
        per_table=results,
    )
