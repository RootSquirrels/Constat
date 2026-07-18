"""Insight rule: EBS gp2 → gp3 migration (storage cost, no behavior change)."""

from constat_ebs_gp2_to_gp3.resolver import RULE_NAME, SOURCE_NAME, evaluate

__all__ = ["RULE_NAME", "SOURCE_NAME", "evaluate"]
