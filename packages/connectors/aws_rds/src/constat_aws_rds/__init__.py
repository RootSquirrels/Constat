"""AWS RDS connector."""

from constat_aws_rds.collector import (
    collect_db_instances,
    db_to_facts,
    db_to_observation,
    db_to_resource,
)

__all__ = [
    "collect_db_instances",
    "db_to_facts",
    "db_to_observation",
    "db_to_resource",
]
