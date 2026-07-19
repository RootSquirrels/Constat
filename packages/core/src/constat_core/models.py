"""Core data models. Stable contract — changes require an ADR."""

from __future__ import annotations

from datetime import date, datetime
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
    resource_id: UUID | None = None
    account_id: str | None = None
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

    P1 item 1: operator acknowledgment. The three `ack_*` fields let
    the pilot's operator triage the daily critical list: which ones
    are in flight, which are resolved, which were dismissed. NULL
    ack_status means "open / not yet triaged". Last write wins in
    V1; history is V2 (a separate `insight_acks` table).
    """

    id: UUID | None = None
    rule_name: str  # e.g. "rds_eol"
    resource_id: UUID | None = None
    account_id: str | None = None
    severity: Severity
    title: str
    payload: dict[str, Any]
    computed_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=__import__("datetime").timezone.utc)
    )
    ack_status: str | None = None  # 'acknowledged' | 'in_progress' | 'resolved' | 'dismissed'
    ack_at: datetime | None = None  # server-set on PATCH
    ack_by: str | None = None  # free-form operator identifier


class Inconclusive(BaseModel):
    """A 'we don't know' record. The evaluation could not complete because
    key facts were missing or malformed. The GTM promise is to surface
    what the customer doesn't know — INCONCLUSIVE is its honest form
    (criterion n°15: visible, never silent).

    Distinct from Insight (which says "there IS a gap"). Distinct from
    NO_MATCH (which says "we know there's no gap").
    """

    id: UUID | None = None
    rule_name: str  # e.g. "rds_eol"
    resource_id: UUID | None = None
    account_id: str | None = None
    missing_facts: list[str]  # e.g. ["aws.rds.vcpu", "aws.rds.engine_version"]
    reason: str | None = None
    computed_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=__import__("datetime").timezone.utc)
    )
    # Work-queue fields (roadmap 2.5): an owner, a due date, and a triage
    # status. Written only by PATCH /inconclusives/{id}; the runner never
    # sets them (its delete-and-replace recreates rows at the defaults).
    owner: str | None = None
    due_date: date | None = None
    status: str = "open"  # 'open' | 'acknowledged' | 'resolved'
