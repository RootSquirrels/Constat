"""FOCUS ingestion HTTP endpoint.

Triggers the same path as the CLI but in-process. The file_path can be
a CSV or Parquet file — format is detected by extension. For V1 the
server must have access to the file. File upload (via multipart) is V2.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from constat_api.audit import get_audit_db, record_read
from constat_api.auth import Principal, require_operator, verify_api_key
from constat_api.cli.focus import ingest_focus_file
from constat_api.db import get_db
from constat_api.repositories.focus_coverage import compute_focus_coverage

router = APIRouter(
    prefix="/collect/focus",
    tags=["focus"],
    dependencies=[Depends(verify_api_key)],
)

# Read surface for FOCUS (coverage diagnostics). Kept on a separate
# router because the ingest router is mounted under /collect/focus while
# the coverage endpoint lives at /focus/coverage.
read_router = APIRouter(
    tags=["focus"],
    dependencies=[Depends(verify_api_key)],
)


class IngestRequest(BaseModel):
    account_external_id: str
    file_path: str  # Path to FOCUS file (CSV or Parquet)
    account_name: str | None = None


class IngestResponse(BaseModel):
    account_id: str
    rows_total: int  # data rows in the file (header excluded for CSV)
    rows_read: int  # rows that parsed successfully
    rows_skipped: int  # rows_total - rows_read; malformed/dropped by the loader
    rows_written: int
    inserted: int
    updated: int
    duration_seconds: float


@router.post("", response_model=IngestResponse, dependencies=[Depends(require_operator)])
def trigger_focus_ingest(body: IngestRequest, session: Session = Depends(get_db)) -> IngestResponse:
    try:
        result = ingest_focus_file(
            session=session,
            path=Path(body.file_path),
            account_external_id=body.account_external_id,
            account_name=body.account_name,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    return IngestResponse(
        account_id=result.account_id,
        rows_total=result.rows_total,
        rows_read=result.rows_read,
        rows_skipped=result.rows_skipped,
        rows_written=result.rows_written,
        inserted=result.inserted,
        updated=result.updated,
        duration_seconds=result.duration_seconds,
    )


class FocusCoverageAccount(BaseModel):
    account_id: str
    periods: list[tuple[str, str]]  # (period_start, period_end), ISO dates, sorted
    covered_months: int
    missing_months: list[str]  # YYYY-MM labels absent inside the observed range
    stale: bool  # latest period_end older than STALE_AFTER_DAYS
    first_period: str | None
    last_period: str | None


class FocusCoverageResponse(BaseModel):
    accounts: list[FocusCoverageAccount]
    has_gaps: bool  # any account with missing months inside its range
    has_stale: bool  # any account whose data is older than STALE_AFTER_DAYS


@read_router.get("/focus/coverage", response_model=FocusCoverageResponse)
def get_focus_coverage(
    session: Session = Depends(get_db),
    principal: Principal = Depends(verify_api_key),
    audit_session: Session = Depends(get_audit_db),
) -> FocusCoverageResponse:
    """FOCUS coverage diagnostics (known-issues.md §4 detection half).

    A truncated or lagging FOCUS export must never silently produce a
    wrong chargeback: this endpoint reports per-account month gaps inside
    the observed period range and stale data so the web can warn instead
    of presenting understated totals as fact.
    """
    coverages = compute_focus_coverage(session)
    # Read attribution (CISO 3.3): coverage reveals the account fleet's
    # billing footprint — who looked must be on record.
    record_read(
        audit_session,
        actor=principal.name,
        target_type="focus_coverage",
        route="/focus/coverage",
        row_count=len(coverages),
    )
    return FocusCoverageResponse(
        accounts=[
            FocusCoverageAccount(
                account_id=str(c.account_id),
                periods=[(start.isoformat(), end.isoformat()) for start, end in c.periods],
                covered_months=c.covered_months,
                missing_months=c.missing_months,
                stale=c.stale,
                first_period=c.first_period.isoformat() if c.first_period else None,
                last_period=c.last_period.isoformat() if c.last_period else None,
            )
            for c in coverages
        ],
        has_gaps=any(c.missing_months for c in coverages),
        has_stale=any(c.stale for c in coverages),
    )
