"""Chargeback insight: aggregate FOCUS 1.0 charges per (account, service, period).

The FOCUS loader yields `FocusCharge` rows. This module groups them, computes
the amortized-vs-billed drift, and emits one Insight per grouping key.

Two grouping modes in V1, refined in V2:
- (account, service, period) — per-period monthly trend
- (account, service, period, tag_key_value) — per-tag breakdown

V2 tag aggregation (P3 item 11 fix): when a (service, period) row has
N input FOCUS rows, each carrying a tag dict, we count the rows per
(tag_value) and attribute cost proportionally. Replaces V1's even
split (1/N per unique value), which was wrong for heterogeneous tag
data. The per-row data lives in `focus_charge_tags` (migration 0009);
the runner reads it and exposes it via `FocusCharge.tags` as a flat
list of per-row tag dicts.

Migration 0020: per-row cost-weighted attribution. The V2 row-count
weighting was still wrong when costs were heterogeneous — a row of
3 EUR counted the same as a row of 97 EUR. Migration 0020 stores
per-input-row (billed_cost, amortized_cost) in focus_charge_tags, and
the resolver attributes each input row's own cost to its tag value.
3 EUR web + 97 EUR api -> 3 EUR web / 97 EUR api (3% / 97%), not
50 EUR / 50 EUR. The audit committee's deal-breaker was "an
attribution by tag that follows the number of lines rather than
their cost" — this is the fix.

Currency: every grouping includes `billing_currency` in the bucket
key. FOCUS 1.0 is provider-agnostic and a single export can mix
currencies (e.g. an Azure EA with an EUR-billed and a USD-billed
subscription). Summing EUR + USD into one number labeled "usd" is a
silent FX error, so each (account, service, period, currency) gets
its own insight, and the payload carries `billing_currency` so the
restitution never has to guess. The payload amount KEYS keep their
V1 `*_usd` suffix (registry-locked, see constat_core.monetary) —
the `billing_currency` field is the authoritative label.
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

# Sentinel tag value when a charge has no tag for the requested key.
# Surfaces in the insight title and payload so the user can see "untagged"
# spend separately from tagged spend.
UNTAGGED = "__untagged__"


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
    # All unique tag dicts seen across the input FocusCharge rows that
    # contributed to this aggregate. Empty list when no tags were present.
    # Used by `aggregate_by_tag` to re-aggregate by any tag key.
    tags: list[dict[str, str]] = field(default_factory=list)
    # Set when the cost is the result of a tag-based re-aggregation. The
    # tag_key is the user-chosen key (e.g. "Application"); tag_value is the
    # value the cost was attributed to (or UNTAGGED).
    tag_key: str | None = None
    tag_value: str | None = None
    # Human-readable account name (audit F-13). Empty when unknown;
    # build_insights falls back to account_id in the title.
    account_name: str = ""
    # ISO 4217 code shared by every charge in this aggregate (the
    # grouping is currency-aware, so one aggregate is always single-
    # currency). Surfaced in the insight payload as `billing_currency`.
    billing_currency: str = "USD"

    @property
    def drift_amortized_minus_billed(self) -> Decimal:
        """Positive: user is being amortized UP (reservation/RI coverage gap).
        Negative: user is being amortized DOWN (refunds/credits)."""
        return self.amortized_cost - self.billed_cost


def aggregate(charges: Iterable[FocusCharge]) -> list[AggregatedCost]:
    """Group FOCUS charges by (account, service, currency) — all periods summed.

    Kept for backward compat / V1 'all-time' view. V1 production code
    should prefer aggregate_by_period for monthly trends.
    """
    buckets: dict[tuple[str, str, str], list[FocusCharge]] = defaultdict(list)
    for c in charges:
        buckets[(c.account_id, c.service, c.billing_currency)].append(c)

    results: list[AggregatedCost] = []
    for (account_id, service, currency), rows in buckets.items():
        results.append(
            AggregatedCost(
                account_id=account_id,
                service=service,
                billed_cost=sum((c.billed_cost for c in rows), Decimal("0")),
                amortized_cost=sum((c.amortized_cost for c in rows), Decimal("0")),
                charge_count=len(rows),
                tags=_merge_tag_lists(rows),
                account_name=_first_account_name(rows),
                billing_currency=currency,
            )
        )
    return results


def aggregate_by_period(
    charges: Iterable[FocusCharge],
) -> list[AggregatedCost]:
    """Group FOCUS charges by (account, service, period_start, period_end, currency).

    One row per (account, service, billing period, currency). Use this for monthly
    trends. The `tags` field carries every unique tag dict seen in the
    contributing rows, ready for downstream tag-based re-aggregation.
    """
    buckets: dict[tuple[str, str, date, date, str], list[FocusCharge]] = defaultdict(list)
    for c in charges:
        key = (c.account_id, c.service, c.period_start, c.period_end, c.billing_currency)
        buckets[key].append(c)

    results: list[AggregatedCost] = []
    for (account_id, service, ps, pe, currency), rows in buckets.items():
        results.append(
            AggregatedCost(
                account_id=account_id,
                service=service,
                billed_cost=sum((r.billed_cost for r in rows), Decimal("0")),
                amortized_cost=sum((r.amortized_cost for r in rows), Decimal("0")),
                charge_count=len(rows),
                period_start=ps,
                period_end=pe,
                tags=_merge_tag_lists(rows),
                account_name=_first_account_name(rows),
                billing_currency=currency,
            )
        )
    return results


def aggregate_by_tag(
    charges: Iterable[FocusCharge],
    *,
    tag_key: str,
) -> list[AggregatedCost]:
    """Re-aggregate by (account, service, period, currency, tag_key_value).

    Migration 0020 (cost-weighted attribution, the audit committee's
    fix): for each FocusCharge representing N input FOCUS rows, the
    per-row cost is attributed to that row's tag value. Replaces V2's
    row-count weighting, which was wrong when costs were heterogeneous
    (3 EUR web + 97 EUR api gave 50/50 under V2, gives 3% / 97% now).

    Each FocusCharge.tags is a list of per-row tag dicts (1 element
    per contributing input row, possibly empty `{}` for untagged
    rows). FocusCharge.per_row_costs is a parallel list of
    (billed, amortized) tuples for the same input rows. The resolver
    zips the two lists and attributes per-row cost to tag_value.

    Attribution rules:
    - For each input row: if its tag dict has tag_key, its cost goes
      to bucket tag_value. Otherwise, its cost goes to UNTAGGED.
    - 3 EUR web + 97 EUR api -> web=3, api=97 (3% / 97% of total).
    - 1 row web (3 EUR) + 1 row untagged (97 EUR) -> web=3, untagged=97.
    - Empty tags AND empty per_row_costs (pre-0020 data) -> fall back
      to V2 row-count weighting, then to UNTAGGED with the full
      FocusCharge.billed_cost.

    Output: one AggregatedCost per (account, service, period, currency,
    tag_value) tuple, with `tag_key` and `tag_value` set in the result.
    """
    if not tag_key:
        raise ValueError("tag_key must be a non-empty string")

    # (account, service, period, currency, tag_value) -> (sum_billed, sum_amortized, count)
    buckets: dict[tuple[str, str, date, date, str, str], tuple[Decimal, Decimal, int]] = (
        defaultdict(lambda: (Decimal("0"), Decimal("0"), 0))
    )
    # Track per-bucket tag dicts for the insight payload.
    bucket_tags: dict[tuple[str, str, date, date, str, str], list[dict[str, str]]] = defaultdict(
        list
    )
    # First non-empty account name seen per account_id (audit F-13).
    account_names: dict[str, str] = {}

    for c in charges:
        if c.account_name and c.account_id not in account_names:
            account_names[c.account_id] = c.account_name
        if c.period_start is None or c.period_end is None:
            # Should never happen for storage rows; defensive.
            continue

        if c.per_row_costs:
            # Migration 0020 path: per-row cost attribution. Iterate
            # (tag_dict, (billed, amortized)) pairs. If tag_key is in
            # the tag dict, the cost goes to that tag_value. Otherwise
            # (tag dict is {} or doesn't have the key), the cost goes
            # to UNTAGGED.
            for tag_dict, (billed, amortized) in zip(c.tags, c.per_row_costs, strict=True):
                if tag_key in tag_dict and tag_dict[tag_key] != "":
                    tag_value = tag_dict[tag_key]
                    bucket_tags_dict = {tag_key: tag_value}
                else:
                    tag_value = UNTAGGED
                    bucket_tags_dict = {}
                key = (
                    c.account_id,
                    c.service,
                    c.period_start,
                    c.period_end,
                    c.billing_currency,
                    tag_value,
                )
                prev_billed, prev_amortized, prev_count = buckets[key]
                buckets[key] = (
                    prev_billed + billed,
                    prev_amortized + amortized,
                    prev_count + 1,
                )
                bucket_tags[key].append(bucket_tags_dict)
        elif c.tags:
            # Fallback for charges with tags but no per_row_costs
            # (shouldn't happen post-0020, but be defensive): use V2
            # row-count weighting on the tag dicts.
            _attribute_row_count(c, tag_key, buckets, bucket_tags)
        else:
            # No per-row data at all: full cost to UNTAGGED.
            key = (
                c.account_id,
                c.service,
                c.period_start,
                c.period_end,
                c.billing_currency,
                UNTAGGED,
            )
            prev_billed, prev_amortized, prev_count = buckets[key]
            buckets[key] = (
                prev_billed + c.billed_cost,
                prev_amortized + c.amortized_cost,
                prev_count + 1,
            )
            bucket_tags[key].append({})

    results: list[AggregatedCost] = []
    for (account_id, service, ps, pe, currency, tag_value), (
        billed,
        amortized,
        count,
    ) in buckets.items():
        results.append(
            AggregatedCost(
                account_id=account_id,
                service=service,
                billed_cost=billed,
                amortized_cost=amortized,
                charge_count=count,
                period_start=ps,
                period_end=pe,
                tags=_unique_dicts_flat(
                    bucket_tags[(account_id, service, ps, pe, currency, tag_value)]
                ),
                tag_key=tag_key,
                tag_value=tag_value,
                account_name=account_names.get(account_id, ""),
                billing_currency=currency,
            )
        )
    return results


def _attribute_row_count(
    c: FocusCharge,
    tag_key: str,
    buckets: dict[tuple[str, str, date, date, str, str], tuple[Decimal, Decimal, int]],
    bucket_tags: dict[tuple[str, str, date, date, str, str], list[dict[str, str]]],
) -> None:
    """V2 row-count fallback for charges with tags but no per_row_costs.

    This branch should not run for post-0020 ingests: every input row
    has per_row_costs. It exists for backward compat with old data
    that has only per-row tag dicts in focus_charge_tags. When the
    per-row cost is 0 (migration 0020 default for old rows), the
    charge ends up here.

    Same semantics as the V2 implementation: count rows per tag_value
    and attribute cost by count weight. Heterogeneous costs still give
    a wrong split, but that's the data-quality debt documented in
    migration 0020 — re-ingest the FOCUS file to recover per-row cost.
    """
    per_value_count: dict[str, int] = {}
    for t in c.tags:
        if tag_key in t:
            v = t[tag_key]
            per_value_count[v] = per_value_count.get(v, 0) + 1

    if not per_value_count:
        # No row carried the requested key -> full cost to UNTAGGED.
        key = (
            c.account_id,
            c.service,
            c.period_start,
            c.period_end,
            c.billing_currency,
            UNTAGGED,
        )
        prev_billed, prev_amortized, prev_count = buckets[key]
        buckets[key] = (
            prev_billed + c.billed_cost,
            prev_amortized + c.amortized_cost,
            prev_count + 1,
        )
        bucket_tags[key].append({})
        return

    total_rows = sum(per_value_count.values())
    for tag_value, count in per_value_count.items():
        weight = Decimal(count) / Decimal(total_rows)
        key = (c.account_id, c.service, c.period_start, c.period_end, c.billing_currency, tag_value)
        prev_billed, prev_amortized, prev_count = buckets[key]
        buckets[key] = (
            prev_billed + c.billed_cost * weight,
            prev_amortized + c.amortized_cost * weight,
            prev_count + 1,
        )
        bucket_tags[key].append({tag_key: tag_value})


def _first_account_name(rows: Iterable[FocusCharge]) -> str:
    """First non-empty account_name among the contributing charges ('' if none)."""
    for r in rows:
        if r.account_name:
            return r.account_name
    return ""


def _merge_tag_lists(rows: Iterable[FocusCharge]) -> list[dict[str, str]]:
    """Flatten all per-row tag lists and return the unique dicts, first-seen order."""
    out: list[dict[str, str]] = []
    seen: set[frozenset[tuple[str, str]]] = set()
    for r in rows:
        for t in r.tags:
            key = frozenset(t.items())
            if key not in seen:
                seen.add(key)
                out.append(t)
    return out


def _unique_dicts_flat(values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    """Deduplicate a flat list of dicts by value, preserving first-seen order."""
    out: list[dict[str, str]] = []
    seen: set[frozenset[tuple[str, str]]] = set()
    for v in values:
        key = frozenset(v.items())
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


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
    (e.g. 'AmazonRDS on Production (2026-07-01 → 2026-07-31)'). When the cost is
    the result of a tag-based re-aggregation, the title includes the
    tag_key/tag_value (e.g. '[Application=web]').

    Severity is always INFO (audit F-13): amortized-vs-billed drift is
    normal RI/Savings-Plans mechanics, not an anomaly — escalating to
    WARNING/CRITICAL on drift magnitude was misleading. The drift insight
    itself is the product; the magnitude is in the payload.
    """
    insights: list[Insight] = []
    for agg in aggregated:
        drift = agg.drift_amortized_minus_billed
        direction = "up" if drift > 0 else "down" if drift < 0 else "flat"
        label = _period_label(agg) if period_label == "" else period_label
        display_account = agg.account_name or agg.account_id

        tag_suffix = ""
        if agg.tag_key is not None and agg.tag_value is not None:
            tag_suffix = f" [{agg.tag_key}={agg.tag_value}]"

        title = (
            f"{agg.service} on {display_account} ({label}){tag_suffix}: "
            f"amortized {direction} by {agg.billing_currency} {abs(drift):.2f}"
        )

        insights.append(
            Insight(
                rule_name=RULE_NAME,
                resource_id=None,
                account_id=agg.account_id,
                severity=Severity.INFO,
                title=title,
                payload={
                    "service": agg.service,
                    "account_id": agg.account_id,
                    "period_label": label,
                    "period_start": agg.period_start.isoformat() if agg.period_start else None,
                    "period_end": agg.period_end.isoformat() if agg.period_end else None,
                    # The amount KEYS keep their V1 `*_usd` suffix
                    # (registry-locked in constat_core.monetary.MONETARY);
                    # `billing_currency` is the authoritative label for
                    # what those amounts actually are.
                    "billing_currency": agg.billing_currency,
                    "billed_cost_usd": float(agg.billed_cost),
                    "amortized_cost_usd": float(agg.amortized_cost),
                    "drift_amortized_minus_billed_usd": float(drift),
                    "charge_count": agg.charge_count,
                    "tag_key": agg.tag_key,
                    "tag_value": agg.tag_value,
                    "tags": [dict(t) for t in agg.tags],
                },
            )
        )
    return insights
