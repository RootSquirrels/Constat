"""Constat API — V1 (with persistence + FOCUS ingestion).

Routers:
- /health                       — DB ping
- /insights                     — list/get/post insights
- /collect/aws                  — AWS collection stub
- /collect/focus                — FOCUS CSV ingestion
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from constat_api.routers import focus, health, insights
from constat_api.settings import settings

logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.api_title,
    description="Cloud inventory observability — the écart chiffré.",
    version="0.2.0",
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
app.include_router(focus.router)


@app.post("/collect/aws")
def trigger_aws_collect() -> dict[str, str]:
    """Trigger an AWS RDS collection run. V1: no-op stub.

    Real implementation lands in commit #3 (cross-account AssumeRole).
    """
    logger.info("AWS collection trigger received (no-op in V1)")
    return {"status": "queued"}
