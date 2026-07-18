"""AWS RDS connector."""

from constat_aws_rds.collector import (
    DEFAULT_REGIONS,
    collect_db_instances,
    db_to_facts,
    db_to_observation,
    db_to_resource,
)

__all__ = [
    "DEFAULT_REGIONS",
    "collect_db_instances",
    "db_to_facts",
    "db_to_observation",
    "db_to_resource",
]
