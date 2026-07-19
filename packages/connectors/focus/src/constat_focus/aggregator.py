"""Aggregate raw FOCUS 1.0 charges into one row per (service, period).

Pure logic — no DB, no I/O. Dedup key: (service, period_start, period_end)
for a given account. resource_id and region are collapsed via mode. Tags
are preserved as a list of unique tag dicts so the chargeback runner can
re-aggregate by any tag key.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from constat_focus.loader import FocusCharge


@dataclass(frozen=True)
class AggregatedFocusCharge:
    """One row ready to be written to focus_charges.

    V2 (migration 0009): the per-row tag data is preserved in
    `per_row_tag_dicts` so the upsert can write one focus_charge_tags
    row per (input row, key, value). This enables proportional cost
    attribution in the chargeback runner instead of V1's even split.

    Migration 0019: `billing_currency` is preserved as-written across
    the aggregation. The aggregator doesn't convert — all rows of a
    bucket must share the same currency; a mixed-currency bucket is
    refused loud (ValueError) because the storage natural key
    (account, service, period) cannot hold two currencies without
    summing them under one label.

    Migration 0020: `per_row_costs` is parallel to `per_row_tag_dicts`.
    Each tuple is (billed_cost, amortized_cost) for one input FOCUS
    row. The resolver uses (per_row_tag_dicts, per_row_costs) together
    to attribute cost per-input-row to its tag value (cost-weighted,
    not count-weighted). 3 EUR web + 97 EUR api -> 3% / 97%.
    """

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
    # Denormalized cache for the focus_charges.tags JSONB column.
    # Unique tag dicts seen across the input rows for this
    # (service, period). Kept for backward compat with V1 readers.
    tags: list[dict[str, str]]
    # V2: per-input-row tag dicts, in input order. Each element is
    # one input row's tag dict (the loader wraps a single dict in a
    # list per row). Length == number of input rows that contributed
    # to this aggregate. Used by the upsert to write focus_charge_tags
    # rows. Cross-row duplicates are preserved (intentional: the runner
    # uses the count to attribute cost proportionally).
    per_row_tag_dicts: list[dict[str, str]] = field(default_factory=list)
    # Migration 0020: per-input-row costs, parallel to per_row_tag_dicts.
    # Each tuple is (billed, amortized) for the same input row at the
    # same index. The upsert writes these to focus_charge_tags.billed_cost
    # and .amortized_cost, denormalized across all (key, value) rows
    # of the same input row (the resolver groups by input_row_index).
    per_row_costs: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    # ISO 4217 currency code, preserved as-written (USD, EUR, GBP, ...).
    # Same for all rows in the bucket — aggregate_for_storage raises on
    # a mixed-currency bucket before producing this row.
    billing_currency: str = "USD"


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
    """Group FocusCharge rows by (service, period_start, period_end) and sum costs.

    V2: also preserves per-input-row tag dicts (`per_row_tag_dicts`)
    so the upsert can write one focus_charge_tags row per input row.
    See migration 0009.

    Currency: the storage natural key is (account, service, period) —
    one row per bucket, one `billing_currency` column. A bucket that
    mixes currencies (e.g. an Azure EA export with an EUR-billed and a
    USD-billed subscription for the same service+period) CANNOT be
    stored without either summing unlike currencies under one label or
    silently overwriting one currency with the other. Both are the
    silent-FX-error class the audit committee flagged as a deal-breaker,
    so we refuse loud: ValueError with operator guidance. Ingest each
    currency under its own account id (e.g. `--account 87654321-eur`)
    until the storage key carries the currency.
    """
    buckets: dict[tuple[str, object, object], list[FocusCharge]] = defaultdict(list)
    for c in charges:
        buckets[(c.service, c.period_start, c.period_end)].append(c)

    results: list[AggregatedFocusCharge] = []
    for (service, ps, pe), rows in buckets.items():
        # Flatten per-input-row tag dicts AND costs. The loader wraps
        # each row's single tag dict in a list, and each row's cost in
        # a 1-tuple in per_row_costs. Flattening both gives parallel
        # lists: per_row_tag_dicts[i] is the tag dict of the same input
        # row as per_row_costs[i]. The resolver uses them together for
        # cost-weighted tag attribution.
        flat_tag_dicts: list[dict[str, str]] = []
        flat_per_row_costs: list[tuple[Decimal, Decimal]] = []
        for r in rows:
            # The lists are parallel: each input row contributes one
            # element to both. If the row had no tags, r.tags[0] is
            # {} (loader invariant). The per_row_costs list always has
            # the same length as the tags list.
            for t, cost in zip(r.tags, r.per_row_costs, strict=True):
                flat_tag_dicts.append(t)
                flat_per_row_costs.append(cost)

        # Fail loud on mixed currencies. The loader validates each row's
        # BillingCurrency but does NOT enforce a single currency per
        # file — an earlier version of this docstring claimed it did,
        # and the mode() below silently labeled the sum of EUR+USD with
        # the majority currency. Storage cannot hold two currencies for
        # one (service, period) bucket, so refuse instead of mislabeling.
        currencies = {r.billing_currency for r in rows}
        if len(currencies) > 1:
            raise ValueError(
                f"FOCUS bucket ({service!r}, {ps}..{pe}) mixes currencies "
                f"{sorted(currencies)}: V1 storage keys cost rows by "
                f"(account, service, period) and cannot hold two currencies "
                f"without summing them. Split the export per currency and "
                f"ingest each under its own account id."
            )
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
                per_row_tag_dicts=flat_tag_dicts,
                per_row_costs=flat_per_row_costs,
                billing_currency=currencies.pop() if currencies else "USD",
            )
        )
    return results
