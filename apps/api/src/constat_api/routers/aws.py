"""AWS collection HTTP endpoint.

Triggers the same path as the CLI but in-process. The base AWS session
is created from settings (env-driven).

V1: synchronous call (blocks the request for the duration of the scan).
V2: queue + background worker.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from constat_api.collectors.aws import TargetAccount, collect_targets
from constat_api.db import get_db
from constat_api.settings import get_base_aws_session

router = APIRouter(prefix="/collect/aws", tags=["aws"])


class TargetIn(BaseModel):
    aws_account_id: str
    role_arn: str | None = None
    external_id: str | None = None
    name: str | None = None
    regions: list[str] | None = None


class CollectRequest(BaseModel):
    targets: list[TargetIn] = Field(min_length=1, description="At least one target required")
    dry_run: bool = False


class CollectResultOut(BaseModel):
    aws_account_id: str
    regions_scanned: list[str]
    resources_written: int
    observations_written: int
    facts_written: int
    errors: list[str]


class CollectResponse(BaseModel):
    results: list[CollectResultOut]


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
        session, targets, base_session=base_session, dry_run=body.dry_run
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
            )
            for r in results
        ]
    )
