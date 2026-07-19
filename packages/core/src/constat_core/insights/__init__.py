"""Generic rule evaluators that multiple insight packages share.

The V1 rules share ~80% of their code: the 4 fact gates, the
3-branch severity logic (force-upgrade / in-extended-support /
pre-EOL window), and the payload assembly. `eol.py` is the
single home of that code; the per-engine packages
(`packages/insights/rds_eol`, `mysql_eol`, `aurora_eol`) are
~30 lines of `EngineEolMatcher` config + a thin wrapper that
calls `evaluate_eol`. Adding a new engine = one matcher; no
change to the shared function.

Chantier III.1 of the roadmap consolidation: the arithmetic
`vcpu × tier rate × 730h` lives in one place, not in three.
"""

from constat_core.insights.eol import (
    EolInsightResult,
    EngineEolMatcher,
    EolRuleConfig,
    evaluate_eol,
)

__all__ = [
    "EolInsightResult",
    "EolRuleConfig",
    "EngineEolMatcher",
    "evaluate_eol",
]
