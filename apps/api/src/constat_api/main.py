"""Constat API — V1 skeleton.

Endpoints:
- GET  /health        — liveness
- GET  /insights      — list current insights (stub; DB-backed in next commit)
- POST /collect/aws   — trigger an AWS RDS collection run (stub; no-op in V1)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Constat API",
    description="Cloud inventory observability — the écart chiffré.",
    version="0.0.0",
)


class InsightOut(BaseModel):
    id: str
    rule_name: str
    severity: str
    title: str
    payload: dict[str, Any]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# Stub: in-memory insights. Will be replaced by a DB-backed query in the next commit.
_INSIGHTS: list[dict[str, Any]] = []


@app.get("/insights", response_model=list[InsightOut])
def list_insights() -> list[dict[str, Any]]:
    """List all current insights. V1: stub."""
    return _INSIGHTS


@app.post("/collect/aws")
def trigger_aws_collect() -> dict[str, str]:
    """Trigger an AWS RDS collection run. V1: no-op stub.

    Real implementation will: assume cross-account role, call the connector,
    write to Postgres + S3.
    """
    logger.info("AWS collection trigger received (no-op in V1)")
    return {"status": "queued"}
