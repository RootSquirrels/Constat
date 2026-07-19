"""AWS collection HTTP endpoints (async, roadmap 1.1 / 1.2).

POST /collect/aws no longer scans inside the HTTP request: at ICP scale
(35 accounts x ~16 regions) a synchronous scan outlives any sane request
timeout. It now validates the targets, writes one `collect_jobs` row,
enqueues one WorkItem per (target x region), and returns 202 with the
job id. The actual scans run in the collection worker (`constat_api.worker`
— in-process pool in inline mode, external service in sqs mode), and
GET /collect/aws/jobs/{job_id} reports progress derived from source_runs.

Backpressure: when the in-process queue is full, the POST answers
503 + Retry-After instead of accepting work it cannot hold.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from constat_api.auth import Principal, require_operator, verify_api_key
from constat_api.collect_queue import QueueFullError, WorkItem, get_queue
from constat_api.collectors.aws import DEFAULT_REGIONS, JOB_REGISTRY
from constat_api.db import get_db
from constat_api.idempotency import cache_response, get_cached_or_none
from constat_api.repositories import collect_jobs as collect_jobs_repo
from constat_api.repositories import source_runs as source_runs_repo

router = APIRouter(
    prefix="/collect/aws",
    tags=["aws"],
    dependencies=[Depends(verify_api_key)],
)

# Retry-After value on 503 backpressure. Arbitrary but honest: the inline
# worker frees capacity within seconds; 10s tells a scripted caller to
# come back soon without hammering.
RETRY_AFTER_SECONDS = 10


class TargetIn(BaseModel):
    aws_account_id: str
    role_arn: str | None = None
    external_id: str | None = None
    name: str | None = None
    regions: list[str] | None = None
    # Selects which AWS resource types to scan. Default (None) = RDS only
    # for V1 backward compat. Known keys: "rds", "ec2_volume",
    # "ec2_snapshot", "ec2_instance". Unknown keys are rejected with 422
    # before anything is enqueued.
    resource_types: list[str] | None = None

    @model_validator(mode="after")
    def _role_arn_requires_external_id(self) -> TargetIn:
        """Confused-deputy guard (F-06): AssumeRole without an ExternalId
        lets anyone who learns the role ARN ride our trust policy, so a
        role_arn without external_id is rejected (HTTP 422)."""
        if self.role_arn and not self.external_id:
            raise ValueError("external_id is required when role_arn is set")
        return self


class CollectRequest(BaseModel):
    targets: list[TargetIn] = Field(min_length=1, description="At least one target required")
    dry_run: bool = False
    # When True, force-start a new scan even if a previous one is stuck
    # in 'running' for the same scope. Use after a worker crash to recover.
    force: bool = False


class CollectAcceptedResponse(BaseModel):
    """202 body: the job handle. Scans happen asynchronously."""

    job_id: UUID
    items_enqueued: int


class CleanupResponse(BaseModel):
    cleaned: int
    threshold_hours: float


class CollectJobRunOut(BaseModel):
    """One source_run line in the job status response."""

    region: str
    resource_type: str
    source: str
    status: str
    resources_found: int | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


class CollectJobStatusResponse(BaseModel):
    """Job row + progress derived from its source_runs.

    `scopes_started` counts distinct (account, region) pairs that have at
    least one source_run; `pending` = total_items - scopes_started (the
    "queued-ish" remainder). One work item writes one source_run per
    scanned resource type, so `runs` can outnumber `total_items`.
    """

    job_id: UUID
    actor: str
    created_at: datetime
    total_items: int
    summary: dict[str, Any]
    scopes_started: int
    pending: int
    runs_by_status: dict[str, int]
    runs: list[CollectJobRunOut]


def _idempotency_key_header(
    x_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str | None:
    """Read the Idempotency-Key header (or None when absent)."""
    return x_idempotency_key


@router.post("", status_code=202, response_model=CollectAcceptedResponse)
def trigger_aws_collect(
    body: CollectRequest,
    idempotency_key: str | None = Depends(_idempotency_key_header),
    session: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> CollectAcceptedResponse:
    # Idempotency replay: if a request with this key was processed
    # recently, return the cached response. Same key = same job, no
    # re-enqueue; body is ignored on replay.
    if idempotency_key:
        cached = get_cached_or_none("collect_aws", idempotency_key)
        if cached is not None:
            return CollectAcceptedResponse.model_validate(cached)

    # Validate resource_types BEFORE enqueueing: the collector validates
    # too, but in async mode a bad key would otherwise surface in the
    # worker, after the client already got a 202.
    for t in body.targets:
        if t.resource_types:
            unknown = sorted(set(t.resource_types) - set(JOB_REGISTRY))
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown resource_type(s) {unknown} (known: {sorted(JOB_REGISTRY)})",
                )

    # The job row first (flushed -> job_id), then one WorkItem per
    # (target x region). Summary is counts only — no account ids or ARNs,
    # same non-PII discipline as audit_events metadata.
    all_resource_types = sorted({rt for t in body.targets for rt in (t.resource_types or ("rds",))})
    n_regions = sum(len(t.regions) if t.regions else len(DEFAULT_REGIONS) for t in body.targets)
    job = collect_jobs_repo.create_job(
        session,
        actor=principal.name,
        total_items=n_regions,
        summary={
            "accounts": len({t.aws_account_id for t in body.targets}),
            "regions": n_regions,
            "resource_types": all_resource_types,
        },
    )
    items = [
        WorkItem(
            job_id=job.job_id,
            aws_account_id=t.aws_account_id,
            role_arn=t.role_arn,
            external_id=t.external_id,
            name=t.name,
            region=region,
            resource_types=tuple(t.resource_types) if t.resource_types else None,
            force=body.force,
            dry_run=body.dry_run,
        )
        for t in body.targets
        for region in (t.regions or DEFAULT_REGIONS)
    ]

    try:
        get_queue().send(items)
    except QueueFullError as e:
        # Backpressure (1.2): drop the job row and tell the caller to
        # slow down rather than grow an unbounded in-memory backlog.
        session.rollback()
        raise HTTPException(
            status_code=503,
            detail=str(e),
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        ) from e
    session.commit()

    response = CollectAcceptedResponse(job_id=job.job_id, items_enqueued=len(items))
    if idempotency_key:
        cache_response(
            "collect_aws",
            idempotency_key,
            response.model_dump(mode="json"),
        )
    return response


@router.get("/jobs/{job_id}", response_model=CollectJobStatusResponse)
def get_collect_job(
    job_id: UUID,
    session: Session = Depends(get_db),
) -> CollectJobStatusResponse:
    """Job status for the async collect flow. Reader role allowed."""
    job = collect_jobs_repo.get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown collect job {job_id}")
    runs = collect_jobs_repo.list_runs_for_job(session, job_id)
    runs_by_status: dict[str, int] = {}
    for r in runs:
        runs_by_status[r.status] = runs_by_status.get(r.status, 0) + 1
    scopes_started = len({(r.account_id, r.region) for r in runs})
    return CollectJobStatusResponse(
        job_id=job.job_id,
        actor=job.actor,
        created_at=job.created_at,
        total_items=job.total_items,
        summary=job.summary,
        scopes_started=scopes_started,
        pending=max(job.total_items - scopes_started, 0),
        runs_by_status=runs_by_status,
        runs=[
            CollectJobRunOut(
                region=r.region,
                resource_type=r.resource_type,
                source=r.source,
                status=r.status,
                resources_found=r.resources_found,
                error=r.error,
                started_at=r.started_at,
                finished_at=r.finished_at,
            )
            for r in runs
        ],
    )


@router.post(
    "/cleanup-stuck-runs", response_model=CleanupResponse, dependencies=[Depends(require_operator)]
)
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
