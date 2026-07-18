"""Tests for the AWS EC2/EBS connector.

Mirrors test_aws_rds_connector style: no real boto3 calls; we patch
the boto3 Session/client so the paginator returns canned data. The
connector's job is to translate AWS responses into canonical
Resource / Fact / Observation — the test asserts that translation
is correct.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

from constat_aws_ec2.collector import (
    INSTANCE_RESOURCE_TYPE,
    SNAPSHOT_RESOURCE_TYPE,
    SOURCE_NAME,
    VOLUME_RESOURCE_TYPE,
    collect_volumes,
    instance_to_observation,
    instance_to_resource,
    snapshot_to_observation,
    snapshot_to_resource,
    volume_to_facts,
    volume_to_observation,
    volume_to_resource,
)
from constat_core.models import ValueState

# ---------------------------------------------------------------------------
# Test fixtures: minimal AWS-shaped dicts the connector maps.
# ---------------------------------------------------------------------------


def _make_volume(
    *,
    volume_id: str = "vol-0123456789abcdef0",
    volume_type: str = "gp2",
    state: str = "in-use",
    size: int = 100,
    iops: int | None = 3000,
    throughput: int | None = 125,
    encrypted: bool = True,
    instance_id: str | None = "i-0123456789abcdef0",
    device: str | None = "/dev/sda1",
) -> dict[str, Any]:
    """Build a DescribeVolumes-style dict. Optional fields default to
    in-use gp2 with IOPS/throughput (gp3 shape) — tests override as needed."""
    attachments: list[dict[str, Any]] = []
    if instance_id and device:
        attachments.append(
            {
                "InstanceId": instance_id,
                "Device": device,
                "State": "attached",
                "DeleteOnTermination": True,
            }
        )
    return {
        "VolumeId": volume_id,
        "VolumeType": volume_type,
        "State": state,
        "Size": size,
        "Iops": iops,
        "Throughput": throughput,
        "Encrypted": encrypted,
        "AvailabilityZone": "eu-west-1a",
        "CreateTime": datetime(2024, 6, 1, tzinfo=UTC),
        "Attachments": attachments,
        "Tags": [],
    }


def _make_snapshot(
    *,
    snapshot_id: str = "snap-0123456789abcdef0",
    state: str = "completed",
    volume_size: int = 100,
    volume_id: str | None = "vol-abc",
    encrypted: bool = True,
) -> dict[str, Any]:
    return {
        "SnapshotId": snapshot_id,
        "State": state,
        "VolumeSize": volume_size,
        "VolumeId": volume_id,
        "Encrypted": encrypted,
        "OwnerId": "111111111111",
        "StartTime": datetime(2024, 6, 1, tzinfo=UTC),
        "Description": "test",
        "Tags": [],
    }


def _make_instance(
    *,
    instance_id: str = "i-0123456789abcdef0",
    instance_type: str = "t3.medium",
    state: str = "running",
) -> dict[str, Any]:
    return {
        "InstanceId": instance_id,
        "InstanceType": instance_type,
        "State": {"Name": state, "Code": 16},
        "ImageId": "ami-abc",
        "LaunchTime": datetime(2024, 6, 1, tzinfo=UTC),
        "PrivateIpAddress": "10.0.0.1",
        "PublicIpAddress": None,
        "Tags": [],
    }


# ---------------------------------------------------------------------------
# Resource type + source constants
# ---------------------------------------------------------------------------


def test_volume_resource_type_is_canonical():
    assert VOLUME_RESOURCE_TYPE == "AWS::EC2::Volume"


def test_snapshot_resource_type_is_canonical():
    assert SNAPSHOT_RESOURCE_TYPE == "AWS::EC2::Snapshot"


def test_instance_resource_type_is_canonical():
    assert INSTANCE_RESOURCE_TYPE == "AWS::EC2::Instance"


def test_source_name_is_aws_ec2():
    """The source name is what the runner's RULE_SOURCES uses to look
    up scope-completeness. Must be distinct from aws_rds so an RDS scan
    doesn't accidentally prove an EC2 scope (or vice-versa)."""
    assert SOURCE_NAME == "aws_ec2"
    assert SOURCE_NAME != "aws_rds"


# ---------------------------------------------------------------------------
# collect_volumes: paginator + region tag
# ---------------------------------------------------------------------------


