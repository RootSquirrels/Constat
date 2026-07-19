"""SQLAlchemy ORM models. Mirror db/migrations/0001_init.sql.

Portable: works on both Postgres (production) and sqlite (tests). Custom types
GUID and JSONBType bridge the dialect differences.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import CHAR, JSON, TypeDecorator

from constat_api.settings import DEFAULT_TENANT_ID


class GUID(TypeDecorator):
    """Platform-independent UUID. Native UUID on Postgres, CHAR(36) elsewhere."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[override]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PgUUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):  # type: ignore[override]
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value, dialect):  # type: ignore[override]
        if value is None:
            return value
        if isinstance(value, UUID):
            return value
        return UUID(str(value))


class JSONBType(TypeDecorator):
    """JSONB on Postgres, JSON elsewhere."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[override]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class Base(DeclarativeBase):
    """SQLAlchemy declarative base. All ORM models inherit from this."""


class AccountORM(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # Tenant-scoped external id (audit F-12, migration 0011): two
        # tenants may reference the same AWS account id (MSP case), so
        # uniqueness is per (tenant_id, external_id), not global.
        UniqueConstraint("tenant_id", "external_id", name="uq_accounts_tenant_external"),
    )

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResourceORM(Base):
    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "region", "resource_type", "native_id", name="uq_resource_identity"
        ),
        Index("idx_resources_account_type", "account_id", "resource_type"),
    )

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    account_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    region: Mapped[str] = mapped_column(String, nullable=False)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    native_id: Mapped[str] = mapped_column(String, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ObservationORM(Base):
    __tablename__ = "observations"

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    resource_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONBType(), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    source_run_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("source_runs.id", ondelete="SET NULL")
    )


class FactORM(Base):
    __tablename__ = "facts"
    __table_args__ = (
        # Current-state UNIQUE: one row per (tenant, resource, namespace, key, source).
        # observed_at is the timestamp of the most recent observation (metadata),
        # NOT part of the key. Matches migration 0006's uq_fact_current.
        # See docs/development/known-issues.md §1.
        UniqueConstraint(
            "tenant_id",
            "resource_id",
            "namespace",
            "key",
            "source",
            name="uq_fact_current",
        ),
        CheckConstraint(
            "resource_id IS NOT NULL OR account_id IS NOT NULL", name="fact_scope_present"
        ),
        CheckConstraint(
            "value_state IN ('KNOWN', 'UNKNOWN', 'STALE', 'ERROR')", name="fact_value_state_check"
        ),
    )

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    resource_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("resources.id", ondelete="CASCADE")
    )
    account_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="CASCADE")
    )
    namespace: Mapped[str] = mapped_column(String, nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[Any] = mapped_column(JSONBType())
    value_state: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_source_run_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("source_runs.id", ondelete="SET NULL")
    )


class FocusChargeORM(Base):
    __tablename__ = "focus_charges"

    # BigSerial on postgres; Integer on sqlite (sqlite's ROWID alias only
    # auto-increments INTEGER PRIMARY KEY, not BigInteger).
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    account_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    service: Mapped[str] = mapped_column(String, nullable=False)
    region: Mapped[str | None] = mapped_column(String)
    pricing_category: Mapped[str | None] = mapped_column(String)
    billed_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0")
    )
    amortized_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0")
    )
    resource_id: Mapped[str | None] = mapped_column(String)
    sub_account_id: Mapped[str | None] = mapped_column(String)
    # FOCUS Tags: denormalized list of unique tag dicts seen for this
    # (service, period) row. Kept for fast access; the source of truth
    # for per-row tag attribution is `focus_charge_tags` (migration
    # 0009). The chargeback_by_tag runner uses the per-row table to
    # attribute cost proportionally (no more even-split approximation).
    tags: Mapped[list[dict[str, str]]] = mapped_column(JSONBType(), nullable=False, default=list)
    charge_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FocusChargeTagORM(Base):
    """V2 per-row FOCUS Tags (P3 item 11 fix).

    One row per (focus_charge, input FOCUS row, key, value) tuple. The
    `focus_charges.tags` JSONB column is a denormalized cache of the
    unique tag dicts; this table preserves per-row data so the
    chargeback runner can attribute cost proportionally rather than
    evenly across unique values.

    Why per-row matters: a focus_charge representing 5 input FOCUS
    rows might have 3 rows tagged Application=web and 2 tagged
    Application=api. V1 even-split gave 50/50; V2 gives 60/40.
    """

    __tablename__ = "focus_charge_tags"
    # NO UniqueConstraint: a focus_charge representing N input rows has
    # N rows per (key, value) — the count IS the signal that drives
    # proportional cost attribution in the runner. UNIQUE would silently
    # break the V2 fix (see migration 0009 for the rationale).

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    focus_charge_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("focus_charges.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(String, nullable=False)


class InsightORM(Base):
    __tablename__ = "insights"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical')", name="insights_severity_check"
        ),
        CheckConstraint(
            "resource_id IS NOT NULL OR account_id IS NOT NULL", name="insight_scope_present"
        ),
        CheckConstraint(
            "ack_status IN ('acknowledged', 'in_progress', 'resolved', 'dismissed')",
            name="insights_ack_status_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    rule_name: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("resources.id", ondelete="CASCADE")
    )
    account_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="CASCADE")
    )
    severity: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONBType(), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # P1 item 1: operator acknowledgment. NULL = "open / not yet
    # triaged". The runner / collector never writes these; only the
    # PATCH /insights/{id} endpoint does. ack_at is server-set.
    ack_status: Mapped[str | None] = mapped_column(String, nullable=True)
    ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ack_by: Mapped[str | None] = mapped_column(String, nullable=True)


class InsightEventORM(Base):
    """Appeared/resolved history of insights (roadmap 2.4, migration 0017).

    The runner's delete-and-replace wipes the insights table each run; this
    append-only table keeps the lifecycle: one row when a gap appears, one
    when it closes (with the last known monthly amount = money recovered).
    `fingerprint` (sha256 of rule_name|resource_id|title) is the stable
    identity of an insight across runs — the rows it replaced are gone, so
    we cannot FK to insights.

    resource_id / insight_run_id are ON DELETE SET NULL: history must
    survive the retirement of the resource or the purge of old runs.
    """

    __tablename__ = "insight_events"
    __table_args__ = (
        CheckConstraint("event IN ('appeared', 'resolved')", name="insight_events_event_check"),
        Index("idx_insight_events_tenant_rule_time", "tenant_id", "rule_name", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    rule_name: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("resources.id", ondelete="SET NULL")
    )
    # TEXT (not the accounts FK): chargeback insights are account-scoped
    # and history must not break if the account row disappears.
    account_id: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str] = mapped_column(String, nullable=False)
    monthly_usd: Mapped[float | None] = mapped_column(Float)
    insight_run_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("insight_runs.id", ondelete="SET NULL")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InsightRunORM(Base):
    __tablename__ = "insight_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'failed', 'partial')",
            name="insight_runs_status_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    rule_name: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, nullable=False)
    resources_scanned: Mapped[int | None] = mapped_column(Integer)
    insights_emitted: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class InconclusiveORM(Base):
    __tablename__ = "inconclusive"
    __table_args__ = (
        # Roadmap 2.5 (migration 0018): the inconclusive queue is an
        # operator work queue with a triage status.
        CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved')", name="inconclusive_status_check"
        ),
    )

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    rule_name: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("resources.id", ondelete="CASCADE")
    )
    account_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="CASCADE")
    )
    missing_facts: Mapped[list[str]] = mapped_column(JSONBType(), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Workflow fields (roadmap 2.5). Written only by PATCH /inconclusives/{id};
    # the runner never touches them... except that delete-and-replace recreates
    # the rows, which reset the workflow to defaults — accepted V1 semantic.
    owner: Mapped[str | None] = mapped_column(String)
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="open", server_default="open"
    )


class CollectJobORM(Base):
    """One accepted POST /collect/aws (async collection, migration 0015).

    The row is written at enqueue time; workers do not update it — job
    progress is DERIVED from the source_runs that carry its job_id, so a
    crashed worker never leaves the job row itself lying. `summary` holds
    counts only (accounts / regions / resource_types), never PII.
    """

    __tablename__ = "collect_jobs"

    job_id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String, nullable=False)
    total_items: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONBType(), nullable=False, default=dict)


class CollectTargetORM(Base):
    """Persisted collect target (batch onboarding, migration 0016).

    One row per (tenant, AWS account) to scan. POST /collect/aws with an
    empty body collects every row here; POST /collect/targets/import
    upserts them from a CSV.

    external_id is a shared secret (F-06). It is write-only over the API —
    the repository's `list_targets` defers it by default so a GET handler
    cannot leak it by accident; only the collect path selects it.

    The 12-digit CHECK on aws_account_id lives in the migration only:
    its Postgres `~` regex is not portable to sqlite (tests), so the
    repository/router re-validate in Python instead.
    """

    __tablename__ = "collect_targets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "aws_account_id", name="uq_collect_targets_tenant_account"),
    )

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    aws_account_id: Mapped[str] = mapped_column(String, nullable=False)
    role_arn: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str | None] = mapped_column(String)
    regions: Mapped[list[str] | None] = mapped_column(JSONBType())
    resource_types: Mapped[list[str] | None] = mapped_column(JSONBType())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceRunORM(Base):
    __tablename__ = "source_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'failed', 'partial')",
            name="source_runs_status_check",
        ),
        Index(
            "uq_source_run_active",
            "account_id",
            "region",
            "resource_type",
            "source",
            unique=True,
            sqlite_where=text("status = 'running'"),
            postgresql_where=text("status = 'running'"),
        ),
        Index("idx_source_runs_tenant_job", "tenant_id", "job_id"),
    )

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    account_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    region: Mapped[str] = mapped_column(String, nullable=False)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, nullable=False)
    resources_found: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    # Nullable back-pointer to the collect_jobs row that enqueued this run
    # (async collection, migration 0015). NULL for CLI / legacy sync runs.
    job_id: Mapped[UUID | None] = mapped_column(GUID())


class AuditEventORM(Base):
    """Append-only audit log (V1 security feature, migration 0010).

    Records "who did what when" for the privileged operations. The
    metadata field is a strict JSONB dict: counts, durations, rule
    names, region names. NEVER raw account_id, ARN, tag values, or
    any other customer-identifying field — the AuditLogger enforces
    this on the Python side, the database trusts us.
    """

    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONBType(), nullable=False, default=dict
    )


class RetentionPolicyORM(Base):
    """Per-tenant retention policy (V1 security feature, migration 0010).

    One row per (tenant_id, table_name). Operators can override the
    retention_days per tenant in V2. The RetentionRunner picks
    enabled rows and deletes data older than retention_days.

    table_name is a free string validated by the RetentionRunner
    against an allow-list (we don't want an operator typo to wipe
    the wrong table).
    """

    __tablename__ = "retention_policies"

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    table_name: Mapped[str] = mapped_column(String, nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_deleted_count: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PIIClassificationORM(Base):
    """Per-field PII classification (V1 security feature, migration 0010).

    One row per (resource_type, resource_id, field_name). Records
    the field's sensitivity level + SHA-256 of the value. We store
    only the hash, never the value itself — the value lives in
    the source row (focus_charges.account_id, etc.) where the
    business logic needs it; here we just need to know
    "what's the sensitivity of this field on this resource?" for
    the privacy questionnaire.
    """

    __tablename__ = "pii_classifications"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    field_name: Mapped[str] = mapped_column(String, nullable=False)
    sensitivity: Mapped[str] = mapped_column(String, nullable=False)
    value_hash: Mapped[str] = mapped_column(String, nullable=False)
    classified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
