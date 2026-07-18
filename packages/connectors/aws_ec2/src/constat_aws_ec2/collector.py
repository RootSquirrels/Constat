"""AWS EC2/EBS collector.

Mirrors the aws_rds connector pattern: the caller owns the boto3 Session
(so we can support cross-account AssumeRole). This module only translates
AWS API responses into canonical Resources / Facts / Observations.

Three resource types:
- AWS::EC2::Volume   (EBS volumes, gp2/gp3/io1/io2/st1/sc1/magnetic)
- AWS::EC2::Snapshot (EBS snapshots, owner=self)
- AWS::EC2::Instance (EC2 instances, all states)

Per-region pagination uses the boto3 paginator + the same adaptive
retry config as aws_rds (10 attempts, client-side rate limiting,
jittered backoff). A throttled multi-region scan backs off smoothly
instead of hammering the API in lockstep.

Source name: `aws_ec2`. The runner's scope-completeness check looks up
source_runs by this name, distinct from `aws_rds`. A successful RDS scan
does NOT prove EC2 scope and vice-versa.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config as BotoConfig
from constat_core.models import Fact, Observation, Resource, ValueState

# Resource types. One source_run per (account, region, resource_type, source).
VOLUME_RESOURCE_TYPE = "AWS::EC2::Volume"
SNAPSHOT_RESOURCE_TYPE = "AWS::EC2::Snapshot"
INSTANCE_RESOURCE_TYPE = "AWS::EC2::Instance"

SOURCE_NAME = "aws_ec2"

# Default region set. Same default as aws_rds. Tunable per tenant.
DEFAULT_REGIONS: list[str] = [
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-central-1",
    "us-east-1",
    "us-east-2",
    "us-west-2",
]

# Same adaptive retry config as aws_rds (shared intent): throttling
# resilience for paginated, multi-region scans. EC2 Describe* APIs are
# also throttled (token-bucket) so the same policy applies.
ADAPTIVE_RETRY_CONFIG = BotoConfig(
    retries={"mode": "adaptive", "max_attempts": 10},
    connect_timeout=10,
    read_timeout=30,
)


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Paginators
# ---------------------------------------------------------------------------


def collect_volumes(
    session: boto3.Session, regions: list[str] | None = None
) -> Iterator[dict[str, Any]]:
    """Yield raw EBS volume dicts from AWS EC2 DescribeVolumes across regions.

    Each yielded dict has an extra `_region` key. We do NOT filter by
    status — the rules decide (e.g. `ebs.unattached` cares about
    `status=available`; `ebs.gp2_to_gp3` cares about `type=gp2`).
    """
    regions = regions or DEFAULT_REGIONS
    for region in regions:
        client = session.client("ec2", region_name=region, config=ADAPTIVE_RETRY_CONFIG)
        paginator = client.get_paginator("describe_volumes")
        for page in paginator.paginate():
            for vol in page.get("Volumes", []):
                vol["_region"] = region
                yield vol


def collect_snapshots(
    session: boto3.Session, regions: list[str] | None = None
) -> Iterator[dict[str, Any]]:
    """Yield raw EBS snapshot dicts from DescribeSnapshots across regions.

    Filters to `owner=self` so we only see the prospect's own snapshots
    (orphan detection works on assets the prospect actually owns).
    The full snapshot list (cross-account copies) would otherwise be huge
    on accounts that receive a lot of shared snapshots.
    """
    regions = regions or DEFAULT_REGIONS
    for region in regions:
        client = session.client("ec2", region_name=region, config=ADAPTIVE_RETRY_CONFIG)
        paginator = client.get_paginator("describe_snapshots")
        for page in paginator.paginate(OwnerIds=["self"]):
            for snap in page.get("Snapshots", []):
                snap["_region"] = region
                yield snap


def collect_instances(
    session: boto3.Session, regions: list[str] | None = None
) -> Iterator[dict[str, Any]]:
    """Yield raw EC2 instance dicts from DescribeInstances across regions.

    No state filter — `ec2.stopped_with_storage` needs `state=stopped`,
    and the rules read state themselves. Filtering at the API level
    would force callers to re-scan for every rule.
    """
    regions = regions or DEFAULT_REGIONS
    for region in regions:
        client = session.client("ec2", region_name=region, config=ADAPTIVE_RETRY_CONFIG)
        paginator = client.get_paginator("describe_instances")
        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    inst["_region"] = region
                    yield inst


# ---------------------------------------------------------------------------
# Resource / Fact / Observation mappers
# ---------------------------------------------------------------------------


def volume_to_resource(vol: dict[str, Any], account_id: str) -> Resource:
    """Build a canonical Resource from an EBS DescribeVolumes item."""
    now = _now_utc()
    return Resource(
        account_id=account_id,
        region=vol["_region"],
        resource_type=VOLUME_RESOURCE_TYPE,
        native_id=vol["VolumeId"],
        first_seen_at=now,
        last_seen_at=now,
    )


def volume_to_facts(
    resource_id: UUID, account_id: str, vol: dict[str, Any], observed_at: datetime
) -> list[Fact]:
    """Convert an EBS volume to canonical Facts (aws.ec2.volume.*).

    Keys: size_gb, volume_type, state, encrypted, iops, throughput,
    attached_instance_id, attached_device, create_time. State-derived
    booleans (is_unattached, is_gp2) are derived in the rule, not
    emitted as facts — the catalog doesn't know what a `is_unattached`
    fact means, and rules should encode their own semantics.
    """
    vol_type = vol.get("VolumeType")
    state = vol.get("State")
    size_gb = vol.get("Size")
    iops = vol.get("Iops")
    throughput = vol.get("Throughput")
    encrypted = vol.get("Encrypted")
    create_time = vol.get("CreateTime")
    attachments = vol.get("Attachments") or []
    attached_instance = attachments[0].get("InstanceId") if attachments else None
    attached_device = attachments[0].get("Device") if attachments else None

    def _fact(key: str, value: Any, state: ValueState) -> Fact:
        return Fact(
            resource_id=resource_id,
            account_id=account_id,
            namespace="aws.ec2.volume",
            key=key,
            value=value,
            value_state=state,
            source=SOURCE_NAME,
            observed_at=observed_at,
        )

    return [
        _fact("size_gb", size_gb, ValueState.KNOWN if size_gb is not None else ValueState.UNKNOWN),
        _fact("volume_type", vol_type, ValueState.KNOWN if vol_type else ValueState.UNKNOWN),
        _fact("state", state, ValueState.KNOWN if state else ValueState.UNKNOWN),
        _fact(
            "encrypted",
            encrypted,
            ValueState.KNOWN if encrypted is not None else ValueState.UNKNOWN,
        ),
        _fact("iops", iops, ValueState.KNOWN if iops is not None else ValueState.UNKNOWN),
        _fact(
            "throughput",
            throughput,
            ValueState.KNOWN if throughput is not None else ValueState.UNKNOWN,
        ),
        _fact(
            "attached_instance_id",
            attached_instance,
            ValueState.KNOWN if attached_instance else ValueState.UNKNOWN,
        ),
        _fact(
            "attached_device",
            attached_device,
            ValueState.KNOWN if attached_device else ValueState.UNKNOWN,
        ),
        _fact(
            "create_time",
            create_time.isoformat() if create_time else None,
            ValueState.KNOWN if create_time else ValueState.UNKNOWN,
        ),
    ]


def volume_to_observation(
    resource_id: UUID, vol: dict[str, Any], observed_at: datetime
) -> Observation:
    """Convert an EBS volume to an immutable Observation (full source payload).

    The payload keeps the original AWS keys (VolumeId, VolumeType, ...)
    so a downstream consumer (or a future re-derivation job) doesn't
    have to re-call AWS to see what we saw. The hash is over the
    canonical JSON, not the live object.
    """
    attachments = vol.get("Attachments") or []
    return Observation(
        resource_id=resource_id,
        source=SOURCE_NAME,
        observed_at=observed_at,
        payload={
            "VolumeId": vol.get("VolumeId"),
            "VolumeType": vol.get("VolumeType"),
            "State": vol.get("State"),
            "Size": vol.get("Size"),
            "Iops": vol.get("Iops"),
            "Throughput": vol.get("Throughput"),
            "Encrypted": vol.get("Encrypted"),
            "AvailabilityZone": vol.get("AvailabilityZone"),
            "CreateTime": vol.get("CreateTime").isoformat() if vol.get("CreateTime") else None,
            "Attachments": [
                {
                    "InstanceId": a.get("InstanceId"),
                    "Device": a.get("Device"),
                    "State": a.get("State"),
                }
                for a in attachments
            ],
            "Tags": {t.get("Key"): t.get("Value") for t in (vol.get("Tags") or [])},
        },
    )


def snapshot_to_resource(snap: dict[str, Any], account_id: str) -> Resource:
    """Build a canonical Resource from an EBS DescribeSnapshots item."""
    now = _now_utc()
    return Resource(
        account_id=account_id,
        region=snap["_region"],
        resource_type=SNAPSHOT_RESOURCE_TYPE,
        native_id=snap["SnapshotId"],
        first_seen_at=now,
        last_seen_at=now,
    )


def snapshot_to_observation(
    resource_id: UUID, snap: dict[str, Any], observed_at: datetime
) -> Observation:
    """Convert an EBS snapshot to an immutable Observation."""
    start_time = snap.get("StartTime")
    return Observation(
        resource_id=resource_id,
        source=SOURCE_NAME,
        observed_at=observed_at,
        payload={
            "SnapshotId": snap.get("SnapshotId"),
            "State": snap.get("State"),
            "VolumeSize": snap.get("VolumeSize"),
            "VolumeId": snap.get("VolumeId"),
            "Encrypted": snap.get("Encrypted"),
            "OwnerId": snap.get("OwnerId"),
            "StartTime": start_time.isoformat() if start_time else None,
            "Description": snap.get("Description"),
            "Tags": {t.get("Key"): t.get("Value") for t in (snap.get("Tags") or [])},
        },
    )


def instance_to_resource(inst: dict[str, Any], account_id: str) -> Resource:
    """Build a canonical Resource from an EC2 DescribeInstances item."""
    now = _now_utc()
    return Resource(
        account_id=account_id,
        region=inst["_region"],
        resource_type=INSTANCE_RESOURCE_TYPE,
        native_id=inst["InstanceId"],
        first_seen_at=now,
        last_seen_at=now,
    )


def instance_to_observation(
    resource_id: UUID, inst: dict[str, Any], observed_at: datetime
) -> Observation:
    """Convert an EC2 instance to an immutable Observation."""
    launch_time = inst.get("LaunchTime")
    state = inst.get("State") or {}
    return Observation(
        resource_id=resource_id,
        source=SOURCE_NAME,
        observed_at=observed_at,
        payload={
            "InstanceId": inst.get("InstanceId"),
            "InstanceType": inst.get("InstanceType"),
            "State": state.get("Name"),
            "ImageId": inst.get("ImageId"),
            "LaunchTime": launch_time.isoformat() if launch_time else None,
            "PrivateIpAddress": inst.get("PrivateIpAddress"),
            "PublicIpAddress": inst.get("PublicIpAddress"),
            "Tags": {t.get("Key"): t.get("Value") for t in (inst.get("Tags") or [])},
        },
    )
