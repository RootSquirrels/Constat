"""Generic rule evaluators that multiple insight packages share.

The V1 rules share ~80% of their code:
- the fact gates, 3-branch severity logic (force-upgrade /
  in-extended-support / pre-EOL window), and payload assembly for
  EOL — `eol.py` is the single home of that code.
- the required-facts gate, NO_MATCH predicate, monthly cost
  arithmetic, $500/$50 severity assignment, and payload
  assembly for storage cost — `storage.py` is the single home
  of that code.

The per-rule packages (`packages/insights/{rds,mysql,aurora}_eol`
and `{ebs_gp2_to_gp3,ebs_unattached,snapshot_orphan}`) are
~30 lines of `EngineEolMatcher` / `StorageRuleConfig` config + a
thin wrapper that calls `evaluate_eol` / `evaluate_storage`.
Adding a new engine = one matcher (EOL); adding a new storage
rule = one config.

Chantier III of the roadmap consolidation: the cost arithmetic
lives in one place, not in 6 rule packages.
"""

from constat_core.insights.eol import (
    EngineEolMatcher,
    EolInsightResult,
    EolRuleConfig,
    evaluate_eol,
)
from constat_core.insights.storage import (
    StorageCost,
    StorageInconclusiveError,
    StorageInsightResult,
    StorageRuleConfig,
    evaluate_storage,
)

__all__ = [
    "EngineEolMatcher",
    "EolInsightResult",
    "EolRuleConfig",
    "StorageCost",
    "StorageInconclusiveError",
    "StorageInsightResult",
    "StorageRuleConfig",
    "evaluate_eol",
    "evaluate_storage",
]
