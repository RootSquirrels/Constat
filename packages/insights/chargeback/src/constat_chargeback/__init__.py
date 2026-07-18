"""Insight rule: per-account x service cost chargeback.

Emits one Insight per (account, service) tuple with billed/amortized/effective
totals and the delta (amortized - billed) to surface reservation/RI drift.

Implementation lives in `resolver.py`. Aggregation windows are passed in.
"""

from constat_chargeback.resolver import RULE_NAME, aggregate, build_insights

__all__ = ["RULE_NAME", "aggregate", "build_insights"]
