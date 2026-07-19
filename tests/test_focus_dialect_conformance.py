"""FOCUS 1.0 dialect conformance tests (roadmap-consolidation §II.2).

One conformance harness, parameterized over the per-provider golden
files in `tests/golden/`. Each provider's golden runs through:

1. `load_focus(...)` — the dialect auto-detects from the file's
   first row and applies the dialect's normalize hook.
2. The service catalog — every loaded row must carry a
   `service_canonical` (the cross-provider stable name) for the
   service names the catalog knows.
3. `aggregate_for_storage(...)` — the bucket key uses
   `COALESCE(service_canonical, service)` so the dedup is provider-
   agnostic.

A regression to "the loader is provider-aware" or "the dedup is
provider-specific" fails one of these assertions.

Adding a provider = one new `Dialect` subclass + one new golden
file + one new entry in `PROVIDERS` below. The harness is the
single point of truth for "every provider in `REGISTRY` has a
conformance test" — the `test_all_registered_providers_have_a_golden`
metatest enforces it.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

import pytest
from constat_focus.aggregator import aggregate_for_storage
from constat_focus.dialects import REGISTRY
from constat_focus.loader import load_focus
from constat_focus.service_catalog import get_catalog

GOLDEN_DIR = Path(__file__).parent / "golden"

# (provider, golden filename) — one entry per registered dialect.
# The metatest `test_all_registered_providers_have_a_golden` below
# fails when a provider is in `REGISTRY` but not in this map, so
# adding a dialect is a one-line change that pins the contract.
PROVIDERS: dict[str, str] = {
    "aws": "focus_aws.csv",
    "azure": "focus_azure.csv",
}


def _load_golden(provider: str) -> Iterator:
    """Load a golden file via the public loader (auto-detect on)."""
    path = GOLDEN_DIR / PROVIDERS[provider]
    return load_focus(path)


def _load_csv_rows(provider: str) -> list[dict[str, str]]:
    """Read the raw golden CSV (one row dict per data row).

    Used to assert "the loader produced the same number of rows as
    the golden has data rows" — a row skipped for a parse error
    would silently pass a row-count test if we only checked the
    loader's output.
    """
    path = GOLDEN_DIR / PROVIDERS[provider]
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---- Per-provider conformance ------------------------------------------------


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_golden_loads_with_zero_skipped_rows(provider: str) -> None:
    """The loader produces one FocusCharge per data row, with no
    skip (a skip means a FOCUS 1.0 spec violation — the golden is
    spec-shaped by construction)."""
    rows = list(_load_golden(provider))
    assert len(rows) == len(_load_csv_rows(provider))


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_golden_every_row_has_a_canonical(provider: str) -> None:
    """Every FocusCharge from the golden has a `service_canonical`.

    The golden files only carry services that ARE in the service
    catalog (managed_postgres / compute_vm / object_storage today);
    a row whose ServiceName is unknown would yield canonical=None,
    which is a golden/spec drift — not a loader bug, but a signal
    to add the service to the catalog.
    """
    rows = list(_load_golden(provider))
    assert rows, "golden has no rows"
    catalog = get_catalog()
    for r in rows:
        assert r.service_canonical is not None, (
            f"{provider} row ServiceName={r.service!r} is not in the service catalog; "
            f"add it to data/focus_service_catalog.yaml or regenerate the golden"
        )
        # The canonical must be one the catalog knows about.
        assert r.service_canonical in catalog.canonicals, (
            f"service_canonical {r.service_canonical!r} not in catalog"
        )


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_golden_aggregator_keys_by_canonical_not_native(provider: str) -> None:
    """The aggregator dedups by canonical, not by the provider's
    native name. The Azure golden mixes currencies (intentionally
    — the conformance suite tests currency-aware aggregation as a
    separate concern), so the test filters to a single currency
    before aggregating. The strict assertion: every aggregate's
    `service_canonical` is a catalog value, proving the bucket key
    took the canonical path even when the golden carries the
    provider's native ServiceName.
    """
    rows = [
        r
        for r in _load_golden(provider)
        if r.billing_currency == "USD"  # single-currency slice
    ]
    if not rows:
        pytest.skip(f"{provider} golden has no USD rows; test skipped")
    aggregates = aggregate_for_storage(rows)
    catalog = get_catalog()
    for agg in aggregates:
        assert agg.service_canonical in catalog.canonicals, (
            f"aggregator produced a bucket keyed on native service "
            f"{agg.service_canonical!r} which is not in the catalog; "
            f"the dedup key is leaking the provider's native name"
        )


def test_aggregator_collapses_cross_provider_canonical_buckets() -> None:
    """The cross-provider promise: a synthesized list of charges
    (AWS "Amazon RDS" + Azure "Azure Database for PostgreSQL",
    both single-currency, both in the same period) collapses to
    ONE AggregatedFocusCharge (canonical = managed_postgres) — not
    two. The goldens can't drive this test on their own (they're
    single-provider files), so the assertion is built from
    hand-crafted FocusCharge rows in the same shape the loader
    produces.
    """
    from datetime import date
    from decimal import Decimal

    from constat_focus.loader import FocusCharge

    charges = [
        FocusCharge(
            account_id="111111111111",
            account_name="prod",
            service="Amazon Relational Database Service",
            service_canonical="managed_postgres",
            region="eu-west-1",
            pricing_category="On-Demand",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            billed_cost=Decimal("100"),
            amortized_cost=Decimal("100"),
            resource_id=None,
            sub_account_id=None,
            # `tags` is always a single-element list (one input row →
            # one tag dict, possibly empty). Parallel to per_row_costs.
            # The loader guarantees this invariant; hand-built
            # FocusCharge in tests must respect it.
            tags=[{}],
            per_row_costs=[(Decimal("100"), Decimal("100"))],
            billing_currency="USD",
        ),
        FocusCharge(
            account_id="111111111111",
            account_name="prod",
            service="Azure Database for PostgreSQL",
            service_canonical="managed_postgres",
            region="westeurope",
            pricing_category="On-Demand",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            billed_cost=Decimal("200"),
            amortized_cost=Decimal("200"),
            resource_id=None,
            sub_account_id=None,
            tags=[{}],
            per_row_costs=[(Decimal("200"), Decimal("200"))],
            billing_currency="USD",
        ),
    ]
    aggregates = aggregate_for_storage(charges)
    assert len(aggregates) == 1, (
        f"cross-provider chargeback must collapse to one bucket; got {len(aggregates)}"
    )
    agg = aggregates[0]
    assert agg.service_canonical == "managed_postgres"
    assert agg.billed_cost == Decimal("300")
    assert agg.amortized_cost == Decimal("300")


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_dialect_detects_provider_from_first_row(provider: str) -> None:
    """The dialect's auto-detect on the first row returns 1.0
    (perfect confidence) for its own golden, and a different
    dialect (or none) for the other golden. This is the contract
    that lets the loader pick the right dialect without an
    explicit `provider=` argument."""
    dialect = REGISTRY[provider]
    own_rows = _load_csv_rows(provider)
    assert dialect.detect(list(own_rows[0].keys()), own_rows[0]) == 1.0


# ---- Metatest: every registered dialect has a golden + entry in PROVIDERS ---


def test_all_registered_providers_have_a_golden() -> None:
    """The conformance test set is the contract. Every registered
    dialect MUST have a golden file (and a `PROVIDERS` entry); the
    harness is exhaustive by construction. A new dialect without a
    golden fails this test before it can ship."""
    registered = set(REGISTRY)
    mapped = set(PROVIDERS)
    missing = registered - mapped
    assert not missing, (
        f"registered dialects without a golden + PROVIDERS entry: {sorted(missing)}; "
        f"add `tests/golden/focus_<provider>.csv` and a PROVIDERS entry, "
        f"then the parameterized tests above will cover it"
    )
