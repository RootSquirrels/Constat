"""Aggregate raw FOCUS 1.0 charges into one row per (service, period).

Pure logic — no DB, no I/O. Dedup key: (service, period_start, period_end)
for a given account. resource_id and region are collapsed via mode. Tags
are preserved as a list of unique tag dicts so the chargeback runner can
re-aggregate by any tag key.
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
    # All unique tag dicts seen across the input rows for this (service, period).
    # Empty list when no row carried tags. The list preserves per-row data
    # even when the underlying tag values are heterogeneous.
    tags: list[dict[str, str]]


def _mode(values: list[str]) -> str | None:
    """Return the most common value, or None if all are None/empty."""
    non_empty = [v for v in values if v]
    if not non_empty:
        return None
    counter = Counter(non_empty)
    return counter.most_common(1)[0][0]


def _unique_dicts(
    values: list[list[dict[str, str]]] | list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return the list of unique non-empty tag dicts, preserving first-seen order.

    Accepts either a flat `list[dict]` (per-row tag lists merged) or a
    `list[list[dict]]` (a list of per-row tag lists). The latter is what
    we get from `FocusCharge.tags` (each row has its own list of dicts).

    Used for FOCUS Tags: heterogeneous tag values across rows must all be
    preserved so the chargeback runner can re-aggregate by any tag key.
    Dicts are compared by value (frozenset of items).
    """
    seen: set[frozenset[tuple[str, str]]] = set()
    out: list[dict[str, str]] = []
    for v in values:
        # Normalize: if v is a list of dicts, iterate it; else treat v as one dict.
        items: list[dict[str, str]] = v if isinstance(v, list) else [v]  # type: ignore[list-item]
        for d in items:
            if not d:
                continue
            key = frozenset(d.items())
            if key not in seen:
                seen.add(key)
                out.append(d)
    return out


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
                tags=_unique_dicts([r.tags for r in rows]),
            )
        )
    return results
