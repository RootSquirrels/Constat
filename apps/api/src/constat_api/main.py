"""Constat API — V1 (with persistence, FOCUS + AWS ingestion).

Routers:
- /health                       — DB ping
- /insights                     — list/get/post insights
- /collect/aws                  — AWS cross-account collection (async: 202 + job)
- /collect/focus                — FOCUS CSV ingestion

Lifespan: in `inline` collect mode the API process also hosts the
collection worker pool (a few daemon threads draining the in-process
queue). In `sqs` mode no threads start here — an external worker service
(`python -m constat_api.worker`) drains the queue.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from constat_api.auth import verify_metrics_key
from constat_api.logging import configure_logging
from constat_api.metrics import render_metrics
from constat_api.middleware import HTTPMetricsMiddleware, RequestIDMiddleware
from constat_api.routers import (
    accounts,
    admin,
    aws,
    collect_targets,
    compliance,
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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start/stop the inline collection worker pool.

    Only in inline mode AND when CONSTAT_WORKER_INLINE is on — tests set
    it to 0 (conftest) and drive worker.drain_once directly, which keeps
    drains deterministic (no background thread racing the assertions).
    Shutdown: set the stop event, then join briefly. Threads are daemons,
    so a wedged region scan can never hang process exit; a healthy
    shutdown finishes the in-flight item first.
    """
    stop_event = threading.Event()
    threads: list[threading.Thread] = []
    if settings.collect_mode == "inline" and settings.worker_inline:
        from constat_api.collect_queue import get_queue
        from constat_api.db import SessionLocal
        from constat_api.worker import start_worker_pool

        threads = start_worker_pool(
            SessionLocal,
            get_queue(),
            concurrency=settings.worker_concurrency,
            per_account=settings.worker_per_account,
            stop_event=stop_event,
        )
        logger.info(
            "inline collect worker pool started (%d threads, per-account cap %d)",
            len(threads),
            settings.worker_per_account,
        )
    try:
        yield
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=5)


app = FastAPI(
    title=settings.api_title,
    description="Cloud inventory observability — the écart chiffré.",
    version="0.5.0",
    lifespan=lifespan,
)

# RequestIDMiddleware is the OUTERMOST middleware so it sees every
# request before auth / business logic, and the request_id is bound
# to structlog's contextvars for the entire request lifecycle.
app.add_middleware(RequestIDMiddleware)
# HTTPMetricsMiddleware records the SLO counters/histograms. Inside
# RequestIDMiddleware so the access log and the metric both fire on
# the same path.
app.add_middleware(HTTPMetricsMiddleware)
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
app.include_router(collect_targets.router)
app.include_router(runner.router)
app.include_router(status.router)
app.include_router(accounts.router)
app.include_router(admin.router)
app.include_router(compliance.router)


# Prometheus scrape endpoint (F-15). Gated by CONSTAT_METRICS_KEY when
# set (X-Metrics-Key header). When unset it stays open — same trust
# model as /health: the scraper is on the trusted network (k8s sidecar,
# a dedicated VPC, an internal LB) — and we warn at startup so an
# accidental public exposure doesn't go unnoticed.
if not settings.metrics_key:
    logger.warning(
        "/metrics is OPEN (CONSTAT_METRICS_KEY unset). "
        "Set it before exposing the API beyond the trusted network."
    )


@app.get("/metrics", include_in_schema=False, dependencies=[Depends(verify_metrics_key)])
def metrics_endpoint() -> Response:
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)
