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
    cors_origins: tuple[str, ...] = ("http://localhost:3000",)
    default_tenant_id: UUID = DEFAULT_TENANT_ID


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