def test_collect_volumes_yields_each_volume_with_region():
    """The paginator's first page is consumed; each volume is yielded
    with a `_region` key matching the request. Multiple pages get
    flattened into one stream (the collector doesn't care about pages)."""
    v1 = _make_volume(volume_id="vol-1")
    v2 = _make_volume(volume_id="vol-2")
    v3 = _make_volume(volume_id="vol-3")

    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"Volumes": [v1, v2]},
        {"Volumes": [v3]},
    ]
    client.get_paginator.return_value = paginator

    session = MagicMock()
    session.client.return_value = client

    out = list(collect_volumes(session, regions=["eu-west-1"]))

    assert len(out) == 3
    assert [v["VolumeId"] for v in out] == ["vol-1", "vol-2", "vol-3"]
    for v in out:
        assert v["_region"] == "eu-west-1"


def test_collect_volumes_handles_empty_response():
    """An account with no volumes yields nothing (no exception)."""
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Volumes": []}]
    client.get_paginator.return_value = paginator

    session = MagicMock()
    session.client.return_value = client

    assert list(collect_volumes(session, regions=["eu-west-1"])) == []


# ---------------------------------------------------------------------------
# volume_to_resource: identity fields
# ---------------------------------------------------------------------------


def test_volume_to_resource_uses_volume_id_as_native_id():
    vol = _make_volume(volume_id="vol-xyz")
    vol["_region"] = "eu-west-1"  # the collector injects this when iterating
    res = volume_to_resource(vol, "111111111111")
    assert res.native_id == "vol-xyz"
    assert res.region == vol["_region"]
    assert res.resource_type == VOLUME_RESOURCE_TYPE
    assert res.account_id == "111111111111"


# ---------------------------------------------------------------------------
# volume_to_facts: 9 facts (size, type, state, encrypted, iops, throughput,
# attached_instance_id, attached_device, create_time). All KNOWN when input
# is complete; UNKNOWN when the source field is missing.
# ---------------------------------------------------------------------------


def test_volume_to_facts_emits_all_nine_keys_known():
    vol = _make_volume(
        size=500,
        volume_type="gp3",
        state="in-use",
        encrypted=True,
        iops=3000,
        throughput=125,
        instance_id="i-abc",
        device="/dev/sda1",
    )
    observed_at = datetime(2026, 7, 18, tzinfo=UTC)
    rid = uuid4()

    facts = volume_to_facts(rid, "111111111111", vol, observed_at)

    by_key = {f"{f.namespace}.{f.key}": f for f in facts}
    assert len(facts) == 9

    # Every fact carries the EC2 source and the observation timestamp.
    for f in facts:
        assert f.source == "aws_ec2"
        assert f.observed_at == observed_at
        assert f.value_state == ValueState.KNOWN

    # Spot-check values
    assert by_key["aws.ec2.volume.size_gb"].value == 500
    assert by_key["aws.ec2.volume.volume_type"].value == "gp3"
    assert by_key["aws.ec2.volume.state"].value == "in-use"
    assert by_key["aws.ec2.volume.encrypted"].value is True
    assert by_key["aws.ec2.volume.iops"].value == 3000
    assert by_key["aws.ec2.volume.throughput"].value == 125
    assert by_key["aws.ec2.volume.attached_instance_id"].value == "i-abc"
    assert by_key["aws.ec2.volume.attached_device"].value == "/dev/sda1"
    assert by_key["aws.ec2.volume.create_time"].value is not None


def test_volume_to_facts_marks_missing_fields_unknown():
    """An unattached volume (no attachments) has no attached_instance_id.
    That fact must be UNKNOWN, not KNOWN-with-None — the rule's gate
    treats them differently. The volume itself is still KNOWN."""
    vol = _make_volume(
        instance_id=None,
        device=None,
        iops=None,
        throughput=None,
    )
    facts = volume_to_facts(uuid4(), "111111111111", vol, datetime.now(tz=UTC))
    by_key = {f"{f.namespace}.{f.key}": f for f in facts}

    assert by_key["aws.ec2.volume.attached_instance_id"].value_state == ValueState.UNKNOWN
    assert by_key["aws.ec2.volume.attached_device"].value_state == ValueState.UNKNOWN
    # iops/throughput None is also UNKNOWN (gp2 doesn't have those fields)
    assert by_key["aws.ec2.volume.iops"].value_state == ValueState.UNKNOWN
    assert by_key["aws.ec2.volume.throughput"].value_state == ValueState.UNKNOWN
    # size, type, state, encrypted, create_time are still KNOWN
    assert by_key["aws.ec2.volume.size_gb"].value_state == ValueState.KNOWN
    assert by_key["aws.ec2.volume.volume_type"].value_state == ValueState.KNOWN
    assert by_key["aws.ec2.volume.state"].value_state == ValueState.KNOWN
    assert by_key["aws.ec2.volume.encrypted"].value_state == ValueState.KNOWN
    assert by_key["aws.ec2.volume.create_time"].value_state == ValueState.KNOWN


