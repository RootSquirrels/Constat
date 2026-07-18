"""Aggregate raw FOCUS 1.0 charges into one row per (service, period).

Pure logic — no DB, no I/O. Dedup key: (service, period_start, period_end)
for a given account. resource_id is collapsed via mode.
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
    charge_count: int
    region: str | None
    pricing_category: str | None
    resource_id: str | None
    sub_account_id: str | None


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
                charge_count=len(rows),
                region=_mode([r.region for r in rows]),
                pricing_category=_mode([r.pricing_category for r in rows]),
                resource_id=_mode([r.resource_id for r in rows]),
                sub_account_id=_mode([r.sub_account_id for r in rows]),
            )
        )
    return results
