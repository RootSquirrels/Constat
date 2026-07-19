"""Insights repository."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from constat_core.models import Insight, Severity
from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.orm import InsightORM
from constat_api.tenant import tenant_or_default


def _orm_to_pydantic(orm: InsightORM) -> Insight:
    return Insight(
        id=orm.id,
        rule_name=orm.rule_name,
        resource_id=orm.resource_id,
        account_id=str(orm.account_id) if orm.account_id else None,
        severity=Severity(orm.severity),
        title=orm.title,
        payload=orm.payload,
        computed_at=orm.computed_at,
        ack_status=orm.ack_status,
        ack_at=orm.ack_at,
        ack_by=orm.ack_by,
    )


def list_insights(
    session: Session,
    *,
    rule_name: str | None = None,
    severity: Severity | None = None,
    account_id: UUID | None = None,
    ack_status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Insight]:
    """List current insights, newest first. Filters are optional.

    `ack_status='open'` is a virtual value: it filters to
    `ack_status IS NULL`. Any other value matches the column
    directly. The router validates the value before calling.
    """
    stmt = select(InsightORM).order_by(InsightORM.computed_at.desc())
    if rule_name is not None:
        stmt = stmt.where(InsightORM.rule_name == rule_name)
    if severity is not None:
        stmt = stmt.where(InsightORM.severity == severity.value)
    if account_id is not None:
        stmt = stmt.where(InsightORM.account_id == account_id)
    if ack_status is not None:
        if ack_status == "open":
            stmt = stmt.where(InsightORM.ack_status.is_(None))
        elif ack_status in ACK_STATUSES:
            stmt = stmt.where(InsightORM.ack_status == ack_status)
        else:
            # Router validates; this is defense in depth.
            raise ValueError(
                f"invalid ack_status {ack_status!r}; must be 'open' or one of {sorted(ACK_STATUSES)}"
            )
    stmt = stmt.limit(limit).offset(offset)
    return [_orm_to_pydantic(row) for row in session.execute(stmt).scalars()]


def get_insight(session: Session, insight_id: UUID) -> Insight | None:
    orm = session.get(InsightORM, insight_id)
    return _orm_to_pydantic(orm) if orm else None


def insert_insight(session: Session, insight: Insight) -> Insight:
    """Insert one insight. The caller owns the transaction."""
    orm = InsightORM(
        id=insight.id or uuid4(),
        tenant_id=tenant_or_default(session),
        rule_name=insight.rule_name,
        resource_id=insight.resource_id,
        account_id=UUID(insight.account_id) if insight.account_id else None,
        severity=insight.severity.value,
        title=insight.title,
        payload=insight.payload,
        computed_at=insight.computed_at,
        # ack_* fields are operator-driven, never set on insert.
        ack_status=None,
        ack_at=None,
        ack_by=None,
    )
    session.add(orm)
    session.flush()
    return _orm_to_pydantic(orm)


def delete_insights_for_rule(session: Session, rule_name: str) -> int:
    """Delete all insights for a rule. Returns the number of rows deleted.

    Audit F-03: the runner uses delete-and-replace semantics — each run
    starts by clearing the rule's previous insights so re-runs don't
    accumulate duplicates. The caller owns the transaction.
    """
    from sqlalchemy import delete as sa_delete

    stmt = sa_delete(InsightORM).where(InsightORM.rule_name == rule_name)
    result = session.execute(stmt)
    return int(result.rowcount or 0)


def count_insights(session: Session, *, rule_name: str | None = None) -> int:
    from sqlalchemy import func as sa_func

    stmt = select(sa_func.count(InsightORM.id))
    if rule_name is not None:
        stmt = stmt.where(InsightORM.rule_name == rule_name)
    return int(session.execute(stmt).scalar_one())


# Acknowledged ack_status values. Defined once so the router and
# tests share the truth source. Pydantic-side validation is in the
# PATCH body model; this list is the canonical set.
ACK_STATUSES: frozenset[str] = frozenset({"acknowledged", "in_progress", "resolved", "dismissed"})


def update_ack(
    session: Session,
    insight_id: UUID,
    *,
    ack_status: str,
    ack_by: str | None = None,
    ack_at: datetime | None = None,
) -> Insight | None:
    """Update the operator-acknowledgment fields on one insight.

    Server-set semantics: `ack_at` is set to `datetime.now(UTC)` if
    the caller doesn't pass it. The caller is the PATCH endpoint,
    which is the only path that should ever write these fields.

    Returns the updated Insight, or None if the insight doesn't exist
    (the row is not visible across tenants, so None also covers
    "wrong tenant").
    """
    from datetime import UTC, datetime

    from constat_api.orm import InsightORM

    if ack_status not in ACK_STATUSES:
        raise ValueError(
            f"invalid ack_status {ack_status!r}; must be one of {sorted(ACK_STATUSES)}"
        )

    orm = session.get(InsightORM, insight_id)
    if orm is None:
        return None

    orm.ack_status = ack_status
    orm.ack_by = ack_by
    orm.ack_at = ack_at if ack_at is not None else datetime.now(tz=UTC)
    session.flush()
    return _orm_to_pydantic(orm)


# ----------------------------------------------------------------------------
# Ack carry-over across delete-and-replace
# ----------------------------------------------------------------------------
#
# The runner wipes the rule's `insights` rows on every run (audit F-03),
# which also wipes the operator's `ack_status` / `ack_at` / `ack_by`
# fields. The lifecycle log (insight_events) preserves the appeared/
# resolved history via fingerprint, but the operator's *decision* on the
# current gap is lost on every re-run.
#
# The fix: carry the ack over to the fresh insight by **stable_id** —
# the gap's identity, not its display string. The fingerprint (which
# hashes the title) is the lifecycle log's key and stays as-is; it
# changes daily for EOL rules because the title embeds `days_to_eol`.
# The stable_id is what survives the title churn, and the doc's point
# 2 ("ne jamais effacer les décisions humaines") is satisfied as long
# as the underlying gap is unchanged.
#
# What "same gap" means:
#   - Resource rules: same (rule_name, resource_id). Each resource
#     emits at most one insight per rule in V1, and the rule's view
#     of the resource is the gap (EOL countdown, EOL passed, gp2 vs
#     gp3, ...). The amount/severity may change; the ack still belongs
#     to "this resource under this rule".
#   - Chargeback: same (account_id, service, period_label, tag_key,
#     tag_value) read from the payload. The drift amount is the value
#     being measured, not the identity — it goes in the title
#     (dynamic) but not in the stable_id.
#
# When the gap genuinely closes (e.g. PG11 -> PG14, resource retired,
# chargeback bucket emptied), the old insight is gone after the next
# run and the ack is correctly NOT carried over (no fresh insight to
# apply it to). The lifecycle log records the closure as a `resolved`
# event with the last known amount — the CFO-facing "money recovered".


def stable_id_of(
    rule_name: str,
    resource_id: UUID | None,
    account_id: str | None,
    payload: dict[str, Any] | None,
) -> str:
    """The gap identity used to carry operator acks across delete-and-replace.

    Distinct from the lifecycle fingerprint (which hashes the title and
    therefore changes when display-only fields change). Stable across
    daily re-runs, catalog updates that shift countdown numbers, and
    pricing-tier transitions — the operator's ack survives those.

    Args:
        rule_name: the rule that emitted the insight.
        resource_id: the FK on `insights.resource_id` (None for
            chargeback, which is account-scoped).
        account_id: the FK on `insights.account_id` (always set; the
            schema CHECK requires `resource_id OR account_id`).
        payload: the insight's payload dict; chargeback reads service /
            period / tag fields from here.
    """
    if resource_id is not None:
        return f"resource:{rule_name}:{resource_id}"
    p = payload or {}
    service = p.get("service", "")
    period_label = p.get("period_label", "")
    tag_key = p.get("tag_key") or ""
    tag_value = p.get("tag_value") or "UNTAGGED"
    return f"chargeback:{account_id}:{service}:{period_label}:{tag_key}:{tag_value}"


def _ack_snapshot_key(orm: InsightORM) -> str:
    return stable_id_of(
        orm.rule_name,
        orm.resource_id,
        str(orm.account_id) if orm.account_id else None,
        orm.payload or {},
    )


def snapshot_acks(session: Session, rule_name: str) -> dict[str, tuple[str, datetime, str | None]]:
    """Capture every acked insight of the rule, keyed by stable_id.

    Run by the runner BEFORE the per-rule delete, so the carry-over
    target (the fresh insight) can pick up the ack after insert. Only
    acked rows (ack_status IS NOT NULL) are captured; unacked rows
    have nothing to carry.

    Returns {stable_id: (ack_status, ack_at, ack_by)}. The runner
    passes the dict straight to `apply_acks_to_rule` after the fresh
    inserts.
    """
    rows = session.execute(
        select(InsightORM).where(
            InsightORM.rule_name == rule_name,
            InsightORM.ack_status.is_not(None),
        )
    ).scalars()
    return {_ack_snapshot_key(r): (r.ack_status, r.ack_at, r.ack_by) for r in rows}


def apply_acks_to_rule(
    session: Session,
    rule_name: str,
    snapshot: dict[str, tuple[str, datetime, str | None]],
) -> int:
    """Carry the snapshotted acks over to the rule's fresh insights.

    For each current insight with `ack_status IS NULL` (the fresh
    inserts from the runner), look up its stable_id in the snapshot
    and copy `ack_status` / `ack_at` / `ack_by`. Returns the number
    of insights that received an ack. A snapshot that doesn't match
    any current insight (e.g. the gap closed and the new run produced
    no insight) is silently ignored — the carry-over is a no-op, not
    an error.

    The caller (the runner) owns the transaction. The acks land in
    the same commit as the fresh insights, so a failed run leaves no
    half-acked rows.
    """
    if not snapshot:
        return 0
    rows = session.execute(
        select(InsightORM).where(
            InsightORM.rule_name == rule_name,
            InsightORM.ack_status.is_(None),
        )
    ).scalars()
    applied = 0
    for r in rows:
        carried = snapshot.get(_ack_snapshot_key(r))
        if carried is None:
            continue
        ack_status, ack_at, ack_by = carried
        r.ack_status = ack_status
        r.ack_at = ack_at
        r.ack_by = ack_by
        applied += 1
    if applied:
        session.flush()
    return applied