def test_volume_to_facts_uses_correct_namespace():
    """All facts are namespaced under aws.ec2.volume.* — never aws.rds.*.
    A namespace drift would silently break the ebs_gp2_to_gp3 rule (it
    reads aws.ec2.volume.*)."""
    facts = volume_to_facts(uuid4(), "111111111111", _make_volume(), datetime.now(tz=UTC))
    namespaces = {f.namespace for f in facts}
    assert namespaces == {"aws.ec2.volume"}


# ---------------------------------------------------------------------------
# volume_to_observation: full payload preserved
# ---------------------------------------------------------------------------


def test_volume_to_observation_preserves_payload():
    vol = _make_volume(
        volume_id="vol-1",
        volume_type="io1",
        size=200,
        instance_id="i-abc",
        device="/dev/sdf",
    )
    observed_at = datetime(2026, 7, 18, tzinfo=UTC)
    obs = volume_to_observation(uuid4(), vol, observed_at)

    assert obs.source == "aws_ec2"
    assert obs.observed_at == observed_at
    assert obs.payload["VolumeId"] == "vol-1"
    assert obs.payload["VolumeType"] == "io1"
    assert obs.payload["State"] == "in-use"
    assert obs.payload["Size"] == 200
    assert obs.payload["AvailabilityZone"] == "eu-west-1a"
    assert obs.payload["Attachments"][0]["InstanceId"] == "i-abc"
    assert obs.payload["Attachments"][0]["Device"] == "/dev/sdf"


# ---------------------------------------------------------------------------
# Snapshot and instance mappers: smoke tests (full coverage lives in
# the rule tests; these are guard rails against schema drift).
# ---------------------------------------------------------------------------


def test_snapshot_to_resource_uses_snapshot_id():
    snap = _make_snapshot(snapshot_id="snap-1")
    snap["_region"] = "eu-west-1"  # the collector injects this when iterating
    res = snapshot_to_resource(snap, "111111111111")
    assert res.native_id == "snap-1"
    assert res.resource_type == SNAPSHOT_RESOURCE_TYPE
    assert res.region == snap["_region"]


def test_snapshot_to_observation_preserves_keys():
    snap = _make_snapshot(
        snapshot_id="snap-1",
        volume_size=50,
        volume_id="vol-abc",
        encrypted=True,
    )
    obs = snapshot_to_observation(uuid4(), snap, datetime.now(tz=UTC))
    assert obs.source == "aws_ec2"
    assert obs.payload["SnapshotId"] == "snap-1"
    assert obs.payload["VolumeSize"] == 50
    assert obs.payload["VolumeId"] == "vol-abc"
    assert obs.payload["Encrypted"] is True


def test_instance_to_resource_uses_instance_id():
    inst = _make_instance(instance_id="i-1", state="stopped")
    inst["_region"] = "eu-west-1"  # the collector injects this when iterating
    res = instance_to_resource(inst, "111111111111")
    assert res.native_id == "i-1"
    assert res.resource_type == INSTANCE_RESOURCE_TYPE
    assert res.region == inst["_region"]


def test_instance_to_observation_preserves_state_name():
    """The state field is a nested dict in the AWS response; we extract
    just the .Name (string) for the observation payload so the rule
    can read it without unwrapping."""
    inst = _make_instance(state="stopped")
    obs = instance_to_observation(uuid4(), inst, datetime.now(tz=UTC))
    assert obs.payload["State"] == "stopped"
    assert obs.payload["InstanceType"] == "t3.medium"
    assert obs.source == "aws_ec2"
