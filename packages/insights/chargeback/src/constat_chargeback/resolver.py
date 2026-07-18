"""Chargeback insight: aggregate FOCUS 1.0 charges per (account, service).

The FOCUS loader yields `FocusCharge` rows. This module groups them, computes
the amortized-vs-billed drift, and emits one Insight per group with severity
based on the absolute drift amount.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
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

    @property
    def drift_amortized_minus_billed(self) -> Decimal:
        """Positive: user is being amortized UP (reservation/RI coverage gap).
        Negative: user is being amortized DOWN (refunds/credits)."""
        return self.amortized_cost - self.billed_cost


def aggregate(charges: Iterable[FocusCharge], *, period_label: str = "") -> list[AggregatedCost]:
    """Group FOCUS charges by (account, service) and sum costs."""
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


def _severity_for_drift(drift: Decimal) -> Severity:
    abs_drift = abs(drift)
    if abs_drift >= SEVERITY_CRITICAL_USD:
        return Severity.CRITICAL
    if abs_drift >= SEVERITY_WARNING_USD:
        return Severity.WARNING
    return Severity.INFO


def build_insights(
    aggregated: Iterable[AggregatedCost], *, period_label: str = ""
) -> list[Insight]:
    """Convert aggregated costs into Insights."""
    insights: list[Insight] = []
    for agg in aggregated:
        drift = agg.drift_amortized_minus_billed
        severity = _severity_for_drift(drift)
        direction = "up" if drift > 0 else "down" if drift < 0 else "flat"

        title = f"{agg.service} on {agg.account_id}: amortized {direction} by ${abs(drift):.2f}"

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
                    "period_label": period_label,
                    "billed_cost_usd": float(agg.billed_cost),
                    "amortized_cost_usd": float(agg.amortized_cost),
                    "drift_amortized_minus_billed_usd": float(drift),
                    "charge_count": agg.charge_count,
                },
            )
        )
    return insights
