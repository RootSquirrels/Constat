"""AWS collection HTTP endpoint.

Triggers the same path as the CLI but in-process. The base AWS session
is created from settings (env-driven).

V1: synchronous call (blocks the request for the duration of the scan).
V2: queue + background worker.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from constat_api.auth import verify_api_key
from constat_api.collectors.aws import TargetAccount, collect_targets
from constat_api.db import get_db
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.settings import get_base_aws_session

router = APIRouter(
    prefix="/collect/aws",
    tags=["aws"],
    dependencies=[Depends(verify_api_key)],
)


class TargetIn(BaseModel):
    aws_account_id: str
    role_arn: str | None = None
    external_id: str | None = None
    name: str | None = None
    regions: list[str] | None = None


class CollectRequest(BaseModel):
    targets: list[TargetIn] = Field(min_length=1, description="At least one target required")
    dry_run: bool = False
    # When True, force-start a new scan even if a previous one is stuck
    # in 'running' for the same scope. Use after a worker crash to recover.
    force: bool = False
    # Circuit breaker threshold: after this many consecutive region
    # failures, the rest of the regions are skipped. Default 2.
    max_consecutive_region_errors: int = 2


class CollectResultOut(BaseModel):
    aws_account_id: str
    regions_scanned: list[str]
    resources_written: int
    observations_written: int
    facts_written: int
    errors: list[str]
    regions_skipped_by_breaker: list[str] = []


class CollectResponse(BaseModel):
    results: list[CollectResultOut]


class CleanupResponse(BaseModel):
    cleaned: int
    threshold_hours: float


@router.post("", response_model=CollectResponse)
def trigger_aws_collect(
    body: CollectRequest, session: Session = Depends(get_db)
) -> CollectResponse:
    targets = [
        TargetAccount(
            aws_account_id=t.aws_account_id,
            role_arn=t.role_arn,
            external_id=t.external_id,
            name=t.name,
            regions=tuple(t.regions) if t.regions else None,
        )
        for t in body.targets
    ]
    base_session = get_base_aws_session()
    results: list[Any] = collect_targets(
        session,
        targets,
        base_session=base_session,
        dry_run=body.dry_run,
        force=body.force,
        max_consecutive_region_errors=body.max_consecutive_region_errors,
    )
    return CollectResponse(
        results=[
            CollectResultOut(
                aws_account_id=r.aws_account_id,
                regions_scanned=r.regions_scanned,
                resources_written=r.resources_written,
                observations_written=r.observations_written,
                facts_written=r.facts_written,
                errors=r.errors,
                regions_skipped_by_breaker=r.regions_skipped_by_breaker,
            )
            for r in results
        ]
    )


@router.post("/cleanup-stuck-runs", response_model=CleanupResponse)
def trigger_cleanup_stuck_runs(
    threshold_hours: float = 2.0,
    session: Session = Depends(get_db),
) -> CleanupResponse:
    """Mark source_runs stuck in 'running' for longer than `threshold_hours`
    as 'failed'. Returns the number cleaned up.

    Wire this into a periodic scheduler (cron, Fargate task) to recover
    from worker crashes. Idempotent: a no-op when nothing is stuck.
    """
    cleaned = source_runs_repo.cleanup_stuck_runs(
        session, threshold=timedelta(hours=threshold_hours)
    )
    return CleanupResponse(cleaned=cleaned, threshold_hours=threshold_hours)
