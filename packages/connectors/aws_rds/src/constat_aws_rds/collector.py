"""AWS RDS collector.

The caller owns the boto3 Session (so we can support
cross-account AssumeRole). This module only translates AWS API
responses into canonical Resources / Facts / Observations.

Chantier III.3: the retry policy, default region list, the
per-region paginator pattern, and the per-connector `_fact`
closure now live in `constat_core.collectors.aws`. This
connector consumes the lib; the per-resource-type specifics
(the items_extractor, the fact shape, the observation payload)
stay here.

Adding a new AWS resource family for the RDS account = one
`paginate_aws(...)` call + one `*_to_resource` + one
`*_to_facts` + one `*_to_observation`. The retry / pagination /
region plumbing is the lib's job.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any
from uuid import UUID

import boto3
from constat_core.catalog.aws import vcpu_for_instance_class
from constat_core.collectors.aws import (
    known_or_unknown,
    make_fact_builder,
    now_utc,
    paginate_aws,
)
from constat_core.models import Fact, Observation, Resource, ValueState

RDS_RESOURCE_TYPE = "AWS::RDS::DBInstance"
SOURCE_NAME = "aws_rds"

# §III.3: `ADAPTIVE_RETRY_CONFIG` and `DEFAULT_REGIONS` live in
# `constat_core.collectors.aws`. This connector consumes the lib
# via `paginate_aws(...)`; the retry config + region defaults are
# applied inside the lib. The orchestrator (apps/api) imports
# from the lib, not from here.
__all__ = [
    "RDS_RESOURCE_TYPE",
    "SOURCE_NAME",
    "collect_db_instances",
    "db_to_facts",
    "db_to_observation",
    "db_to_resource",
]


def _db_instances_in_page(page: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Flat extractor: each page is `{"DBInstances": [...]}`. The
    generator is a tiny indirection so the `paginate_aws` call
    below is identical in shape to the nested describe_instances
    case in aws_ec2 — one canonical signature per connector."""
    return iter(page.get("DBInstances", []))


def collect_db_instances(
    session: boto3.Session, regions: list[str] | None = None
) -> Iterator[dict[str, Any]]:
    """Yield raw DB instance dicts from AWS RDS API across given regions.

    Each yielded dict has an extra `_region` key (string).

    Uses adaptive retry mode (10 attempts, client-side rate
    limiting, jittered backoff) so a transient throttling blip
    doesn't immediately fail the region. The retry config and
    region defaults are inherited from
    `constat_core.collectors.aws`.
    """
    return paginate_aws(
        session,
        regions,
        service="rds",
        operation="describe_db_instances",
        items_extractor=_db_instances_in_page,
    )


def db_to_resource(db: dict[str, Any], account_id: str) -> Resource:
    """Build a canonical Resource from an RDS DescribeDBInstances item."""
    now = now_utc()
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

    _fact = make_fact_builder(
        namespace="aws.rds",
        source=SOURCE_NAME,
        account_id=account_id,
        observed_at=observed_at,
    )

    return [
        _fact(resource_id, "engine", engine, known_or_unknown(engine)),
        _fact(
            resource_id,
            "engine_version",
            engine_version,
            known_or_unknown(engine_version),
        ),
        _fact(
            resource_id,
            "instance_class",
            instance_class or None,
            known_or_unknown(instance_class),
        ),
        # vcpu is computed from the instance class, not stored on the
        # AWS payload. A class the catalog doesn't know (e.g. a future
        # db.r8g) yields None; UNKNOWN is the honest answer.
        _fact(
            resource_id,
            "vcpu",
            vcpu,
            ValueState.KNOWN if vcpu is not None else ValueState.UNKNOWN,
        ),
        _fact(resource_id, "region", region, known_or_unknown(region)),
    ]


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
