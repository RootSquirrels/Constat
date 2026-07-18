"""Constat API — V1 (with persistence, FOCUS + AWS ingestion).

Routers:
- /health                       — DB ping
- /insights                     — list/get/post insights
- /collect/aws                  — AWS cross-account RDS collection
- /collect/focus                — FOCUS CSV ingestion
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from constat_api.routers import aws, focus, health, inconclusive, insights, runner
from constat_api.settings import settings

logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.api_title,
    description="Cloud inventory observability — the écart chiffré.",
    version="0.4.0",
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
app.include_router(inconclusive.router)
app.include_router(focus.router)
app.include_router(aws.router)
app.include_router(runner.router)
