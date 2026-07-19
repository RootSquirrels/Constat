"""AWS collector helpers shared by the inventory connectors.

Single home of the AWS-side scaffolding the two V1 inventory
connectors (`constat_aws_rds`, `constat_aws_ec2`) used to copy
verbatim: the adaptive-retry boto3 config, the default region
list, the per-region paginator pattern (with `_region` injected
on every yielded item), and the `_fact` closure that stamps
`namespace` / `source` / `observed_at` on every fact.

Chantier III.3 of the roadmap consolidation, under the
adapter-contracts frame of ADR-14. ADR-14 said "existing
connector code is untouched: conformance is proven by wrapping,
not by editing the collectors" — true for the Protocol
conformance check (no edits needed there). §III.3 is the next
step: the connectors consume THIS lib, and the duplicate
copy/paste (and its drift-pin in
`tests/test_contract_pins.py::test_adaptive_retry_config_parity_between_connectors`)
goes away.

A new inventory connector = `paginate_aws` for each resource
type + `make_fact_builder` for each fact namespace. The
retry policy and the default region list are imported from
here, not redefined.

The `_region` tag the paginator injects is what every rule reads
to pick the right catalog grid (EBS storage pricing and RDS
Extended Support pricing are both region-uniform WRONG — they
vary by region). Removing the tag = silent mis-grid. The
shared function makes the tag impossible to forget.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config as BotoConfig

from constat_core.models import Fact, ValueState

# Default region set. Same default for both V1 connectors
# (`aws_rds`, `aws_ec2`); tunable per tenant via the function
# argument. Ordered to match the pilot's V1 region set: 3 EU + 1
# EU-Central + 3 US. A new region goes here ONLY after a catalog
# review confirms the per-region grids cover it (EBS / RDS ES
# pricing both vary by region).
DEFAULT_REGIONS: list[str] = [
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-central-1",
    "us-east-1",
    "us-east-2",
    "us-west-2",
]

# Adaptive retry policy (roadmap scoreboard "Collecte &
# resilience"): client-side rate limiting kicks in on throttling
# responses and the backoff is jittered by default, so a throttled
# fleet-wide scan backs off smoothly instead of hammering the API
# in lockstep. 10 attempts because a full-region paginated scan
# must survive a multi-minute throttling window. Module constant
# so callers can override per client (some scan types are
# smaller-scope and accept a tighter config).
ADAPTIVE_RETRY_CONFIG = BotoConfig(
    retries={"mode": "adaptive", "max_attempts": 10},
    connect_timeout=10,
    read_timeout=30,
)


def now_utc() -> datetime:
    """The single home of 'now' in the collector layer. Centralized
    so tests can patch it; trivial to swap for an injected clock if
    we ever need that."""
    return datetime.now(tz=UTC)


# An items_extractor pulls the per-item sequence out of one
# paginator page. Flat pages use `page.get("DBInstances", [])`
# (a list of dicts); the nested describe_instances case uses a
# generator that walks Reservations[*].Instances[*]. Callable
# keeps both shapes under one signature.
ItemsExtractor = Callable[[dict[str, Any]], Iterable[dict[str, Any]]]


def paginate_aws(
    session: boto3.Session,
    regions: list[str] | None = None,
    *,
    service: str,
    operation: str,
    items_extractor: ItemsExtractor,
    paginate_args: dict[str, Any] | None = None,
    retry_config: BotoConfig | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield items from a paginated AWS API call across regions.

    Each yielded dict is the AWS response item, with an extra
    `_region` key (the region the API call was made in). The
    `_region` tag is what every rule reads to pick the right
    catalog grid — pricing that varies by region (EBS storage,
    RDS Extended Support) cannot be honestly priced without it.

    `items_extractor` is the only part the caller writes per
    resource type. Flat pages pass `page.get("<Key>", [])`;
    the nested describe_instances case passes a generator
    that walks the wrapper level. Everything else (session
    client, paginator, `_region` injection, default regions,
    retry config) is shared.

    `paginate_args` flows to `paginator.paginate(**paginate_args)`
    — e.g. `{"OwnerIds": ["self"]}` for
    DescribeSnapshots. The caller stays in control of the API
    call's parameters.

    `retry_config` defaults to `ADAPTIVE_RETRY_CONFIG`; pass an
    override for tighter (or looser) throttling windows.
    """
    regions = regions if regions is not None else DEFAULT_REGIONS
    paginate_args = paginate_args or {}
    config = retry_config if retry_config is not None else ADAPTIVE_RETRY_CONFIG
    for region in regions:
        client = session.client(service, region_name=region, config=config)
        paginator = client.get_paginator(operation)
        for page in paginator.paginate(**paginate_args):
            for item in items_extractor(page):
                item["_region"] = region
                yield item


# A fact builder stamps the constant fields of a Fact
# (namespace, source, account_id, observed_at) and leaves
# resource_id, key, value, value_state to the caller. The
# closure shape avoids passing the same 4 fields through
# every `_fact(...)` call inside a 10-key fact emitter.
FactBuilder = Callable[[UUID, str, Any, ValueState], Fact]


def make_fact_builder(
    *,
    namespace: str,
    source: str,
    account_id: str,
    observed_at: datetime,
) -> FactBuilder:
    """Return a `_fact` closure that stamps the connector's
    namespace + source on every fact it builds.

    `resource_id` and the per-fact `key` / `value` /
    `value_state` are passed per call; the rest is bound at
    factory time. The 3 V1 connectors each have one
    `make_fact_builder(namespace=..., source=SOURCE_NAME, ...)`
    call per resource type; the per-fact emissions are
    one-liners.
    """
    def _build(resource_id: UUID, key: str, value: Any, state: ValueState) -> Fact:
        return Fact(
            resource_id=resource_id,
            account_id=account_id,
            namespace=namespace,
            key=key,
            value=value,
            value_state=state,
            source=source,
            observed_at=observed_at,
        )

    return _build


def known_or_unknown(value: Any) -> ValueState:
    """Return `KNOWN` if `value` is present, `UNKNOWN` if missing
    or empty.

    The V1 collectors used the ternary
    `ValueState.KNOWN if X else ValueState.UNKNOWN` inline at
    every fact site; the rules' gates interpret UNKNOWN as
    "we don't have this fact, INCONCLUSIVE" and KNOWN as
    "we have a real value". `present` here means not None
    and (for containers) non-empty.

    Not a replacement for an explicit override — a few facts
    are "always KNOWN" by design (e.g. EC2 instance
    `block_device_volume_ids` — an empty list is a real
    observation of "no EBS volumes", not a gap). The
    connector passes `ValueState.KNOWN` explicitly for those.
    """
    if value is None:
        return ValueState.UNKNOWN
    if isinstance(value, (str, list, dict)) and len(value) == 0:
        return ValueState.UNKNOWN
    return ValueState.KNOWN
