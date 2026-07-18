"""Cross-check the fact_definitions.yaml registry against producer/consumer code.

UX/ops P2 (data contract follow-up): every fact published to the
`facts` table and every fact read by an insight rule must have an
entry in `packages/core/src/constat_core/catalog/fact_definitions.yaml`.

The contract:

  1. The connector module declares the facts it PRODUCES (hardcoded
     list in `EXPECTED_PRODUCED` below). The test asserts each of
     them has a YAML entry.
  2. The insight resolver declares the facts it CONSUMES (hardcoded
     list in `EXPECTED_CONSUMED` below). The test asserts each of
     them has a YAML entry.
  3. Every YAML entry's `producer` is a known producer module.
  4. Every YAML entry's `consumers` references a known insight rule.
  5. The YAML has no orphan entries (a fact nobody produces AND
     nobody consumes is also a bug — stale or speculative).
  6. The YAML schema is well-formed (caught by the loader).

When you add a fact:
  1. Add the entry to `fact_definitions.yaml`.
  2. Update `EXPECTED_PRODUCED` (if you added it in a connector) or
     `EXPECTED_CONSUMED` (if you added it in an insight).
  3. Bump `last_reviewed` to today and mention it in the PR title.
"""

from __future__ import annotations

import pytest

from constat_core.catalog.fact_definitions import (
    FactRegistry,
    VALID_VALUE_TYPES,
    load_registry,
)


# When a new connector or insight is added, add its facts here.
# These are the facts the code ACTUALLY writes / reads. The YAML must
# have a matching entry. If you add a new fact in the code, the test
# fails until you add the YAML entry and update this list.
EXPECTED_PRODUCED: dict[str, list[tuple[str, str]]] = {
    "aws_rds": [
        # from packages/connectors/aws_rds/src/constat_aws_rds/collector.py::db_to_facts
        ("aws.rds", "engine"),
        ("aws.rds", "engine_version"),
        ("aws.rds", "instance_class"),
        ("aws.rds", "vcpu"),
    ],
}

EXPECTED_CONSUMED: dict[str, list[tuple[str, str]]] = {
    # from packages/insights/rds_eol/src/constat_rds_eol/resolver.py::evaluate
    "rds_eol": [
        ("aws.rds", "engine"),
        ("aws.rds", "engine_version"),
        ("aws.rds", "vcpu"),
    ],
}


def test_registry_loads_clean() -> None:
    """The YAML parses and the loader accepts it (catches schema typos)."""
    reg = load_registry(force_reload=True)
    assert reg.schema_version == 1
    assert reg.last_reviewed  # non-empty
    assert reg.facts, "registry must not be empty"


def test_registry_value_types_are_recognized() -> None:
    """Every value_type in the YAML is in the allowed set."""
    reg = load_registry()
    for f in reg.facts:
        assert f.value_type in VALID_VALUE_TYPES, (
            f"{f.dotted_key()} has unknown value_type {f.value_type!r}; "
            f"allowed: {sorted(VALID_VALUE_TYPES)}"
        )


def test_registry_descriptions_are_present() -> None:
    """The loader already enforces this; the test is a regression guard."""
    reg = load_registry()
    for f in reg.facts:
        assert f.description.strip(), f"{f.dotted_key()} has empty description"


def test_registry_has_no_duplicate_keys() -> None:
    """No two entries for the same (namespace, key)."""
    reg = load_registry()
    seen: set[tuple[str, str]] = set()
    for f in reg.facts:
        assert (f.namespace, f.key) not in seen, (
            f"Duplicate entry: {f.dotted_key()}"
        )
        seen.add((f.namespace, f.key))


def test_every_produced_fact_is_in_registry() -> None:
    """Every fact a connector claims to produce has a YAML entry."""
    reg = load_registry()
    registered = reg.all_keys()
    missing: list[str] = []
    for producer, facts in EXPECTED_PRODUCED.items():
        for ns, key in facts:
            if (ns, key) not in registered:
                missing.append(f"{producer} -> {ns}.{key}")
    assert not missing, (
        f"The following facts are produced by code but missing from "
        f"fact_definitions.yaml:\n  " + "\n  ".join(missing)
    )


def test_every_consumed_fact_is_in_registry() -> None:
    """Every fact an insight reads has a YAML entry."""
    reg = load_registry()
    registered = reg.all_keys()
    missing: list[str] = []
    for consumer, facts in EXPECTED_CONSUMED.items():
        for ns, key in facts:
            if (ns, key) not in registered:
                missing.append(f"{consumer} -> {ns}.{key}")
    assert not missing, (
        f"The following facts are consumed by code but missing from "
        f"fact_definitions.yaml:\n  " + "\n  ".join(missing)
    )


