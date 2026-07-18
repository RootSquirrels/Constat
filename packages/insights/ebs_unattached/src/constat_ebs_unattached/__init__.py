"""Insight rule: EBS unattached volumes (storage cost with no consumer)."""

from constat_ebs_unattached.resolver import RULE_NAME, SOURCE_NAME, evaluate

__all__ = ["RULE_NAME", "SOURCE_NAME", "evaluate"]
