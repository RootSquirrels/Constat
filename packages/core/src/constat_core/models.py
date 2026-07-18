"""Core data models. Stable contract — changes require an ADR."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from constat_core.namespaces import ValueState


class Resource(BaseModel):
    """Stable identity of a cloud resource across observations.

    `retired_at` is non-null only when retirement is *proven* by a complete
    source scan. We never guess a resource is gone.
    """

    id: UUID = Field(default_factory=uuid4)
    account_id: str  # AWS account ID (external, 12-digit)
    region: str
    resource_type: str  # e.g. "AWS::RDS::DBInstance"
    native_id: str  # e.g. ARN
    first_seen_at: datetime
    last_seen_at: datetime
    retired_at: datetime | None = None


class Observation(BaseModel):
    """Immutable source data point. Replayable from S3/Parquet."""

    id: UUID = Field(default_factory=uuid4)
    resource_id: UUID
    source: str  # e.g. "aws_rds"
    observed_at: datetime
    payload: dict[str, Any]
    ingested_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=__import__("datetime").timezone.utc)
    )


class Fact(BaseModel):
    """Current value, namespaced. Source + state + timestamp.

    `resource_id` is null for facts scoped to an account (e.g. cost aggregates).
    `account_id` is null only for catalog facts (which are global).
    """

    id: UUID | None = None
    resource_id: UUID | None
    account_id: str | None
    namespace: str  # e.g. "aws.rds"
    key: str
    value: Any
    value_state: ValueState
    source: str
    observed_at: datetime
    computed_at: datetime | None = None


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Insight(BaseModel):
    """A computed gap. The `payload` carries enough evidence to be proven.

    `resource_id` is null for account-level insights (e.g. total chargeback drift).
    """

    id: UUID | None = None
    rule_name: str  # e.g. "rds_eol"
    resource_id: UUID | None
    account_id: str | None
    severity: Severity
    title: str
    payload: dict[str, Any]
    computed_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=__import__("datetime").timezone.utc)
    )
