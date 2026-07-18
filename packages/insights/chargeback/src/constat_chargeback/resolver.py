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
    # All unique tag dicts seen across the input FocusCharge rows that
    # contributed to this aggregate. Empty list when no tags were present.
    # Used by `aggregate_by_tag` to re-aggregate by any tag key.
    tags: list[dict[str, str]] = field(default_factory=list)
    # Set when the cost is the result of a tag-based re-aggregation. The
    # tag_key is the user-chosen key (e.g. "Application"); tag_value is the
    # value the cost was attributed to (or UNTAGGED).
    tag_key: str | None = None
    tag_value: str | None = None

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
                tags=_merge_tag_lists(rows),
            )
        )
    return results


def aggregate_by_period(
    charges: Iterable[FocusCharge],
) -> list[AggregatedCost]:
    """Group FOCUS charges by (account, service, period_start, period_end).

    One row per (account, service, billing period). Use this for monthly
    trends. The `tags` field carries every unique tag dict seen in the
    contributing rows, ready for downstream tag-based re-aggregation.
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
                tags=_merge_tag_lists(rows),
            )
        )
    return results


def aggregate_by_tag(
    charges: Iterable[FocusCharge],
    *,
    tag_key: str,
) -> list[AggregatedCost]:
    """Re-aggregate by (account, service, period, tag_key_value).

    V2 cost attribution (P3 item 11 fix): for each FocusCharge
    representing N input FOCUS rows, count the rows per (tag_value)
    and attribute cost proportionally. Replaces V1's even split
    (1/N per unique value), which was wrong for heterogeneous tag data.

    Each FocusCharge.tags is a list of per-row tag dicts (1 element
    per contributing input row). The runner reads focus_charge_tags
    to build this. The loader wraps a single dict per raw FOCUS row.

    Attribution rules:
    - If 5 input rows have {Application: web} and 2 have
      {Application: api}, web gets 5/7 of the cost, api gets 2/7.
    - Rows with no tag for the key go to UNTAGGED with their full cost.
    - Empty tags -> full cost to UNTAGGED.

    Output: one AggregatedCost per (account, service, period, tag_value)
    tuple, with `tag_key` and `tag_value` set in the result.
    """
    if not tag_key:
        raise ValueError("tag_key must be a non-empty string")

    # (account, service, period, tag_value) -> list of (cost_billed, cost_amortized, weight)
    # The weight is the per-row attribution share. Sum of weights for a
    # (charge, tag_value) is the proportion of that charge's cost
    # attributed to tag_value.
    buckets: dict[tuple[str, str, date, date, str], list[tuple[Decimal, Decimal, Decimal]]] = (
        defaultdict(list)
    )
    # Track per-bucket tag dicts for the insight payload.
    bucket_tags: dict[tuple[str, str, date, date, str], list[dict[str, str]]] = defaultdict(list)

    for c in charges:
        if c.period_start is None or c.period_end is None:
            # Should never happen for storage rows; defensive.
            continue

        # Count per-input-row tag values for `tag_key`. Each element of
        # c.tags is one input row's tag dict.
        per_value_count: dict[str, int] = {}
        for t in c.tags:
            if tag_key in t:
                v = t[tag_key]
                per_value_count[v] = per_value_count.get(v, 0) + 1

        if not per_value_count:
            # No row carried the requested key -> full cost to UNTAGGED.
            key = (c.account_id, c.service, c.period_start, c.period_end, UNTAGGED)
            buckets[key].append((c.billed_cost, c.amortized_cost, Decimal("1")))
            bucket_tags[key].append({})
            continue

        # Proportional split: each tag_value gets cost * (count / total_rows).
        total_rows = sum(per_value_count.values())
        for tag_value, count in per_value_count.items():
            weight = Decimal(count) / Decimal(total_rows)
            key = (c.account_id, c.service, c.period_start, c.period_end, tag_value)
            buckets[key].append((c.billed_cost, c.amortized_cost, weight))
            bucket_tags[key].append({tag_key: tag_value})

    results: list[AggregatedCost] = []
    for (account_id, service, ps, pe, tag_value), weights in buckets.items():
        billed = sum((b * w for b, _a, w in weights), Decimal("0"))
        amortized = sum((a * w for _b, a, w in weights), Decimal("0"))
        results.append(
            AggregatedCost(
                account_id=account_id,
                service=service,
                billed_cost=billed,
                amortized_cost=amortized,
                charge_count=len(weights),
                period_start=ps,
                period_end=pe,
                tags=_unique_dicts_flat(bucket_tags[(account_id, service, ps, pe, tag_value)]),
                tag_key=tag_key,
                tag_value=tag_value,
            )
        )
    return results


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
    (e.g. 'AmazonRDS on 111 (2026-07-01 → 2026-07-31)'). When the cost is
    the result of a tag-based re-aggregation, the title includes the
    tag_key/tag_value (e.g. '[Application=web]').
    """
    insights: list[Insight] = []
    for agg in aggregated:
        drift = agg.drift_amortized_minus_billed
        severity = _severity_for_drift(drift)
        direction = "up" if drift > 0 else "down" if drift < 0 else "flat"
        label = _period_label(agg) if period_label == "" else period_label

        tag_suffix = ""
        if agg.tag_key is not None and agg.tag_value is not None:
            tag_suffix = f" [{agg.tag_key}={agg.tag_value}]"

        title = (
            f"{agg.service} on {agg.account_id} ({label}){tag_suffix}: "
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
                    "tag_key": agg.tag_key,
                    "tag_value": agg.tag_value,
                    "tags": [dict(t) for t in agg.tags],
                },
            )
        )
    return insights
