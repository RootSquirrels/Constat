"""Chargeback insight: aggregate FOCUS 1.0 charges per (account, service, period).

The FOCUS loader yields `FocusCharge` rows. This module groups them, computes
the amortized-vs-billed drift, and emits one Insight per (account, service, period).

V1: per-period aggregation = monthly trends. V2 will add tag-based
aggregation (Application, CostCenter, etc.) — same pattern, just more
grouping keys.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from constat_core.models import Insight, Severity
from constat_focus.loader import FocusCharge

RULE_NAME = "chargeback"

# Drift thresholds (USD/month per service) for severity escalation.
# Tunable. Calibrate against real prospect data in the first G0 run.
SEVERITY_WARNING_USD = Decimal("100")
SEVERITY_CRITICAL_USD = Decimal("1000")


@dataclass(frozen=True)
class AggregatedCost:
    account_id: str
    service: str
    billed_cost: Decimal
    amortized_cost: Decimal
    charge_count: int
    # Period is optional for backward compat with the all-time aggregate.
    # When set, build_insights uses it in the title and payload.
    period_start: date | None = None
    period_end: date | None = None
    # V2: tag-based grouping (Application, CostCenter, ...). For V1: empty.
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def drift_amortized_minus_billed(self) -> Decimal:
        """Positive: user is being amortized UP (reservation/RI coverage gap).
        Negative: user is being amortized DOWN (refunds/credits)."""
        return self.amortized_cost - self.billed_cost


def aggregate(charges: Iterable[FocusCharge]) -> list[AggregatedCost]:
    """Group FOCUS charges by (account, service) — all periods summed.

    Kept for backward compat / V1 'all-time' view. V1 production code
    should prefer aggregate_by_period for monthly trends.
    """
    buckets: dict[tuple[str, str], list[FocusCharge]] = defaultdict(list)
    for c in charges:
        buckets[(c.account_id, c.service)].append(c)

    results: list[AggregatedCost] = []
    for (account_id, service), rows in buckets.items():
        results.append(
            AggregatedCost(
                account_id=account_id,
                service=service,
                billed_cost=sum((c.billed_cost for c in rows), Decimal("0")),
                amortized_cost=sum((c.amortized_cost for c in rows), Decimal("0")),
                charge_count=len(rows),
            )
        )
    return results


def aggregate_by_period(
    charges: Iterable[FocusCharge],
) -> list[AggregatedCost]:
    """Group FOCUS charges by (account, service, period_start, period_end).

    One row per (account, service, billing period). Use this for monthly
    trends. V2 will add tag keys to the grouping tuple.
    """
    buckets: dict[tuple[str, str, date, date], list[FocusCharge]] = defaultdict(list)
    for c in charges:
        buckets[(c.account_id, c.service, c.period_start, c.period_end)].append(c)

    results: list[AggregatedCost] = []
    for (account_id, service, ps, pe), rows in buckets.items():
        results.append(
            AggregatedCost(
                account_id=account_id,
                service=service,
                billed_cost=sum((r.billed_cost for r in rows), Decimal("0")),
                amortized_cost=sum((r.amortized_cost for r in rows), Decimal("0")),
                charge_count=len(rows),
                period_start=ps,
                period_end=pe,
            )
        )
    return results


def _severity_for_drift(drift: Decimal) -> Severity:
    abs_drift = abs(drift)
    if abs_drift >= SEVERITY_CRITICAL_USD:
        return Severity.CRITICAL
    if abs_drift >= SEVERITY_WARNING_USD:
        return Severity.WARNING
    return Severity.INFO


def _period_label(agg: AggregatedCost) -> str:
    """Render the period as a human-readable label."""
    if agg.period_start and agg.period_end:
        return f"{agg.period_start.isoformat()} → {agg.period_end.isoformat()}"
    return "all-time"


def build_insights(
    aggregated: Iterable[AggregatedCost], *, period_label: str = ""
) -> list[Insight]:
    """Convert aggregated costs into Insights.

    When the AggregatedCost has period info, the title includes the period
    (e.g. 'AmazonRDS on 111 (2026-07-01 → 2026-07-31)'). The payload always
    includes period_start/period_end as ISO strings (or null for all-time).
    """
    insights: list[Insight] = []
    for agg in aggregated:
        drift = agg.drift_amortized_minus_billed
        severity = _severity_for_drift(drift)
        direction = "up" if drift > 0 else "down" if drift < 0 else "flat"
        label = _period_label(agg) if period_label == "" else period_label

        title = (
            f"{agg.service} on {agg.account_id} ({label}): "
            f"amortized {direction} by ${abs(drift):.2f}"
        )

        insights.append(
            Insight(
                rule_name=RULE_NAME,
                resource_id=None,
                account_id=agg.account_id,
                severity=severity,
                title=title,
                payload={
                    "service": agg.service,
                    "account_id": agg.account_id,
                    "period_label": label,
                    "period_start": agg.period_start.isoformat() if agg.period_start else None,
                    "period_end": agg.period_end.isoformat() if agg.period_end else None,
                    "billed_cost_usd": float(agg.billed_cost),
                    "amortized_cost_usd": float(agg.amortized_cost),
                    "drift_amortized_minus_billed_usd": float(drift),
                    "charge_count": agg.charge_count,
                    "tags": dict(agg.tags),  # V2: tag-based grouping
                },
            )
        )
    return insights
