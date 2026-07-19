"""ESTIMATED -> ACTUAL reconciliation against FOCUS (roadmap 2.3).

An estimate is "invoice-confirmed" when FOCUS has cost lines for the SAME
resource over a recent period. The match chain is:

    insights.resource_id -> resources.native_id  ==  focus_charges.resource_id

(native_id is the ARN for RDS, vol-xxx for EBS, snap-xxx for snapshots,
i-xxx for EC2 instances — the same string FOCUS carries as ResourceId.)

When a match exists, the insight's payload is updated IN PLACE (dict merge):
`focus_confirmed: true`, `focus_actual_monthly_usd` (the resource's actual
cost over the latest available FOCUS period, monthly-normalized),
`focus_period` (label), and `value_basis: "ACTUAL"`. The MonetaryKind never
changes (ADR-13) — a confirmed saving stays a saving, better evidenced.
Insights with no matching FOCUS line stay ESTIMATED; nothing is written.

chargeback is out of scope: it is ACTUAL by construction (built FROM focus
lines), so the function returns 0 for it immediately.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from constat_core.monetary import MONETARY, ValueBasis
from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.orm import FocusChargeORM, InsightORM, ResourceORM

logger = logging.getLogger(__name__)

# FOCUS ServiceName values accepted as proof per rule. A rule only trusts
# cost lines from its own service: an RDS EOL estimate must be confirmed by
# RDS billing lines, not by an unrelated service that happens to reference
# the same resource id (rare but possible with reused native ids). A rule
# absent from this map accepts lines from any service.
RULE_FOCUS_SERVICES: dict[str, str] = {
    "rds_eol": "Amazon Relational Database Service",
    "mysql_eol": "Amazon Relational Database Service",
    "aurora_eol": "Amazon Relational Database Service",
    "ebs_gp2_to_gp3": "Amazon Elastic Compute Cloud - Compute",
    "ebs_unattached": "Amazon Elastic Compute Cloud - Compute",
    "snapshot_orphan": "Amazon Elastic Compute Cloud - Compute",
    "ec2_stopped_with_storage": "Amazon Elastic Compute Cloud - Compute",
}

# Monthly normalization horizon: a cost over N days becomes cost / N * 30.
DAYS_IN_MONTH = 30


def _latest_period_lines(
    session: Session, native_id: str, service: str | None
) -> list[FocusChargeORM]:
    """FOCUS lines for this native_id in the latest period available.

    "Latest" is keyed on period_end; all lines sharing that period_end are
    returned (a resource can produce several lines in one period — e.g.
    instance + storage). Their costs are summed by the caller.
    """
    stmt = select(FocusChargeORM).where(FocusChargeORM.resource_id == native_id)
    if service is not None:
        stmt = stmt.where(FocusChargeORM.service == service)
    rows = list(session.execute(stmt).scalars())
    if not rows:
        return []
    latest_end = max(r.period_end for r in rows)
    return [r for r in rows if r.period_end == latest_end]


def reconcile_with_focus(session: Session, rule_name: str) -> int:
    """Confirm fresh insights of `rule_name` against FOCUS. Returns the count.

    Called at the end of run_resource_rule, inside the run transaction, after
    the fresh insights are inserted. Only touches insights that carry the
    rule's registered (ESTIMATED) monetary payload key.
    """
    entry = MONETARY.get(rule_name)
    if entry is None or entry.value_basis != ValueBasis.ESTIMATED:
        # chargeback is ACTUAL by construction; nothing to confirm.
        return 0

    insights = list(
        session.execute(select(InsightORM).where(InsightORM.rule_name == rule_name)).scalars()
    )
    service = RULE_FOCUS_SERVICES.get(rule_name)
    confirmed = 0

    for insight in insights:
        raw = insight.payload.get(entry.payload_key)
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            continue  # no estimate to confirm (e.g. inconclusive-shaped payload)
        if insight.resource_id is None:
            continue  # account-level insight: no resource to match on
        resource = session.get(ResourceORM, insight.resource_id)
        if resource is None:
            continue

        lines = _latest_period_lines(session, resource.native_id, service)
        if not lines:
            continue  # no FOCUS line for this resource: stays ESTIMATED

        period_start = min(r.period_start for r in lines)
        period_end = max(r.period_end for r in lines)
        # Inclusive day count: a 2026-06-01..2026-06-30 line is a full
        # 30-day month, so its monthly cost normalizes to itself.
        days = (period_end - period_start).days + 1
        if days <= 0:
            continue
        # amortized_cost is the FOCUS EffectiveCost (see constat_focus.loader).
        total = sum((r.amortized_cost for r in lines), Decimal("0"))
        monthly = float(total) / days * DAYS_IN_MONTH

        insight.payload = {
            **insight.payload,
            "focus_confirmed": True,
            "focus_actual_monthly_usd": monthly,
            "focus_period": f"{period_start.isoformat()}..{period_end.isoformat()}",
            "value_basis": ValueBasis.ACTUAL.value,
        }
        confirmed += 1

    if confirmed:
        session.flush()
        logger.info("reconcile: %s — %d insight(s) confirmed against FOCUS", rule_name, confirmed)
    return confirmed