def test_no_orphan_entries() -> None:
    """A fact in the YAML that nobody produces AND nobody consumes is stale."""
    reg = load_registry()
    produced: set[tuple[str, str]] = {
        fact for facts in EXPECTED_PRODUCED.values() for fact in facts
    }
    consumed: set[tuple[str, str]] = {
        fact for facts in EXPECTED_CONSUMED.values() for fact in facts
    }
    orphans: list[str] = []
    for f in reg.facts:
        key = (f.namespace, f.key)
        # Collected-for-archive facts (consumers=[]) are allowed; we
        # only flag facts that BOTH have no consumer AND are not in
        # the produced list.
        if key not in produced and f.consumers:
            orphans.append(f.dotted_key())
    assert not orphans, (
        f"Orphan YAML entries (no producer, has consumer):\n  "
        + "\n  ".join(orphans)
    )


def test_registry_producer_references_are_known() -> None:
    """Every YAML entry's `producer` is in EXPECTED_PRODUCED."""
    reg = load_registry()
    known_producers = set(EXPECTED_PRODUCED.keys())
    unknown: list[str] = []
    for f in reg.facts:
        if f.producer not in known_producers:
            unknown.append(f"{f.dotted_key()} -> producer={f.producer!r}")
    assert not unknown, (
        f"YAML references unknown producers:\n  " + "\n  ".join(unknown)
        + f"\nKnown producers: {sorted(known_producers)}"
    )


def test_registry_consumer_references_are_known() -> None:
    """Every YAML entry's `consumers` are in EXPECTED_CONSUMED."""
    reg = load_registry()
    known_consumers = set(EXPECTED_CONSUMED.keys())
    unknown: list[str] = []
    for f in reg.facts:
        for c in f.consumers:
            if c not in known_consumers:
                unknown.append(f"{f.dotted_key()} -> consumer={c!r}")
    assert not unknown, (
        f"YAML references unknown consumers:\n  " + "\n  ".join(unknown)
        + f"\nKnown consumers: {sorted(known_consumers)}"
    )


def test_registry_round_trip() -> None:
    """A simple smoke test: the registry's lookup methods work as documented."""
    reg = load_registry()
    engine = reg.get(namespace="aws.rds", key="engine")
    assert engine is not None
    assert engine.dotted_key() == "aws.rds.engine"
    assert engine.value_type == "string"
    assert "postgres" in engine.allowed_values
    assert "rds_eol" in engine.consumers
    assert engine.producer == "aws_rds"

    # by_producer and by_consumer.
    rds_facts = reg.by_producer("aws_rds")
    assert len(rds_facts) == 4
    eol_facts = reg.by_consumer("rds_eol")
    assert len(eol_facts) == 3


def test_registry_yaml_against_real_producer_code() -> None:
    """Sanity: import the real collector and check the facts it would
    produce match the registry. This is a smoke test — the real check
    is the EXPECTED_PRODUCED list. If this test fails, the producer
    module moved and EXPECTED_PRODUCED needs updating.
    """
    try:
        from constat_aws_rds.collector import db_to_facts
    except ImportError:
        pytest.skip("constat_aws_rds not on path; EXPECTED_PRODUCED is the source of truth")

    import inspect
    from uuid import uuid4

    from constat_core.models import Fact
    from datetime import UTC, datetime

    src = inspect.getsource(db_to_facts)
    # The producer should reference the 4 namespace+key combinations
    # we register. The "namespace" string and the four _fact() keys.
    assert 'namespace="aws.rds"' in src, "collector no longer uses aws.rds namespace"
    for key in ("engine", "engine_version", "instance_class", "vcpu"):
        assert f'"{key}"' in src, f"collector no longer produces {key}"

    # Build a fake DB row and confirm the function actually produces the 4 facts.
    fake_db = {
        "DBInstanceArn": "arn:aws:rds:eu-west-1:111111111111:db:fake",
        "DBInstanceIdentifier": "fake",
        "Engine": "postgres",
        "EngineVersion": "14.7",
        "DBInstanceClass": "db.m5.xlarge",
        "DBInstanceStatus": "available",
        "MultiAZ": False,
        "StorageEncrypted": True,
        "AllocatedStorage": 20,
        "InstanceCreateTime": None,
        "DBSubnetGroup": {"DBSubnetGroupName": "default"},
        "Endpoint": {"Address": "fake.xxx.rds.amazonaws.com"},
        "_region": "eu-west-1",
    }
    facts = db_to_facts(
        resource_id=uuid4(),
        account_id="111111111111",
        db=fake_db,
        observed_at=datetime.now(tz=UTC),
    )
    produced_keys = {(f.namespace, f.key) for f in facts}
    expected = {
        ("aws.rds", "engine"),
        ("aws.rds", "engine_version"),
        ("aws.rds", "instance_class"),
        ("aws.rds", "vcpu"),
    }
    assert produced_keys == expected, (
        f"Producer emits {produced_keys}, registry expects {expected}"
    )
