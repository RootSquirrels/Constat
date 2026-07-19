"""AWS RDS collector.

The caller owns the boto3 Session (so we can support cross-account AssumeRole).
This module only translates AWS API responses into canonical Resources / Facts / Observations.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config as BotoConfig
from constat_core.catalog.aws import vcpu_for_instance_class
from constat_core.models import Fact, Observation, Resource, ValueState

RDS_RESOURCE_TYPE = "AWS::RDS::DBInstance"
SOURCE_NAME = "aws_rds"

# Default region set. Tune per tenant.
DEFAULT_REGIONS: list[str] = [
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-central-1",
    "us-east-1",
    "us-east-2",
    "us-west-2",
]

# Adaptive retry mode (roadmap scoreboard "Collecte & résilience"):
# client-side rate limiting kicks in on throttling responses and the
# backoff is jittered by default, so a throttled fleet-wide scan backs
# off smoothly instead of hammering the API in lockstep. 10 attempts
# because a full-region paginated scan must survive a multi-minute
# throttling window. Module constant so callers can override per client.
ADAPTIVE_RETRY_CONFIG = BotoConfig(
    retries={"mode": "adaptive", "max_attempts": 10},
    connect_timeout=10,
    read_timeout=30,
)


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


def collect_db_instances(
    session: boto3.Session, regions: list[str] | None = None
) -> Iterator[dict[str, Any]]:
    """Yield raw DB instance dicts from AWS RDS API across given regions.

    Each yielded dict has an extra `_region` key (string).

    Uses adaptive retry mode (10 attempts, client-side rate limiting,
    jittered backoff) so a transient throttling blip doesn't immediately
    fail the region.
    """
    regions = regions or DEFAULT_REGIONS
    for region in regions:
        client = session.client("rds", region_name=region, config=ADAPTIVE_RETRY_CONFIG)
        paginator = client.get_paginator("describe_db_instances")
        for page in paginator.paginate():
            for db in page["DBInstances"]:
                db["_region"] = region
                yield db


def db_to_resource(db: dict[str, Any], account_id: str) -> Resource:
    """Build a canonical Resource from an RDS DescribeDBInstances item."""
    now = _now_utc()
    return Resource(
        account_id=account_id,
        region=db["_region"],
        resource_type=RDS_RESOURCE_TYPE,
        native_id=db["DBInstanceArn"],
        first_seen_at=now,
        last_seen_at=now,
    )


def db_to_facts(
    resource_id: UUID, account_id: str, db: dict[str, Any], observed_at: datetime
) -> list[Fact]:
    """Convert an RDS DescribeDBInstances item to canonical Facts (aws.rds.*)."""
    instance_class = db.get("DBInstanceClass") or ""
    engine = db.get("Engine")
    engine_version = db.get("EngineVersion")
    vcpu = vcpu_for_instance_class(instance_class)
    # Injected by collect_db_instances; absent only for hand-built rows
    # (replay tooling always sets it). The EOL rules gate on this fact:
    # Extended Support pricing is not region-uniform.
    region = db.get("_region")

    def _fact(key: str, value: Any, state: ValueState) -> Fact:
        return Fact(
            resource_id=resource_id,
            account_id=account_id,
            namespace="aws.rds",
            key=key,
            value=value,
            value_state=state,
            source=SOURCE_NAME,
            observed_at=observed_at,
        )

    facts: list[Fact] = [
        _fact("engine", engine, ValueState.KNOWN if engine else ValueState.UNKNOWN),
        _fact(
            "engine_version",
            engine_version,
            ValueState.KNOWN if engine_version else ValueState.UNKNOWN,
        ),
        _fact(
            "instance_class",
            instance_class or None,
            ValueState.KNOWN if instance_class else ValueState.UNKNOWN,
        ),
        _fact(
            "vcpu",
            vcpu,
            ValueState.KNOWN if vcpu is not None else ValueState.UNKNOWN,
        ),
        _fact(
            "region",
            region,
            ValueState.KNOWN if region else ValueState.UNKNOWN,
        ),
    ]
    return facts


def db_to_observation(resource_id: UUID, db: dict[str, Any], observed_at: datetime) -> Observation:
    """Convert an RDS item to an immutable Observation (full source payload)."""
    create_time = db.get("InstanceCreateTime")
    return Observation(
        resource_id=resource_id,
        source=SOURCE_NAME,
        observed_at=observed_at,
        payload={
            "DBInstanceArn": db.get("DBInstanceArn"),
            "DBInstanceIdentifier": db.get("DBInstanceIdentifier"),
            "Engine": db.get("Engine"),
            "EngineVersion": db.get("EngineVersion"),
            "DBInstanceClass": db.get("DBInstanceClass"),
            "DBInstanceStatus": db.get("DBInstanceStatus"),
            "AllocatedStorage": db.get("AllocatedStorage"),
            "InstanceCreateTime": create_time.isoformat() if create_time else None,
            "MultiAZ": db.get("MultiAZ"),
            "StorageEncrypted": db.get("StorageEncrypted"),
            "DBSubnetGroup": (db.get("DBSubnetGroup") or {}).get("DBSubnetGroupName"),
            "Endpoint": (db.get("Endpoint") or {}).get("Address"),
        },
    )
