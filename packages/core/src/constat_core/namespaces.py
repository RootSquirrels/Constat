"""Fact value states.

Every fact carries a `value_state`. The product surfaces `UNKNOWN` explicitly —
that's the differentiator vs Trusted Advisor / Cost Explorer, which silently omit.
"""

from __future__ import annotations

from enum import StrEnum


class ValueState(StrEnum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"
    ERROR = "ERROR"


# Fact namespace prefixes. New prefix = open an issue. We don't do EAV.
# See AGENTS.md "Namespaces" section.
NAMESPACE_AWS = "aws"  # direct from AWS APIs
NAMESPACE_CATALOG = "catalog"  # versioned reference data
NAMESPACE_COST = "cost"  # FOCUS-derived
NAMESPACE_DERIVED = "derived"  # computed by insights
