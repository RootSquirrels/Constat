"""FOCUS 1.0 service-name cross-provider catalog.

Loaded once at module import from `data/focus_service_catalog.yaml`
(a versioned data file, not code — see roadmap-consolidation §II.3).
The catalog maps each provider's native ServiceName to a single
canonical name (e.g. AWS "Amazon Relational Database Service" and
Azure "Azure Database for PostgreSQL" both map to `managed_postgres`).

The canonical is exposed to the rest of the pipeline as
`service_canonical` on FocusCharge / AggregatedFocusCharge. The
provider's native name is preserved as `service` for traceability —
the rule never reads it (the grep-pin CI makes that guarantee).

Adding a service = one block in the YAML. No Python change.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

# Path resolution: the YAML ships next to this module so the package
# is self-contained and `pip install -e .` / `uv sync` both pick it up.
_DATA_PATH = Path(__file__).parent / "data" / "focus_service_catalog.yaml"


@dataclass(frozen=True)
class ServiceMapping:
    """One canonical service and the native names that map to it.

    `category` is the broad service family (database, compute, storage,
    ...) used for grouping in the UI; it is not the storage key. The
    storage key is `canonical`.
    """

    canonical: str
    category: str
    # provider -> set of native names that map to this canonical
    providers: dict[str, frozenset[str]]


class ServiceCatalog:
    """The cross-provider service catalog.

    Lookup: `canonical_for(provider, native_name) -> str | None`.
    Returns None when the (provider, name) pair is not in the catalog
    — the loader treats that as "service stays native; no canonical
    available", and the aggregator falls back to the native name for
    the dedup key.
    """

    def __init__(self, mappings: list[ServiceMapping]) -> None:
        self._mappings = tuple(mappings)
        # Reverse index: (provider, native_name_lower) -> canonical.
        # Lower-cased to make the lookup case-insensitive — FOCUS
        # exports vary in case across providers and we don't want the
        # catalog to be a case-sensitivity minefield.
        self._by_native: dict[tuple[str, str], str] = {}
        for m in mappings:
            for provider, names in m.providers.items():
                for name in names:
                    key = (provider, name.lower())
                    if key in self._by_native and self._by_native[key] != m.canonical:
                        raise ValueError(
                            f"service catalog: ({provider!r}, {name!r}) is mapped "
                            f"to both {self._by_native[key]!r} and {m.canonical!r}; "
                            f"each native name must belong to one canonical"
                        )
                    self._by_native[key] = m.canonical

    def canonical_for(self, provider: str, native_name: str) -> str | None:
        """Resolve a (provider, native ServiceName) to the canonical.

        Returns None when the pair is not in the catalog — the loader
        keeps the native name in that case so the caller can decide
        whether to fail loud or accept the data with no canonical.
        """
        return self._by_native.get((provider, native_name.lower()))

    @property
    def canonicals(self) -> tuple[str, ...]:
        """The list of canonical service names, in catalog order.

        Used by the conformance tests to assert "every row in the
        golden exports has a canonical" without enumerating them in
        the test.
        """
        return tuple(m.canonical for m in self._mappings)


@lru_cache(maxsize=1)
def _load_catalog() -> ServiceCatalog:
    """Read the YAML once, cache the result.

    `lru_cache` keeps a single ServiceCatalog instance per process;
    the YAML is small (a few hundred bytes) and read once at module
    import. Reloading (tests that want a custom catalog) can call
    `ServiceCatalogCache.clear()`.
    """
    raw = yaml.safe_load(_DATA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "services" not in raw:
        raise ValueError(
            f"FOCUS service catalog at {_DATA_PATH} is malformed: "
            f"expected a top-level 'services' key, got {type(raw).__name__}"
        )
    mappings: list[ServiceMapping] = []
    for entry in raw["services"]:
        if not isinstance(entry, dict):
            raise ValueError(
                f"service catalog: each entry must be a mapping, got {type(entry).__name__}"
            )
        canonical = entry.get("canonical")
        category = entry.get("category", "")
        providers_raw = entry.get("providers", {})
        if not canonical or not isinstance(canonical, str):
            raise ValueError(
                f"service catalog: each entry needs a 'canonical' string, got {entry!r}"
            )
        if not isinstance(providers_raw, dict):
            raise ValueError(
                f"service catalog: 'providers' must be a mapping, got {type(providers_raw).__name__}"
            )
        providers: dict[str, frozenset[str]] = {}
        for prov, names in providers_raw.items():
            if not isinstance(names, list):
                raise ValueError(
                    f"service catalog: providers.{prov} must be a list, got {type(names).__name__}"
                )
            providers[prov] = frozenset(names)
        mappings.append(
            ServiceMapping(
                canonical=canonical,
                category=category,
                providers=providers,
            )
        )
    return ServiceCatalog(mappings)


def get_catalog() -> ServiceCatalog:
    """The process-wide catalog. Loads the YAML on first call."""
    return _load_catalog()


def clear_cache() -> None:
    """Drop the cached catalog. Test-only — production code never resets it."""
    _load_catalog.cache_clear()
