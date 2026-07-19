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
    attached_instance_id, attached_device, create_time, region. The
    region fact is what lets the pricing rules pick the right catalog
    grid (EBS pricing is not region-uniform); it comes from the
    collector-injected `_region` key, KNOWN whenever it is present.
    State-derived booleans (is_unattached, is_gp2) are derived in the
    rule, not emitted as facts — the catalog doesn't know what a
    `is_unattached` fact means, and rules should encode their own
    semantics.
    """
    vol_type = vol.get("VolumeType")
    state = vol.get("State")
    size_gb = vol.get("Size")
    iops = vol.get("Iops")
    throughput = vol.get("Throughput")
    encrypted = vol.get("Encrypted")
    create_time = vol.get("CreateTime")
    region = vol.get("_region")
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
        _fact("region", region, ValueState.KNOWN if region else ValueState.UNKNOWN),
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


def snapshot_to_facts(
    resource_id: UUID, account_id: str, snap: dict[str, Any], observed_at: datetime
) -> list[Fact]:
    """Convert an EBS snapshot to canonical Facts (aws.ec2.snapshot.*).

    Keys: state, size_gb, storage_tier, volume_id, start_time,
    description, region. The region fact lets snapshot_orphan pick the
    right snapshot pricing grid (collector-injected `_region`).
    Cross-resource facts (volume_exists) are NOT produced here — they
    need the region's volume scan, so they are written by the
    correlation post-pass (`correlation_facts`). Same split as volumes:
    state-derived booleans (is_orphan) are derived in the rule.
    """
    state = snap.get("State")
    size_gb = snap.get("VolumeSize")
    storage_tier = snap.get("StorageTier")
    volume_id = snap.get("VolumeId")
    start_time = snap.get("StartTime")
    description = snap.get("Description")
    region = snap.get("_region")

    def _fact(key: str, value: Any, state: ValueState) -> Fact:
        return Fact(
            resource_id=resource_id,
            account_id=account_id,
            namespace="aws.ec2.snapshot",
            key=key,
            value=value,
            value_state=state,
            source=SOURCE_NAME,
            observed_at=observed_at,
        )

    return [
        _fact("state", state, ValueState.KNOWN if state else ValueState.UNKNOWN),
        _fact("size_gb", size_gb, ValueState.KNOWN if size_gb is not None else ValueState.UNKNOWN),
        _fact(
            "storage_tier",
            storage_tier,
            ValueState.KNOWN if storage_tier else ValueState.UNKNOWN,
        ),
        _fact("volume_id", volume_id, ValueState.KNOWN if volume_id else ValueState.UNKNOWN),
        _fact(
            "start_time",
            start_time.isoformat() if start_time else None,
            ValueState.KNOWN if start_time else ValueState.UNKNOWN,
        ),
        _fact(
            "description",
            description,
            ValueState.KNOWN if description is not None else ValueState.UNKNOWN,
        ),
        _fact("region", region, ValueState.KNOWN if region else ValueState.UNKNOWN),
    ]


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
            "BlockDeviceMappings": [
                {
                    "DeviceName": m.get("DeviceName"),
                    "VolumeId": (m.get("Ebs") or {}).get("VolumeId"),
                }
                for m in (inst.get("BlockDeviceMappings") or [])
            ],
            "Tags": {t.get("Key"): t.get("Value") for t in (inst.get("Tags") or [])},
        },
    )


def instance_to_facts(
    resource_id: UUID, account_id: str, inst: dict[str, Any], observed_at: datetime
) -> list[Fact]:
    """Convert an EC2 instance to canonical Facts (aws.ec2.instance.*).

    Keys: state, instance_type, launch_time, block_device_volume_ids,
    region. The region fact lets ec2_stopped_with_storage price the
    attached volumes on the right catalog grid — the volumes of an
    instance are always in the instance's region (EBS volumes attach
    within an AZ), so one region fact covers the whole breakdown.
    The cross-resource `attached_volumes` fact (volume ids resolved to
    sizes/types against the region's volume scan) is written by the
    correlation post-pass (`correlation_facts`), not here — a single
    instance's raw payload doesn't carry volume sizes.
    """
    state = (inst.get("State") or {}).get("Name")
    instance_type = inst.get("InstanceType")
    launch_time = inst.get("LaunchTime")
    region = inst.get("_region")
    volume_ids = [
        ebs.get("VolumeId")
        for m in (inst.get("BlockDeviceMappings") or [])
        if (ebs := m.get("Ebs") or {}).get("VolumeId")
    ]

    def _fact(key: str, value: Any, state: ValueState) -> Fact:
        return Fact(
            resource_id=resource_id,
            account_id=account_id,
            namespace="aws.ec2.instance",
            key=key,
            value=value,
            value_state=state,
            source=SOURCE_NAME,
            observed_at=observed_at,
        )

    return [
        _fact("state", state, ValueState.KNOWN if state else ValueState.UNKNOWN),
        _fact(
            "instance_type",
            instance_type,
            ValueState.KNOWN if instance_type else ValueState.UNKNOWN,
        ),
        _fact(
            "launch_time",
            launch_time.isoformat() if launch_time else None,
            ValueState.KNOWN if launch_time else ValueState.UNKNOWN,
        ),
        # Always KNOWN: an empty list is a real observation ("no EBS
        # block devices"), not a gap. Instance-store-only instances
        # legitimately have zero entries.
        _fact("block_device_volume_ids", volume_ids, ValueState.KNOWN),
        _fact("region", region, ValueState.KNOWN if region else ValueState.UNKNOWN),
    ]


# ---------------------------------------------------------------------------
# Cross-resource correlation (post-pass)
# ---------------------------------------------------------------------------


def correlation_facts(
    *,
    volumes: list[tuple[UUID, dict[str, Any]]] | None,
    snapshots: list[tuple[UUID, dict[str, Any]]],
    instances: list[tuple[UUID, dict[str, Any]]],
    account_id: str,
    observed_at: datetime,
) -> list[Fact]:
    """Build the cross-resource facts one item's raw payload cannot carry.

    Pure function over the (resource_id, raw) pairs collected in ONE
    region — no DB access. The caller (apps/api collector) runs it after
    all jobs of a region and persists the result.

    - `aws.ec2.snapshot.volume_exists` (bool): the snapshot's VolumeId
      was seen by THIS region's volume scan. Written for every snapshot
      when the volume job ran — True and False are both proven facts.
    - `aws.ec2.instance.attached_volumes` (list of
      {volume_id, size_gb, volume_type}): for stopped instances only,
      the BlockDeviceMappings volume ids resolved to sizes/types from
      the volume scan. A volume id the scan didn't see (deleted since,
      or in another account) is skipped — the list is what we can prove.

    `volumes=None` means the volume job did NOT run (or failed) in this
    region: no correlation fact is written at all. Absence of the fact
    is what makes the rules INCONCLUSIVE — we never write a guessed
    "volume does not exist" (absence of proof is not proof of absence).
    """
    if volumes is None:
        return []

    volume_index: dict[str, dict[str, Any]] = {
        raw["VolumeId"]: raw for _, raw in volumes if raw.get("VolumeId")
    }
    facts: list[Fact] = []

    for resource_id, snap in snapshots:
        volume_id = snap.get("VolumeId")
        facts.append(
            Fact(
                resource_id=resource_id,
                account_id=account_id,
                namespace="aws.ec2.snapshot",
                key="volume_exists",
                value=bool(volume_id) and volume_id in volume_index,
                value_state=ValueState.KNOWN,
                source=SOURCE_NAME,
                observed_at=observed_at,
            )
        )

    for resource_id, inst in instances:
        if (inst.get("State") or {}).get("Name") != "stopped":
            continue
        attached: list[dict[str, Any]] = []
        for mapping in inst.get("BlockDeviceMappings") or []:
            volume_id = (mapping.get("Ebs") or {}).get("VolumeId")
            if not volume_id:
                continue
            vol_raw = volume_index.get(volume_id)
            if vol_raw is None:
                continue
            attached.append(
                {
                    "volume_id": volume_id,
                    "size_gb": vol_raw.get("Size"),
                    "volume_type": vol_raw.get("VolumeType"),
                }
            )
        facts.append(
            Fact(
                resource_id=resource_id,
                account_id=account_id,
                namespace="aws.ec2.instance",
                key="attached_volumes",
                value=attached,
                value_state=ValueState.KNOWN,
                source=SOURCE_NAME,
                observed_at=observed_at,
            )
        )

    return facts
