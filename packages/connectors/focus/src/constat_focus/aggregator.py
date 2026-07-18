"""Aggregate raw FOCUS charges into one row per (service, period).

This is pure logic — no DB, no I/O. The dedup key is exactly
(service, period_start, period_end) for a given account.

Region and pricing_category are collapsed to the most common value
(modal) so a single row in focus_charges can serve chargeback queries.
The `charge_count` column records the number of source rows that went
into the aggregate, so drift is auditable.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from constat_focus.loader import FocusCharge


@dataclass(frozen=True)
class AggregatedFocusCharge:
    """One row ready to be written to focus_charges."""

    service: str
    period_start: object  # datetime.date — see import below
    period_end: object
    billed_cost: Decimal
    amortized_cost: Decimal
    effective_cost: Decimal
    charge_count: int
    region: str | None
    pricing_category: str | None


def _mode(values: list[str]) -> str | None:
    """Return the most common value, or None if all are None/empty."""
    non_empty = [v for v in values if v]
    if not non_empty:
        return None
    counter = Counter(non_empty)
    return counter.most_common(1)[0][0]


def aggregate_for_storage(charges: Iterable[FocusCharge]) -> list[AggregatedFocusCharge]:
    """Group FocusCharge rows by (service, period_start, period_end) and sum costs."""
    buckets: dict[tuple[str, object, object], list[FocusCharge]] = defaultdict(list)
    for c in charges:
        buckets[(c.service, c.period_start, c.period_end)].append(c)

    results: list[AggregatedFocusCharge] = []
    for (service, ps, pe), rows in buckets.items():
        results.append(
            AggregatedFocusCharge(
                service=service,
                period_start=ps,
                period_end=pe,
                billed_cost=sum((r.billed_cost for r in rows), Decimal("0")),
                amortized_cost=sum((r.amortized_cost for r in rows), Decimal("0")),
                effective_cost=sum((r.effective_cost for r in rows), Decimal("0")),
                charge_count=len(rows),
                region=_mode([r.region for r in rows]),
                pricing_category=_mode([r.pricing_category for r in rows]),
            )
        )
    return results
