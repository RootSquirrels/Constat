"""Application settings.

V1: env-driven, no secrets manager. The CONSTAT_DATABASE_URL is the main knob.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID

import boto3

# V1 single-tenant. Every row gets this tenant_id. The column is in place
# from day 1 so V2 multi-tenant is a migration of policies, not schema.
DEFAULT_TENANT_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "CONSTAT_DATABASE_URL",
        "postgresql://constat:constat@localhost:5432/constat",
    )
    api_title: str = "Constat API"
    # CORS origins for the web app (F-15). Env CONSTAT_CORS_ORIGINS is a
    # comma-separated list; unset falls back to the local Next.js dev
    # server.
    cors_origins: tuple[str, ...] = tuple(
        o.strip() for o in os.getenv("CONSTAT_CORS_ORIGINS", "").split(",") if o.strip()
    ) or ("http://localhost:3000",)
    default_tenant_id: UUID = DEFAULT_TENANT_ID
    # V1 auth: optional API key. When set, every request must carry a
    # matching X-API-Key header. When unset (dev), auth is open — a
    # warning is logged at startup. NEVER deploy without setting this
    # in any environment that an external caller can reach.
    api_key: str | None = os.getenv("CONSTAT_API_KEY") or None
    # F-10: POST /insights lets any caller forge an insight without
    # provenance. It exists for tests and local demos, so it is gated
    # behind this explicit opt-in, default OFF. Real insights are
    # written by the rule runner, not this endpoint.
    enable_manual_insights: bool = os.getenv("CONSTAT_ENABLE_MANUAL_INSIGHTS", "").lower() in (
        "1",
        "true",
        "yes",
    )
    # F-15: optional key for /metrics. When set, the scraper must send
    # it via the X-Metrics-Key header. When unset, /metrics stays open
    # (same trust model as /health: the scraper is on the trusted
    # network) and a warning is logged at startup. Set this before
    # exposing /metrics beyond the private network.
    metrics_key: str | None = os.getenv("CONSTAT_METRICS_KEY") or None


settings = Settings()


def get_base_aws_session() -> boto3.Session:
    """Build the base boto3 session used for AssumeRole.

    Local dev: CONSTAT_AWS_PROFILE=<name> reads ~/.aws/credentials.
    Prod (ECS/Fargate): use the task IAM role via default chain.
    """
    profile = os.getenv("CONSTAT_AWS_PROFILE")
    if profile:
        return boto3.Session(profile_name=profile)
    return boto3.Session()
