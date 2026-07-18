"""AWS EC2/EBS connector — boto3 DescribeInstances/Volumes/Snapshots."""

from constat_aws_ec2.collector import (
    ADAPTIVE_RETRY_CONFIG,
    DEFAULT_REGIONS,
    INSTANCE_RESOURCE_TYPE,
    SNAPSHOT_RESOURCE_TYPE,
    SOURCE_NAME,
    VOLUME_RESOURCE_TYPE,
    collect_instances,
    collect_snapshots,
    collect_volumes,
    instance_to_observation,
    instance_to_resource,
    snapshot_to_observation,
    snapshot_to_resource,
    volume_to_facts,
    volume_to_observation,
    volume_to_resource,
)

__all__ = [
    "ADAPTIVE_RETRY_CONFIG",
    "DEFAULT_REGIONS",
    "INSTANCE_RESOURCE_TYPE",
    "SNAPSHOT_RESOURCE_TYPE",
    "SOURCE_NAME",
    "VOLUME_RESOURCE_TYPE",
    "collect_instances",
    "collect_snapshots",
    "collect_volumes",
    "instance_to_observation",
    "instance_to_resource",
    "snapshot_to_observation",
    "snapshot_to_resource",
    "volume_to_facts",
    "volume_to_observation",
    "volume_to_resource",
]
