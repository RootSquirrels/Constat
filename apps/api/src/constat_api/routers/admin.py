"""Admin HTTP endpoints.

UX/ops P2 item 8: scheduled cleanup of the `inconclusive` table.
The endpoint is the trigger an external scheduler (cron, k8s
CronJob, Task Scheduler) calls. Same auth as the other routers.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from constat_api.auth import require_operator, verify_api_key
from constat_api.db import get_db
from constat_api.repositories import inconclusive as inconclusive_repo

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(verify_api_key)],
)


class CleanupResponse(BaseModel):
    older_than_days: int
    deleted: int


@router.post(
    "/cleanup-inconclusives",
    response_model=CleanupResponse,
    dependencies=[Depends(require_operator)],
)
def cleanup_inconclusives(
    older_than_days: int = Query(
        default=30, ge=1, le=365, description="Delete records older than N days."
    ),
    session: Session = Depends(get_db),
) -> CleanupResponse:
    """Delete inconclusive records older than N days.

    Returns the number of rows deleted. Caller (the scheduler) owns
    the response — there is no idempotency token; calling twice in
    the same hour is safe (second call deletes 0).
    """
    deleted = inconclusive_repo.delete_older_than(session, older_than_days=older_than_days)
    session.commit()
    return CleanupResponse(older_than_days=older_than_days, deleted=deleted)
