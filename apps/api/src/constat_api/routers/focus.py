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

from constat_api.auth import require_operator, verify_api_key
from constat_api.cli.focus import ingest_focus_file
from constat_api.db import get_db

router = APIRouter(
    prefix="/collect/focus",
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
