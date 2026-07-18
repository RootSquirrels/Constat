"""Retention policy runner (V1 security feature, migration 0010).

Per-table automatic deletion. The first thing a SOC2 / GDPR
questionnaire asks: "do you delete data automatically, on a
schedule, with proof?". This module is the proof.

Defaults: see DEFAULT_RETENTION_DAYS below. These are seeded
on first boot. Operators can override per-row in the
retention_policies table.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from constat_api.orm import (
    InconclusiveORM,
    InsightORM,
    ObservationORM,
    RetentionPolicyORM,
    SourceRunORM,
)
from constat_api.settings import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)


# Default retention windows (in days). V1 single-tenant. The
# RetentionRunner seeds these on first boot via seed_default_policies.
#
# Rationale:
# - observations: 90 days. Raw AWS payloads are the highest-PII-risk
#   data we store. 90 days is enough to debug a recent scan
#   discrepancy; beyond that, the source-of-truth in AWS is
#   authoritative anyway.
# - focus_charges: 365 days. FOCUS exports are customer billing
#   data, useful for trend analysis. 1 year covers a fiscal year.
# - insights / inconclusive: 730 days. Computed gaps; 2 years lets
#   a customer see "what was the state 2 years ago" for audit.
# - source_runs: 180 days. The metadata is low-value; the
#   resources_found count is the only long-tail useful field.
# - audit_events: 1825 days (5y). Compliance: ISO 27001 / SOC2
#   typically require 1-3 years; we go to 5y for headroom and
#   to satisfy longer contractual SLAs.

DEFAULT_RETENTION_DAYS: dict[str, int] = {
    "observations": 90,
    "focus_charges": 365,
    "insights": 730,
    "inconclusive": 730,
    "source_runs": 180,
    "audit_events": 1825,
}


# Whitelist of tables the RetentionRunner is allowed to issue
# DELETEs against. table_name from retention_policies is validated
# against this set before the DELETE runs. Defense in depth: a
# typo in the table_name should NOT wipe the wrong table.
ALLOWED_TABLES: frozenset[str] = frozenset(DEFAULT_RETENTION_DAYS.keys())


# Map table_name -> ORM model class. Used by the runner to issue
# the DELETE. Kept here (not in the database) so we control
# the SQL the runner can run.
_TABLE_MODELS: dict[str, Any] = {
    "observations": ObservationORM,
    "focus_charges": None,  # handled specially (no created_at column)
    "insights": InsightORM,
    "inconclusive": InconclusiveORM,
    "source_runs": SourceRunORM,
    "audit_events": None,  # handled specially (uses occurred_at)
}

# Which column on each model holds the creation timestamp. Used
# to compute the cutoff ("older than retention_days").
_TABLE_TIMESTAMP_COLUMN: dict[str, str] = {
    "observations": "ingested_at",
    "insights": "computed_at",
    "inconclusive": "computed_at",
    "source_runs": "started_at",
}


def seed_default_policies(session: Session) -> int:
    """Insert the default retention policies if missing.

    Returns the number of policies inserted. Idempotent: re-running
    on a populated table is a no-op.
    """
    inserted = 0
    for table_name, days in DEFAULT_RETENTION_DAYS.items():
        existing = session.execute(
            select(RetentionPolicyORM).where(
                RetentionPolicyORM.tenant_id == DEFAULT_TENANT_ID,
                RetentionPolicyORM.table_name == table_name,
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                RetentionPolicyORM(
                    tenant_id=DEFAULT_TENANT_ID,
                    table_name=table_name,
                    retention_days=days,
                    enabled=True,
                )
            )
            inserted += 1
    if inserted:
        session.commit()
    return inserted


def apply_retention(
    session: Session,
    *,
    table_name: str,
    retention_days: int,
    tenant_id: Any = DEFAULT_TENANT_ID,
) -> int:
    """Delete rows from `table_name` older than `retention_days` for
    the given tenant. Returns the number of rows deleted.

    Validates `table_name` against ALLOWED_TABLES. Unknown or
    disallowed tables raise ValueError — better than wiping the
    wrong table.
    """
    if table_name not in ALLOWED_TABLES:
        raise ValueError(
            f"unknown or disallowed table for retention: {table_name!r}. "
            f"Allowed: {sorted(ALLOWED_TABLES)}"
        )
    if retention_days < 0:
        raise ValueError(f"retention_days must be >= 0, got {retention_days}")

    cutoff = datetime.now(tz=UTC) - timedelta(days=retention_days)

    # Special cases: tables without a created_at-style column, or
    # with a non-default timestamp column.
    if table_name == "focus_charges":
        # We don't track creation time on focus_charges; we have
        # `ingested_at`. Use that as the cutoff. (For V2 we'd add
        # a created_at column or use period_end.)
        from constat_api.orm import FocusChargeORM

        stmt = delete(FocusChargeORM).where(
            FocusChargeORM.tenant_id == tenant_id,
            FocusChargeORM.ingested_at < cutoff,
        )
    elif table_name == "audit_events":
        from constat_api.orm import AuditEventORM

        stmt = delete(AuditEventORM).where(
            AuditEventORM.tenant_id == tenant_id,
            AuditEventORM.occurred_at < cutoff,
        )
    else:
        model = _TABLE_MODELS[table_name]
        ts_col = _TABLE_TIMESTAMP_COLUMN[table_name]
        col = getattr(model, ts_col)
        stmt = delete(model).where(
            model.tenant_id == tenant_id,
            col < cutoff,
        )

    result = session.execute(stmt)
    return int(result.rowcount or 0)


def apply_all_enabled(session: Session) -> dict[str, int]:
    """Apply retention for every enabled policy. Returns a dict of
    {table_name: deleted_count}. Used by the CLI / endpoint.

    Updates the policy's last_applied_at and last_deleted_count for
    the operator's audit trail.
    """
    policies = (
        session.execute(
            select(RetentionPolicyORM).where(
                RetentionPolicyORM.tenant_id == DEFAULT_TENANT_ID,
                RetentionPolicyORM.enabled.is_(True),
            )
        )
        .scalars()
        .all()
    )

    results: dict[str, int] = {}
    for policy in policies:
        try:
            deleted = apply_retention(
                session,
                table_name=policy.table_name,
                retention_days=policy.retention_days,
                tenant_id=policy.tenant_id,
            )
        except ValueError as exc:
            # Skip disallowed table names rather than blowing up the
            # whole run. The policy row is broken; mark it disabled
            # so the operator notices on the next review.
            logger.warning("Skipping retention for %s: %s", policy.table_name, exc)
            policy.enabled = False
            results[policy.table_name] = -1
            continue

        policy.last_applied_at = datetime.now(tz=UTC)
        policy.last_deleted_count = deleted
        results[policy.table_name] = deleted

    session.commit()
    return results
