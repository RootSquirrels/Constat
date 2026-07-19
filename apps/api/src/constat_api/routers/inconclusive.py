"""Inconclusive HTTP endpoints.

Returns the 'we don't know' records. Parallel to /insights: a complete
picture of fleet coverage requires both endpoints.

Roadmap 2.5: the queue is an operator work queue — PATCH /inconclusives/{id}
assigns an owner / due date / triage status, and GET /inconclusives can
filter and sort the queue.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from constat_core.models import Inconclusive
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from constat_api.audit import get_audit_db, record_event, record_read
from constat_api.auth import Principal, require_operator, verify_api_key
from constat_api.db import get_db
from constat_api.repositories import inconclusive as repo

router = APIRouter(
    prefix="/inconclusives",
    tags=["inconclusive"],
    dependencies=[Depends(verify_api_key)],
)


@router.get("", response_model=list[Inconclusive])
def list_inconclusive_endpoint(
    rule_name: str | None = Query(default=None),
    account_id: UUID | None = Query(default=None),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="Triage status: open | acknowledged | resolved.",
    ),
    sort: str = Query(
        default="computed_at",
        description=(
            "computed_at (newest first, default) | rule_name (group by rule, "
            "newest first inside each rule). There is deliberately no 'impact' "
            "sort: inconclusive records carry no amounts — we don't fake a "
            "score. The honest triage order is by rule, then by age."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
    principal: Principal = Depends(verify_api_key),
    audit_session: Session = Depends(get_audit_db),
) -> list[Inconclusive]:
    if status_filter is not None and status_filter not in repo.WORKFLOW_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"invalid status {status_filter!r}; must be one of {sorted(repo.WORKFLOW_STATUSES)}"
            ),
        )
    if sort not in repo.SORTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid sort {sort!r}; must be one of {sorted(repo.SORTS)}",
        )
    items = repo.list_inconclusive(
        session,
        rule_name=rule_name,
        account_id=account_id,
        status=status_filter,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    # Read attribution (CISO 3.3): the "we don't know" surface is as
    # sensitive as the insights themselves.
    record_read(
        audit_session,
        actor=principal.audit_actor,
        tenant_id=principal.tenant_id,
        target_type="inconclusives",
        route="/inconclusives",
        filters={
            "rule_name": rule_name is not None,
            "account_id": account_id is not None,
            "status": status_filter is not None,
        },
        row_count=len(items),
    )
    return items


@router.get("/{inconclusive_id}", response_model=Inconclusive)
def get_inconclusive_endpoint(
    inconclusive_id: UUID,
    session: Session = Depends(get_db),
    principal: Principal = Depends(verify_api_key),
    audit_session: Session = Depends(get_audit_db),
) -> Inconclusive:
    """O(1) lookup via repo.get_inconclusive. Replaces the previous small-N scan."""
    item = repo.get_inconclusive(session, inconclusive_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="inconclusive not found")
    record_read(
        audit_session,
        actor=principal.audit_actor,
        tenant_id=principal.tenant_id,
        target_type="inconclusive",
        route="/inconclusives/{inconclusive_id}",
        row_count=1,
    )
    return item


@router.post(
    "",
    response_model=Inconclusive,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_operator)],
)
def create_inconclusive_endpoint(
    item: Inconclusive, session: Session = Depends(get_db)
) -> Inconclusive:
    """Insert one inconclusive. Used by tests + ingestion workers."""
    return repo.insert_inconclusive(session, item)


# ----------------------------------------------------------------------------
# Operator workflow (roadmap 2.5) — mirrors the PATCH /insights ack pattern
# ----------------------------------------------------------------------------


class WorkflowPatchIn(BaseModel):
    """Body for PATCH /inconclusives/{id}. All fields optional: only the
    keys the client explicitly sends are applied (None clears a field)."""

    owner: str | None = None
    due_date: date | None = None
    status: str | None = None


@router.patch(
    "/{inconclusive_id}",
    response_model=Inconclusive,
    dependencies=[Depends(require_operator)],
)
def patch_inconclusive_endpoint(
    inconclusive_id: UUID,
    body: WorkflowPatchIn,
    session: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> Inconclusive:
    """Set the workflow fields of one inconclusive record (owner/due/status).

    Partial-update semantics: `model_fields_set` tells "cleared" (key sent
    with null) apart from "untouched" (key absent). Last write wins; every
    patch records an audit_events row with the calling principal — the
    values themselves (owner name) stay out of the audit metadata, only
    the field names are logged (no PII).
    """
    if body.status is not None and body.status not in repo.WORKFLOW_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"invalid status {body.status!r}; must be one of {sorted(repo.WORKFLOW_STATUSES)}"
            ),
        )
    fields: dict[str, Any] = body.model_dump(include=body.model_fields_set)
    updated = repo.update_workflow(session, inconclusive_id, fields)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="inconclusive not found")
    record_event(
        session,
        action="inconclusive_workflow",
        actor=principal.audit_actor,
        tenant_id=principal.tenant_id,
        target_type="inconclusive",
        target_id=str(inconclusive_id),
        metadata={"fields_updated": sorted(fields)},
    )
    session.commit()
    return updated
