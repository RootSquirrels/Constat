"""FOCUS cost context for ESTIMATED insights (audit committee fix).

The audit committee (FinOps re-audit) flagged the prior
ESTIMATED -> ACTUAL flip as unsound. The FOCUS line for a resource
is its TOTAL billed cost (the whole DB instance, the whole volume),
not the specific cost component the rule is pricing (the Extended
Support supplement, the gp2-to-gp3 savings). Flipping the label
to ACTUAL on a total-vs-component match misrepresents the data.

V1 behavior: this function still attaches the FOCUS context
(`focus_confirmed`, `focus_resource_monthly_usd`, `focus_period`)
so an operator can see "the rule's estimate and the resource's
FOCUS line for the same period" side by side, but the
`value_basis` is never changed. The label stays ESTIMATED for
every rule. The basis reflects the rule's registered value_basis
(see packages/core/constat_core/monetary.py), which is ESTIMATED
for every V1 rule.

V2: a per-charge-type matcher (e.g. FOCUS line description contains
"Extended Support") will be able to link a rule's amount to a
specific FOCUS component. When that matcher is in place, this
function can promote the matching slice to ACTUAL — at the
component level, not the resource level. Until then, no ACTUAL.

chargeback is also ESTIMATED in V1: its drift is a derived signal
(amortized minus billed), not a FOCUS line itself.

The match chain (still useful for context, not for ACTUAL):

    insights.resource_id -> resources.native_id  ==  focus_charges.resource_id

(native_id is the ARN for RDS, vol-xxx for EBS, snap-xxx for snapshots,
i-xxx for EC2 instances — the same string FOCUS carries as ResourceId.)
"""

from __future__ import annotations

import logging
from decimal import Decimal

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
    """Attach FOCUS context to fresh insights of `rule_name`. Returns the count.

    Called at the end of run_resource_rule, inside the run transaction, after
    the fresh insights are inserted. Touches only insights that have a
    numeric monetary payload key (the registered one) AND a resource_id we
    can match against FOCUS.

    V1: never flips value_basis. Adds `focus_confirmed`, `focus_resource_monthly_usd`,
    `focus_period`, and `focus_billing_currency` to the payload so the
    restitution can show the rule's estimate next to the resource's
    FOCUS cost for the same period. The basis stays ESTIMATED — see
    the module docstring for the audit committee's rationale.
    """
    from constat_core.monetary import MONETARY

    entry = MONETARY.get(rule_name)
    if entry is None:
        return 0  # non-monetary rule, nothing to attach context to

    insights = list(
        session.execute(select(InsightORM).where(InsightORM.rule_name == rule_name)).scalars()
    )
    service = RULE_FOCUS_SERVICES.get(rule_name)
    contextualized = 0

    for insight in insights:
        raw = insight.payload.get(entry.payload_key)
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            continue  # no estimate to contextualize
        if insight.resource_id is None:
            continue  # account-level insight: no resource to match on
        resource = session.get(ResourceORM, insight.resource_id)
        if resource is None:
            continue

        lines = _latest_period_lines(session, resource.native_id, service)
        if not lines:
            continue  # no FOCUS line for this resource: nothing to add

        period_start = min(r.period_start for r in lines)
        period_end = max(r.period_end for r in lines)
        # Inclusive day count: a 2026-06-01..2026-06-30 line is a full
        # 30-day month, so its monthly cost normalizes to itself.
        days = (period_end - period_start).days + 1
        if days <= 0:
            continue
        # amortized_cost is the FOCUS EffectiveCost (see constat_focus.loader).
        # All lines for one (account, resource) at one period share the
        # same billing currency (the loader refuses mixed-currency input
        # — migration 0019). Use the first line's currency.
        total = sum((r.amortized_cost for r in lines), Decimal("0"))
        monthly = float(total) / days * DAYS_IN_MONTH
        currency = lines[0].billing_currency

        # value_basis is NOT changed: stays ESTIMATED. The audit committee
        # flagged the prior flip as unsound (the FOCUS line is the
        # resource's total cost, not the rule's specific cost component).
        # The FOCUS context below is informational; the operator can see
        # the estimate and the FOCUS line side by side.
        insight.payload = {
            **insight.payload,
            "focus_confirmed": True,
            "focus_resource_monthly_usd": monthly,
            "focus_period": f"{period_start.isoformat()}..{period_end.isoformat()}",
            "focus_billing_currency": currency,
        }
        contextualized += 1

    if contextualized:
        session.flush()
        logger.info(
            "reconcile: %s — %d insight(s) contextualized against FOCUS",
            rule_name,
            contextualized,
        )
    return contextualized
