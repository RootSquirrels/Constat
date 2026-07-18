"""Constat API — V1 (with persistence).

Routers:
- /health    — DB ping
- /insights  — list/get/post insights
- /collect/* — ingestion triggers (stubs; real impl in commit #2/#3)
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from constat_api.routers import health, insights
from constat_api.settings import settings

logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.api_title,
    description="Cloud inventory observability — the écart chiffré.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(insights.router)


@app.post("/collect/aws")
def trigger_aws_collect() -> dict[str, str]:
    """Trigger an AWS RDS collection run. V1: no-op stub.

    Real implementation lands in commit #3 (cross-account AssumeRole).
    """
    logger.info("AWS collection trigger received (no-op in V1)")
    return {"status": "queued"}


@app.post("/collect/focus")
def trigger_focus_collect() -> dict[str, str]:
    """Trigger a FOCUS ingestion run. V1: no-op stub.

    Real implementation lands in commit #2 (FOCUS CLI).
    """
    logger.info("FOCUS collection trigger received (no-op in V1)")
    return {"status": "queued"}
