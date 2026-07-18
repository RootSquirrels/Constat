"""FOCUS ingestion HTTP endpoint.

Triggers the same path as the CLI but in-process. CSV path comes from the
request body — for V1 the server must have access to the file. File upload
(via multipart) is V2.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from constat_api.cli.focus import ingest_focus_csv
from constat_api.db import get_db

router = APIRouter(prefix="/collect/focus", tags=["focus"])


class IngestRequest(BaseModel):
    account_external_id: str
    csv_path: str
    account_name: str | None = None


class IngestResponse(BaseModel):
    account_id: str
    rows_read: int
    rows_written: int
    inserted: int
    updated: int
    duration_seconds: float


@router.post("", response_model=IngestResponse)
def trigger_focus_ingest(body: IngestRequest, session: Session = Depends(get_db)) -> IngestResponse:
    try:
        result = ingest_focus_csv(
            session=session,
            csv_path=Path(body.csv_path),
            account_external_id=body.account_external_id,
            account_name=body.account_name,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    return IngestResponse(
        account_id=result.account_id,
        rows_read=result.rows_read,
        rows_written=result.rows_written,
        inserted=result.inserted,
        updated=result.updated,
        duration_seconds=result.duration_seconds,
    )
