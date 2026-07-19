"""Tests for the AWS collector lib (roadmap-consolidation §III.3).

The lib (`constat_core.collectors.aws`) is the single home of
the AWS-side scaffolding the V1 inventory connectors used to
copy verbatim: the adaptive-retry boto3 config, the default
region list, the per-region paginator pattern (with `_region`
injected on every yielded item), and the `_fact` closure
that stamps `namespace` / `source` / `observed_at` on every
fact.

The per-connector test files
(`tests/test_aws_ec2_connector.py`,
`tests/test_collector_resilience.py`) pin the connector-level
behavior. This file pins the LIB-LEVEL behavior: the lib is
the single source of truth, and adding a 3rd connector = one
items_extractor + one *_to_facts, no copy of the retry policy
or region list.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from boto3.session import Session
from botocore.config import Config as BotoConfig
from constat_aws_ec2.collector import (
    collect_instances,
    collect_snapshots,
    collect_volumes,
)
from constat_aws_rds.collector import collect_db_instances
from constat_core.collectors.aws import (
    ADAPTIVE_RETRY_CONFIG,
    DEFAULT_REGIONS,
    known_or_unknown,
    make_fact_builder,
    now_utc,
    paginate_aws,
)
from constat_core.models import ValueState

# ---------------------------------------------------------------------------
# The lib IS the single home of ADAPTIVE_RETRY_CONFIG + DEFAULT_REGIONS
# ---------------------------------------------------------------------------


def test_adaptive_retry_config_is_a_single_object() -> None:
    """The lib's `ADAPTIVE_RETRY_CONFIG` is the canonical boto3
    Config. A new connector (Azure, Prisma, ...) imports from
    here — no copy, no drift pin."""
    assert ADAPTIVE_RETRY_CONFIG.retries == {"mode": "adaptive", "max_attempts": 10}
    assert ADAPTIVE_RETRY_CONFIG.connect_timeout == 10
    assert ADAPTIVE_RETRY_CONFIG.read_timeout == 30


def test_default_regions_covers_the_v1_pilot() -> None:
    """The lib's `DEFAULT_REGIONS` is the canonical list. A new
    region goes here ONLY after a catalog review confirms the
    per-region grids cover it."""
    expected = {
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "eu-central-1",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
    assert set(DEFAULT_REGIONS) == expected


# ---------------------------------------------------------------------------
# paginate_aws: per-region, with `_region` injected on every item
# ---------------------------------------------------------------------------


def _mock_session_with_pages(pages_by_region: dict[str, list[dict[str, Any]]]) -> MagicMock:
    """Build a mock boto3 Session whose `client(svc, region_name=r)` returns
    a client whose `get_paginator(op).paginate(...)` returns the canned
    pages for that region. The simplest shape to test the per-region loop
    in `paginate_aws` without spinning up a real boto3 backend."""
    session = MagicMock(spec=Session)

    def _client(svc: str, region_name: str, config: BotoConfig | None = None) -> MagicMock:
        client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = pages_by_region[region_name]
        client.get_paginator.return_value = paginator
        return client

    session.client.side_effect = _client
    return session


def test_paginate_aws_injects_region_on_every_item() -> None:
    """The `_region` tag is the contract the rules depend on for
    region-aware pricing. A missing tag = silent mis-grid. The
    shared function makes the tag impossible to forget: every
    yielded item carries the region the API call was made in."""
    session = _mock_session_with_pages(
        {
            "eu-west-1": [{"Things": [{"Id": "a"}, {"Id": "b"}]}],
            "us-east-1": [{"Things": [{"Id": "c"}]}],
        }
    )
    items = list(
        paginate_aws(
            session,
            ["eu-west-1", "us-east-1"],
            service="x",
            operation="list_things",
            items_extractor=lambda page: page.get("Things", []),
        )
    )
    assert [i["Id"] for i in items] == ["a", "b", "c"]
    assert [i["_region"] for i in items] == ["eu-west-1", "eu-west-1", "us-east-1"]


def test_paginate_aws_supports_nested_extractors() -> None:
    """The nested `describe_instances` shape
    (`Reservations[*].Instances[*]`) is the one the flat
    extractor can't express. The shared function supports
    nested generators because EC2's paginator is the V1 case
    that needs it."""
    session = _mock_session_with_pages(
        {
            "eu-west-1": [
                {
                    "Reservations": [
                        {"Instances": [{"Id": "i-1"}, {"Id": "i-2"}]},
                        {"Instances": [{"Id": "i-3"}]},
                    ]
                }
            ],
        }
    )
    items = list(
        paginate_aws(
            session,
            ["eu-west-1"],
            service="ec2",
            operation="describe_instances",
            items_extractor=lambda page: (
                inst
                for res in page.get("Reservations", [])
                for inst in res.get("Instances", [])
            ),
        )
    )
    assert [i["Id"] for i in items] == ["i-1", "i-2", "i-3"]
    assert all(i["_region"] == "eu-west-1" for i in items)


def test_paginate_aws_forwards_paginate_args() -> None:
    """`describe_snapshots` needs `OwnerIds=["self"]`. The
    shared function forwards `paginate_args` to the boto3
    paginator so the caller stays in control of API args."""
    session = MagicMock(spec=Session)
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Snapshots": [{"SnapshotId": "s-1"}]}]
    client = MagicMock()
    client.get_paginator.return_value = paginator
    session.client.return_value = client

    list(
        paginate_aws(
            session,
            ["us-east-1"],
            service="ec2",
            operation="describe_snapshots",
            items_extractor=lambda page: page.get("Snapshots", []),
            paginate_args={"OwnerIds": ["self"]},
        )
    )
    paginator.paginate.assert_called_once_with(OwnerIds=["self"])


def test_paginate_aws_uses_default_regions_when_none() -> None:
    """`regions=None` (the common case) defaults to the lib's
    `DEFAULT_REGIONS`. A new tenant that needs a custom region
    set passes an explicit list; the default is the canonical
    V1 pilot set."""
    session = MagicMock(spec=Session)
    paginator = MagicMock()
    paginator.paginate.return_value = [{"DBInstances": []}]
    client = MagicMock()
    client.get_paginator.return_value = paginator
    session.client.return_value = client

    list(
        paginate_aws(
            session,
            None,
            service="rds",
            operation="describe_db_instances",
            items_extractor=lambda page: page.get("DBInstances", []),
        )
    )
    # One client call per default region.
    assert session.client.call_count == len(DEFAULT_REGIONS)


def test_paginate_aws_applies_retry_config_to_every_client() -> None:
    """The lib's `ADAPTIVE_RETRY_CONFIG` is applied to every
    regional client the paginator creates. A connector that
    overrides it (some scan types are smaller-scope) passes
    `retry_config=`; the default is the canonical V1 policy."""
    session = MagicMock(spec=Session)
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Things": []}]
    client = MagicMock()
    client.get_paginator.return_value = paginator
    session.client.return_value = client

    list(
        paginate_aws(
            session,
            ["eu-west-1"],
            service="x",
            operation="list_things",
            items_extractor=lambda page: page.get("Things", []),
        )
    )
    session.client.assert_called_once_with(
        "x", region_name="eu-west-1", config=ADAPTIVE_RETRY_CONFIG
    )


# ---------------------------------------------------------------------------
# V1 connectors consume the lib (regression guard against re-introducing
# the duplicate paginator / retry / region code)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "collect_fn,service,operation,items_key,extra_paginate",
    [
        (collect_db_instances, "rds", "describe_db_instances", "DBInstances", {}),
        (collect_volumes, "ec2", "describe_volumes", "Volumes", {}),
        (
            collect_snapshots,
            "ec2",
            "describe_snapshots",
            "Snapshots",
            {"OwnerIds": ["self"]},
        ),
    ],
)
def test_v1_connectors_consume_paginate_aws(
    collect_fn, service, operation, items_key, extra_paginate
) -> None:
    """The V1 connector's `collect_*` is a thin wrapper around
    `paginate_aws`. A regression that re-introduces the local
    paginator loop (or forgets the `_region` tag) breaks here."""
    session = MagicMock(spec=Session)
    paginator = MagicMock()
    paginator.paginate.return_value = [{items_key: [{"Id": "x"}]}]
    client = MagicMock()
    client.get_paginator.return_value = paginator
    session.client.return_value = client

    items = list(collect_fn(session, regions=["eu-west-1"]))
    assert len(items) == 1
    assert items[0]["Id"] == "x"
    assert items[0]["_region"] == "eu-west-1"
    session.client.assert_called_once_with(
        service, region_name="eu-west-1", config=ADAPTIVE_RETRY_CONFIG
    )
    paginator.paginate.assert_called_once_with(**extra_paginate)


def test_collect_instances_handles_nested_reservations() -> None:
    """The nested extractor: `collect_instances` must flatten
    `Reservations[*].Instances[*]` into a single stream. The
    V1 `ec2.stopped_with_storage` rule reads instance state
    from the flattened items; a regression that loses an
    instance because it lives in the second reservation of
    the page breaks the rule silently."""
    session = MagicMock(spec=Session)
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "Reservations": [
                {"Instances": [{"InstanceId": "i-1"}]},
                {"Instances": [{"InstanceId": "i-2"}, {"InstanceId": "i-3"}]},
            ]
        }
    ]
    client = MagicMock()
    client.get_paginator.return_value = paginator
    session.client.return_value = client

    items = list(collect_instances(session, regions=["eu-west-1"]))
    assert [i["InstanceId"] for i in items] == ["i-1", "i-2", "i-3"]


# ---------------------------------------------------------------------------
# make_fact_builder: stamps namespace/source/account/observed_at per call
# ---------------------------------------------------------------------------


def test_make_fact_builder_stamps_namespace_and_source() -> None:
    """The lib's `make_fact_builder` returns a closure that
    stamps the connector's namespace + source on every fact.
    The per-connector `*_to_facts` function calls the builder
    per fact; the builder takes care of the constant fields."""
    _fact = make_fact_builder(
        namespace="aws.ec2.volume",
        source="aws_ec2",
        account_id="111111111111",
        observed_at=datetime(2026, 7, 19, tzinfo=UTC),
    )
    fact = _fact(uuid4(), "size_gb", 100, ValueState.KNOWN)
    assert fact.namespace == "aws.ec2.volume"
    assert fact.source == "aws_ec2"
    assert fact.account_id == "111111111111"
    assert fact.observed_at == datetime(2026, 7, 19, tzinfo=UTC)
    assert fact.key == "size_gb"
    assert fact.value == 100
    assert fact.value_state == ValueState.KNOWN


# ---------------------------------------------------------------------------
# known_or_unknown: the per-fact state heuristic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ValueState.UNKNOWN),
        ("", ValueState.UNKNOWN),
        ([], ValueState.UNKNOWN),
        ({}, ValueState.UNKNOWN),
        ("gp2", ValueState.KNOWN),
        (0, ValueState.KNOWN),
        (False, ValueState.KNOWN),
        (100, ValueState.KNOWN),
        (["a", "b"], ValueState.KNOWN),
        ({"k": "v"}, ValueState.KNOWN),
    ],
)
def test_known_or_unknown(value, expected) -> None:
    """The per-fact state helper. `None` and empty containers
    are UNKNOWN (the fact was not observed); everything else
    is KNOWN. Used by the V1 collectors to replace the inline
    `KNOWN if X else UNKNOWN` ternary."""
    assert known_or_unknown(value) is expected


# ---------------------------------------------------------------------------
# now_utc: the lib's single "now"
# ---------------------------------------------------------------------------


def test_now_utc_returns_aware_utc() -> None:
    """`now_utc` is the canonical clock in the collector layer.
    A future test that wants to freeze time patches this one
    function; collectors MUST call it (not `datetime.now(tz=UTC)`
    inline) so the patch is effective."""
    now = now_utc()
    assert now.tzinfo is UTC
