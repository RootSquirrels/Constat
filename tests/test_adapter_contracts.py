"""Adapter-contract conformance tests (ADR-14).

Proves the two contracts that have a V1 implementation are actually
satisfied by the existing connectors, that a non-connector does not
accidentally satisfy them, and that the import direction
(packages/* never imports apps/*) holds.

The V1 connectors ship as module-level functions, not classes. The
honest structural check is therefore a thin wrapper (a frozen dataclass)
that points the protocol's members at the module's functions, then an
`isinstance` check against the `@runtime_checkable` protocol — plus a
functional smoke call on each factory, because `runtime_checkable` only
verifies member presence, not signatures.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import constat_aws_ec2.collector as ec2_collector
import constat_aws_rds.collector as rds_collector
import constat_rds_eol.resolver as rds_eol_resolver
import pytest
from constat_core.adapters import CostAdapter, InventoryAdapter
from constat_core.models import Fact, Observation, Resource
from constat_focus.loader import FocusCharge, load_focus

REPO_ROOT = Path(__file__).resolve().parents[1]
FOCUS_FIXTURE = REPO_ROOT / "tests" / "golden" / "focus_aws.csv"

NOW = datetime.now(tz=UTC)
ACCOUNT_ID = "111111111111"


@dataclass(frozen=True)
class _InventoryView:
    """Adapter-shaped view over a collector's module-level functions."""

    source_name: str
    collect: Callable[..., Iterator[dict[str, Any]]]
    to_resource: Callable[[dict[str, Any], str], Resource]
    to_facts: Callable[[UUID, str, dict[str, Any], datetime], list[Fact]]
    to_observation: Callable[[UUID, dict[str, Any], datetime], Observation]


@dataclass(frozen=True)
class _CostView:
    """Adapter-shaped view over the FOCUS loader's module-level function."""

    source_name: str
    load: Callable[..., Iterator[FocusCharge]]


def _rds_view() -> _InventoryView:
    return _InventoryView(
        source_name=rds_collector.SOURCE_NAME,
        collect=rds_collector.collect_db_instances,
        to_resource=rds_collector.db_to_resource,
        to_facts=rds_collector.db_to_facts,
        to_observation=rds_collector.db_to_observation,
    )


def _ec2_views() -> dict[str, _InventoryView]:
    return {
        "volume": _InventoryView(
            source_name=ec2_collector.SOURCE_NAME,
            collect=ec2_collector.collect_volumes,
            to_resource=ec2_collector.volume_to_resource,
            to_facts=ec2_collector.volume_to_facts,
            to_observation=ec2_collector.volume_to_observation,
        ),
        "snapshot": _InventoryView(
            source_name=ec2_collector.SOURCE_NAME,
            collect=ec2_collector.collect_snapshots,
            to_resource=ec2_collector.snapshot_to_resource,
            to_facts=ec2_collector.snapshot_to_facts,
            to_observation=ec2_collector.snapshot_to_observation,
        ),
        "instance": _InventoryView(
            source_name=ec2_collector.SOURCE_NAME,
            collect=ec2_collector.collect_instances,
            to_resource=ec2_collector.instance_to_resource,
            to_facts=ec2_collector.instance_to_facts,
            to_observation=ec2_collector.instance_to_observation,
        ),
    }


def _smoke_inventory_view(view: _InventoryView, raw: dict[str, Any]) -> None:
    """Call every factory once: presence (isinstance) is not a signature proof."""
    resource = view.to_resource(raw, ACCOUNT_ID)
    assert isinstance(resource, Resource)
    facts = view.to_facts(resource.id, ACCOUNT_ID, raw, NOW)
    assert facts and all(isinstance(f, Fact) for f in facts)
    assert all(f.source == view.source_name for f in facts)
    observation = view.to_observation(resource.id, raw, NOW)
    assert isinstance(observation, Observation)
    assert observation.source == view.source_name


def test_aws_rds_collector_satisfies_inventory_adapter() -> None:
    view = _rds_view()
    assert isinstance(view, InventoryAdapter)
    assert view.source_name == "aws_rds"
    _smoke_inventory_view(
        view,
        {
            "DBInstanceArn": "arn:aws:rds:eu-west-1:111111111111:db:myapp",
            "DBInstanceIdentifier": "myapp",
            "Engine": "postgres",
            "EngineVersion": "13.4",
            "DBInstanceClass": "db.t3.medium",
            "_region": "eu-west-1",
        },
    )


@pytest.mark.parametrize("kind", ["volume", "snapshot", "instance"])
def test_aws_ec2_collector_satisfies_inventory_adapter(kind: str) -> None:
    view = _ec2_views()[kind]
    assert isinstance(view, InventoryAdapter)
    assert view.source_name == "aws_ec2"
    raw_items: dict[str, dict[str, Any]] = {
        "volume": {
            "VolumeId": "vol-0123456789abcdef0",
            "VolumeType": "gp2",
            "State": "available",
            "Size": 100,
            "_region": "eu-west-1",
        },
        "snapshot": {
            "SnapshotId": "snap-0123456789abcdef0",
            "State": "completed",
            "VolumeSize": 100,
            "VolumeId": "vol-gone",
            "_region": "eu-west-1",
        },
        "instance": {
            "InstanceId": "i-0123456789abcdef0",
            "InstanceType": "t3.medium",
            "State": {"Name": "stopped"},
            "_region": "eu-west-1",
        },
    }
    _smoke_inventory_view(view, raw_items[kind])


def test_focus_loader_satisfies_cost_adapter() -> None:
    view = _CostView(source_name="focus", load=load_focus)
    assert isinstance(view, CostAdapter)
    charges = list(view.load(FOCUS_FIXTURE))
    assert charges and all(isinstance(c, FocusCharge) for c in charges)
    assert all(c.billing_currency for c in charges)


def test_insight_resolver_is_not_an_inventory_adapter() -> None:
    """Negative control: an insight resolver consumes Facts; it is not a connector."""
    assert not isinstance(rds_eol_resolver, InventoryAdapter)
    assert not isinstance(rds_eol_resolver, CostAdapter)


def _imported_top_level_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_packages_never_import_apps() -> None:
    """Import-direction guard: packages/* must not import constat_api (apps/*)."""
    offenders: list[str] = []
    for package_root in (
        REPO_ROOT / "packages" / "connectors",
        REPO_ROOT / "packages" / "insights",
    ):
        for path in sorted(package_root.rglob("*.py")):
            if "constat_api" in _imported_top_level_modules(path):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"packages importing constat_api: {offenders}"
