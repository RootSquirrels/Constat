"""Fact definitions registry (V1 — test-time only).

Loads `fact_definitions.yaml` and exposes the entries as a typed
structure. The runtime contract is NOT enforced here (we don't
validate facts on insert). The test
`tests/test_fact_definitions.py` does the enforcement by
cross-checking this registry against the producer and consumer code.

V2: the strategic doc describes a full `FactDefinitionRegistry` table
in Postgres. The YAML here is the source for that migration — each
entry becomes a row, the schema (value_type, allowed_values, etc.)
becomes CHECK constraints.

This module is the public API for the registry:

    from constat_core.catalog.fact_definitions import load_registry
    reg = load_registry()
    reg.get(namespace="aws.rds", key="engine")
    # -> FactDefinition(namespace='aws.rds', key='engine', value_type='string', ...)
    reg.all_keys()
    # -> {('aws.rds', 'engine'), ('aws.rds', 'engine_version'), ...}

The YAML file is loaded once per process; the module caches the
result. Use `load_registry(force_reload=True)` in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

VALID_VALUE_TYPES: frozenset[str] = frozenset(
    {"string", "integer", "decimal", "boolean", "date", "datetime", "json"}
)


@dataclass(frozen=True)
class FactDefinition:
    """One entry from the YAML."""

    namespace: str
    key: str
    value_type: str
    description: str
    producer: str
    consumers: tuple[str, ...]
    allowed_values: tuple[str, ...] = ()
    pattern: str | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    since: str = ""

    def dotted_key(self) -> str:
        return f"{self.namespace}.{self.key}"


@dataclass(frozen=True)
class FactRegistry:
    """The full registry, indexed by (namespace, key)."""

    schema_version: int
    last_reviewed: str
    facts: tuple[FactDefinition, ...] = field(default_factory=tuple)
    _by_key: dict[tuple[str, str], FactDefinition] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # Build the index after the dataclass is frozen.
        object.__setattr__(
            self,
            "_by_key",
            {(f.namespace, f.key): f for f in self.facts},
        )

    def get(self, *, namespace: str, key: str) -> FactDefinition | None:
        return self._by_key.get((namespace, key))

    def all_keys(self) -> set[tuple[str, str]]:
        return set(self._by_key.keys())

    def by_producer(self, producer: str) -> list[FactDefinition]:
        return [f for f in self.facts if f.producer == producer]

    def by_consumer(self, consumer: str) -> list[FactDefinition]:
        return [f for f in self.facts if consumer in f.consumers]


def _parse_entry(raw: dict[str, Any]) -> FactDefinition:
    """Parse one YAML entry into a FactDefinition.

    Raises ValueError on schema violation. The cross-check test relies
    on these errors to fail loudly.
    """
    required = ("namespace", "key", "value_type", "producer")
    missing = [k for k in required if k not in raw]
    if missing:
        raise ValueError(f"Fact entry missing required fields: {missing}")

    value_type = raw["value_type"]
    if value_type not in VALID_VALUE_TYPES:
        raise ValueError(
            f"Invalid value_type {value_type!r} for "
            f"{raw['namespace']}.{raw['key']}; "
            f"must be one of {sorted(VALID_VALUE_TYPES)}"
        )

    description = raw.get("description", "").strip()
    if not description:
        raise ValueError(f"Fact {raw['namespace']}.{raw['key']} has no description")

    consumers = raw.get("consumers") or []
    if not isinstance(consumers, list):
        raise ValueError(f"Fact {raw['namespace']}.{raw['key']} consumers must be a list")

    allowed_values = tuple(raw.get("allowed_values") or ())

    # Numeric bounds only make sense for numeric types.
    minimum = raw.get("minimum")
    maximum = raw.get("maximum")
    if (minimum is not None or maximum is not None) and value_type not in (
        "integer",
        "decimal",
    ):
        raise ValueError(
            f"Fact {raw['namespace']}.{raw['key']}: minimum/maximum only valid "
            f"for numeric value_types, got {value_type!r}"
        )

    return FactDefinition(
        namespace=raw["namespace"],
        key=raw["key"],
        value_type=value_type,
        description=description,
        producer=raw["producer"],
        consumers=tuple(consumers),
        allowed_values=allowed_values,
        pattern=raw.get("pattern"),
        minimum=minimum,
        maximum=maximum,
        since=raw.get("since", ""),
    )


def _yaml_path() -> Path:
    """Locate fact_definitions.yaml next to this module."""
    return Path(__file__).parent / "fact_definitions.yaml"


@lru_cache(maxsize=1)
def load_registry(force_reload: bool = False) -> FactRegistry:
    """Load the registry from YAML. Cached after first call.

    `force_reload=True` is for tests that mutate the YAML between calls.
    """
    if force_reload:
        load_registry.cache_clear()

    raw = yaml.safe_load(_yaml_path().read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("fact_definitions.yaml must be a YAML mapping at the root")

    schema_version = int(raw.get("schema_version", 0))
    if schema_version != 1:
        raise ValueError(
            f"Unsupported fact_definitions.yaml schema_version: {schema_version} (expected 1)"
        )

    facts_raw = raw.get("facts")
    if not isinstance(facts_raw, list):
        raise ValueError("fact_definitions.yaml must have a 'facts' list")

    facts = tuple(_parse_entry(entry) for entry in facts_raw)

    # Enforce uniqueness: no two entries for the same (namespace, key).
    seen: set[tuple[str, str]] = set()
    for f in facts:
        if (f.namespace, f.key) in seen:
            raise ValueError(f"Duplicate entry in fact_definitions.yaml: {f.namespace}.{f.key}")
        seen.add((f.namespace, f.key))

    return FactRegistry(
        schema_version=schema_version,
        last_reviewed=str(raw.get("last_reviewed", "")),
        facts=facts,
    )
