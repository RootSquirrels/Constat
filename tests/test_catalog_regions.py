"""Tests for the per-region EBS pricing grids (packages/core/catalog/ebs.py).

Sources for every number below: https://aws.amazon.com/ebs/pricing/
(region selector), cross-checked against the AWS Price List API bulk
files (AmazonEC2 offer, publication 2026-07-17), reviewed 2026-07-19.
"""

from __future__ import annotations

from constat_core.catalog.ebs import (
    DEFAULT_REGION,
    EBS_PRICING,
    EBS_PRICING_BY_REGION,
    EBS_SNAPSHOT_PRICING,
    EBS_SNAPSHOT_PRICING_BY_REGION,
    ebs_price_per_gb_month,
    ebs_snapshot_price_per_gb_month,
    monthly_storage_cost,
    price_region_exact,
)

ALL_VOLUME_TYPES = ("gp2", "gp3", "io1", "io2", "st1", "sc1", "standard")

# Hand-checked against the AWS Price List (publication 2026-07-17).
EXPECTED_STORAGE = {
    "us-east-1": {
        "gp2": 0.10,
        "gp3": 0.08,
        "io1": 0.125,
        "io2": 0.125,
        "st1": 0.045,
        "sc1": 0.015,
        "standard": 0.05,
    },
    "eu-west-1": {
        "gp2": 0.11,
        "gp3": 0.088,
        "io1": 0.138,
        "io2": 0.138,
        "st1": 0.05,
        "sc1": 0.0168,
        "standard": 0.055,
    },
    "eu-west-3": {
        "gp2": 0.116,
        "gp3": 0.0928,
        "io1": 0.145,
        "io2": 0.145,
        "st1": 0.053,
        "sc1": 0.0174,
        "standard": 0.058,
    },
}

EXPECTED_SNAPSHOT = {
    "us-east-1": {"standard": 0.05, "archive": 0.0125},
    "eu-west-1": {"standard": 0.05, "archive": 0.0125},
    "eu-west-3": {"standard": 0.053, "archive": 0.01325},
}


# ---------------------------------------------------------------------------
# Grid contents
# ---------------------------------------------------------------------------


def test_every_region_grid_covers_all_volume_types() -> None:
    for region, expected in EXPECTED_STORAGE.items():
        grid = EBS_PRICING_BY_REGION[region]
        for volume_type in ALL_VOLUME_TYPES:
            assert volume_type in grid, f"{volume_type} missing in {region}"
            assert grid[volume_type].usd_per_gb_month == expected[volume_type]
            assert grid[volume_type].region == region
            assert grid[volume_type].review_date == "2026-07-19"
            assert grid[volume_type].source_url.startswith("https://aws.amazon.com")


def test_every_region_grid_covers_both_snapshot_tiers() -> None:
    for region, expected in EXPECTED_SNAPSHOT.items():
        grid = EBS_SNAPSHOT_PRICING_BY_REGION[region]
        for tier, rate in expected.items():
            assert grid[tier].usd_per_gb_month == rate
            assert grid[tier].region == region


def test_backward_compatible_aliases_are_us_east_1() -> None:
    """EBS_PRICING / EBS_SNAPSHOT_PRICING stay the us-east-1 grids."""
    assert DEFAULT_REGION == "us-east-1"
    assert EBS_PRICING is EBS_PRICING_BY_REGION["us-east-1"]
    assert EBS_SNAPSHOT_PRICING is EBS_SNAPSHOT_PRICING_BY_REGION["us-east-1"]


# ---------------------------------------------------------------------------
# Region-aware lookup + exactness flag
# ---------------------------------------------------------------------------


def test_catalogued_region_returns_its_own_grid_exact() -> None:
    price = ebs_price_per_gb_month("gp3", "eu-west-3")
    assert price is not None
    assert price.usd_per_gb_month == 0.0928
    assert price.region == "eu-west-3"
    assert price_region_exact("eu-west-3", price) is True


def test_uncatalogued_region_falls_back_to_us_east_1_not_exact() -> None:
    """A region we haven't catalogued prices on the us-east-1 grid and
    the caller can TELL — the demo must admit the fallback, not hide it."""
    price = ebs_price_per_gb_month("gp3", "ap-southeast-2")
    assert price is not None
    assert price.usd_per_gb_month == 0.08
    assert price.region == "us-east-1"
    assert price_region_exact("ap-southeast-2", price) is False


def test_no_region_requested_is_us_east_1_exact() -> None:
    """region=None is the backward-compatible path: us-east-1, exact."""
    price = ebs_price_per_gb_month("gp2")
    assert price is not None
    assert price.region == "us-east-1"
    assert price_region_exact(None, price) is True


def test_unknown_volume_type_returns_none_in_any_region() -> None:
    assert ebs_price_per_gb_month("io99", "eu-west-1") is None
    assert ebs_price_per_gb_month("io99", "ap-southeast-2") is None


def test_snapshot_pricing_is_region_aware() -> None:
    price = ebs_snapshot_price_per_gb_month("archive", "eu-west-3")
    assert price is not None
    assert price.usd_per_gb_month == 0.01325
    assert price_region_exact("eu-west-3", price) is True
    fallback = ebs_snapshot_price_per_gb_month("standard", "eu-central-1")
    assert fallback is not None
    assert fallback.usd_per_gb_month == 0.05
    assert price_region_exact("eu-central-1", fallback) is False


def test_monthly_storage_cost_accepts_region() -> None:
    # 100 GB gp2 in eu-west-1 = 100 * $0.11 = $11.00
    assert monthly_storage_cost("gp2", 100, "eu-west-1") == 11.00
    # region=None keeps the historical behavior: us-east-1.
    assert monthly_storage_cost("gp2", 100) == 10.00
    assert monthly_storage_cost("io99", 100, "eu-west-1") is None
