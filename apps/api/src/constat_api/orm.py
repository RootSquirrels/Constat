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

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    external_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
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
    charge_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InsightORM(Base):
    __tablename__ = "insights"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical')", name="insights_severity_check"
        ),
        CheckConstraint(
            "resource_id IS NOT NULL OR account_id IS NOT NULL", name="insight_scope_present"
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
