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

from constat_api.logging import configure_logging
from constat_api.middleware import RequestIDMiddleware
from constat_api.routers import (
    accounts,
    admin,
    aws,
    focus,
    health,
    inconclusive,
    insight_runs,
    insights,
    runner,
    status,
)
from constat_api.settings import settings

# Configure structured logging BEFORE anything else logs anything.
# JSON output is enabled via CONSTAT_LOG_JSON=1 in prod; local dev gets
# a colored console renderer.
configure_logging()

logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.api_title,
    description="Cloud inventory observability — the écart chiffré.",
    version="0.5.0",
)

# RequestIDMiddleware is the OUTERMOST middleware so it sees every
# request before auth / business logic, and the request_id is bound
# to structlog's contextvars for the entire request lifecycle.
app.add_middleware(RequestIDMiddleware)
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
app.include_router(insight_runs.router)
app.include_router(focus.router)
app.include_router(aws.router)
app.include_router(runner.router)
app.include_router(status.router)
app.include_router(accounts.router)
app.include_router(admin.router)
