"""Insight rule: stopped EC2 instances still paying for attached EBS storage."""

from constat_ec2_stopped_with_storage.resolver import RULE_NAME, SOURCE_NAME, evaluate

__all__ = ["RULE_NAME", "SOURCE_NAME", "evaluate"]
