"""Health check — pings the database to surface a stuck connection early."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from constat_api.db import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health(session: Session = Depends(get_db)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}
