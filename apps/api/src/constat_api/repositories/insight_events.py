"""Insight lifecycle events repository (roadmap 2.4).

The runner's delete-and-replace destroys the insights table each run; the
appeared/resolved history is rebuilt here by diffing fingerprints before
and after the run. `insight_events` is append-only — the runner inserts,
nothing updates or deletes (retention is a separate concern, V2).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID

from constat_core.monetary import monthly_cost_and_basis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from constat_api.orm import InsightEventORM, InsightORM
from constat_api.tenant import tenant_or_default

EVENTS: frozenset[str] = frozenset({"appeared", "resolved"})


def fingerprint_of(rule_name: str, resource_id: UUID | None, title: str) -> str:
    """Stable identity of an insight across delete-and-replace runs.

    sha256 of rule_name|resource_id|title (hex). resource_id is "" for
    account-level insights (chargeback), where the title carries the
    account/service/period identity.
    """
    raw = f"{rule_name}|{resource_id or ''}|{title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def snapshot_rule(session: Session, rule_name: str) -> dict[str, InsightORM]:
    """Fingerprint -> current insight row, for every insight of the rule.

    Called by the runner BEFORE the per-rule delete; the returned dict is
    the "old" state the post-run diff compares against.
    """
    rows = session.execute(select(InsightORM).where(InsightORM.rule_name == rule_name)).scalars()
    return {fingerprint_of(r.rule_name, r.resource_id, r.title): r for r in rows}


def _monthly_of(rule_name: str, row: InsightORM) -> float | None:
    monthly, _basis = monthly_cost_and_basis(rule_name, row.payload)
    return monthly


def diff_and_record_events(
    session: Session,
    *,
    rule_name: str,
    previous: dict[str, InsightORM],
    insight_run_id: UUID | None,
) -> tuple[int, int]:
    """Diff current insights against `previous` and insert appeared/resolved events.

    Returns (appeared, resolved). The caller owns the transaction (events
    land in the same commit as the fresh insights — a failed run leaves no
    half-written history).

    - fresh-not-in-old -> `appeared`, with the fresh monthly amount.
    - old-not-in-fresh -> `resolved`, with the OLD monthly amount (the
      "money recovered" when the gap closed). A resource that disappeared
      entirely (retired) also resolves this way: the gap is gone, whatever
      the cause — that is the CFO-facing truth.
    """
    current = snapshot_rule(session, rule_name)
    appeared = 0
    resolved = 0
    # Stamped once per run: both event kinds belong to the session's
    # tenant (RLS WITH CHECK rejects the ORM default otherwise).
    tenant_id = tenant_or_default(session)

    for fp, row in current.items():
        if fp in previous:
            continue
        session.add(
            InsightEventORM(
                tenant_id=tenant_id,
                fingerprint=fp,
                rule_name=rule_name,
                resource_id=row.resource_id,
                account_id=str(row.account_id) if row.account_id else None,
                title=row.title,
                event="appeared",
                monthly_usd=_monthly_of(rule_name, row),
                insight_run_id=insight_run_id,
            )
        )
        appeared += 1

    for fp, row in previous.items():
        if fp in current:
            continue
        session.add(
            InsightEventORM(
                tenant_id=tenant_id,
                fingerprint=fp,
                rule_name=rule_name,
                resource_id=row.resource_id,
                account_id=str(row.account_id) if row.account_id else None,
                title=row.title,
                event="resolved",
                monthly_usd=_monthly_of(rule_name, row),
                insight_run_id=insight_run_id,
            )
        )
        resolved += 1

    if appeared or resolved:
        session.flush()
    return appeared, resolved


def _filtered(stmt, *, rule_name, since, event):
    if rule_name is not None:
        stmt = stmt.where(InsightEventORM.rule_name == rule_name)
    if since is not None:
        stmt = stmt.where(InsightEventORM.occurred_at >= since)
    if event is not None:
        stmt = stmt.where(InsightEventORM.event == event)
    return stmt


def list_events(
    session: Session,
    *,
    rule_name: str | None = None,
    since: datetime | None = None,
    event: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[InsightEventORM]:
    """Events, newest first. The router validates `event` before calling."""
    stmt = _filtered(
        select(InsightEventORM), rule_name=rule_name, since=since, event=event
    ).order_by(InsightEventORM.occurred_at.desc())
    stmt = stmt.limit(limit).offset(offset)
    return list(session.execute(stmt).scalars())


def summarize_events(
    session: Session,
    *,
    rule_name: str | None = None,
    since: datetime | None = None,
    event: str | None = None,
) -> dict[str, float | int]:
    """The "€ récupérés" seed: counts + the resolved monthly total.

    Computed over the WHOLE filtered set (not the page returned by
    list_events): the summary is the headline figure, the list is the proof.
    """
    stmt = _filtered(
        select(
            InsightEventORM.event,
            func.count(),
            func.coalesce(func.sum(InsightEventORM.monthly_usd), 0.0),
        ).group_by(InsightEventORM.event),
        rule_name=rule_name,
        since=since,
        event=event,
    )
    summary: dict[str, float | int] = {
        "appeared_count": 0,
        "resolved_count": 0,
        "resolved_monthly_usd_total": 0.0,
    }
    for event_name, count, monthly_sum in session.execute(stmt):
        if event_name == "appeared":
            summary["appeared_count"] = int(count)
        elif event_name == "resolved":
            summary["resolved_count"] = int(count)
            summary["resolved_monthly_usd_total"] = float(monthly_sum)
    return summary
