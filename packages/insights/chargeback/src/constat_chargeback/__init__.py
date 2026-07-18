"""Insight rule: per-account x service cost chargeback.

Emits one Insight per (account, service) tuple with billed/amortized/effective
totals and the delta (amortized - billed) to surface reservation/RI drift.

Implementation lives in `resolver.py`. Aggregation windows are passed in.
V1 supports per-period and per-tag (Application, CostCenter, ...) grouping.
"""

from constat_chargeback.resolver import (
    RULE_NAME,
    UNTAGGED,
    aggregate,
    aggregate_by_period,
    aggregate_by_tag,
    build_insights,
)

__all__ = [
    "RULE_NAME",
    "UNTAGGED",
    "aggregate",
    "aggregate_by_period",
    "aggregate_by_tag",
    "build_insights",
]
