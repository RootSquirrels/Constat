"""Insight rule: orphaned EBS snapshots (source volume deleted, still billed)."""

from constat_snapshot_orphan.resolver import RULE_NAME, SOURCE_NAME, evaluate

__all__ = ["RULE_NAME", "SOURCE_NAME", "evaluate"]
