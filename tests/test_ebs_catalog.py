"""Tests for the EBS pricing catalog (packages/core/catalog/ebs.py)."""

from __future__ import annotations

from constat_core.catalog.ebs import (
    EBS_CATALOG_VERSION,
    EBS_PRICING,
    ebs_price_per_gb_month,
    ebs_snapshot_price_per_gb_month,
    monthly_storage_cost,
)

# ---------------------------------------------------------------------------
# Catalog version
# ---------------------------------------------------------------------------


def test_catalog_version_is_a_string():
    """The version stamp is stamped on every ebs_gp2_to_gp3 insight
    payload. It must be a string (so it serializes to JSON), and a date
    the sales conversation can cite ('based on EBS pricing dated YYYY-MM-DD')."""
    assert isinstance(EBS_CATALOG_VERSION, str)
    assert len(EBS_CATALOG_VERSION) == 10
    assert EBS_CATALOG_VERSION[4] == "-"
    assert EBS_CATALOG_VERSION[7] == "-"


# ---------------------------------------------------------------------------
# Real EBS pricing (US East, reviewed 2026-07-18)
# ---------------------------------------------------------------------------


def test_gp2_pricing_is_correct():
    """Regression guard: gp2 at $0.10/GB-month is the whole reason the
    rule exists. If this is wrong, every insight is wrong."""
    p = ebs_price_per_gb_month("gp2")
    assert p is not None
    assert p.usd_per_gb_month == 0.10
    assert p.volume_type == "gp2"
    assert p.review_date == "2026-07-18"


def test_gp3_pricing_is_correct():
    """gp3 at $0.08/GB-month is the migration target. 20% savings on
    storage. If this is wrong, the saving figure is wrong."""
    p = ebs_price_per_gb_month("gp3")
    assert p is not None
    assert p.usd_per_gb_month == 0.08


def test_gp3_is_cheaper_than_gp2():
    """The whole point of the rule: gp3 is cheaper than gp2 on storage.
    Without this invariant, the insight is meaningless."""
    gp2 = ebs_price_per_gb_month("gp2")
    gp3 = ebs_price_per_gb_month("gp3")
    assert gp2 is not None and gp3 is not None
    assert gp3.usd_per_gb_month < gp2.usd_per_gb_month


def test_all_volume_types_have_a_review_date():
    """Every catalogued price row must carry a review date. A price
    without a review date is a price nobody can defend at a sales call."""
    for vt, p in EBS_PRICING.items():
        assert p.review_date != "", f"{vt} missing review_date"
        # ISO date format
        assert len(p.review_date) == 10


def test_all_volume_types_have_a_source_url():
    """Every catalogued price row must link to the AWS pricing page.
    Auditors and customers will click through; broken links kill trust."""
    for vt, p in EBS_PRICING.items():
        assert p.source_url.startswith("https://aws.amazon.com"), (
            f"{vt} source_url doesn't look like an AWS page: {p.source_url}"
        )


def test_unknown_volume_type_returns_none():
    """An unknown volume type (e.g. a future io3) returns None, not 0.0.
    The rule treats None as INCONCLUSIVE, not as 'free'."""
    assert ebs_price_per_gb_month("io99") is None
    assert ebs_price_per_gb_month("") is None
    assert ebs_price_per_gb_month("GP2") is None  # case-sensitive: AWS uses lowercase


def test_known_volume_types_covered():
    """The catalog must cover at least the 7 standard volume types AWS
    publishes. A missing entry means the rule silently emits INCONCLUSIVE
    for a common volume type — a V1 deployment failure."""
    for vt in ("gp2", "gp3", "io1", "io2", "st1", "sc1", "standard"):
        assert ebs_price_per_gb_month(vt) is not None, f"{vt} missing from catalog"


# ---------------------------------------------------------------------------
# monthly_storage_cost: $/month helper
# ---------------------------------------------------------------------------


def test_monthly_storage_cost_gp2_100gb():
    """100 GB gp2 = 100 * $0.10 = $10.00/month. This is the arithmetic
    the rule uses to compute the saving."""
    cost = monthly_storage_cost("gp2", 100)
    assert cost == 10.00


def test_monthly_storage_cost_gp3_100gb():
    """100 GB gp3 = 100 * $0.08 = $8.00/month. The delta is $2.00, the
    rule's `savings_monthly_usd` field."""
    cost = monthly_storage_cost("gp3", 100)
    assert cost == 8.00


def test_monthly_storage_cost_gp2_to_gp3_savings():
    """Cross-check: monthly cost of gp2 minus monthly cost of gp3, on
    the same size, is the rule's `savings_monthly_usd`."""
    size = 500
    gp2_cost = monthly_storage_cost("gp2", size)
    gp3_cost = monthly_storage_cost("gp3", size)
    assert gp2_cost is not None and gp3_cost is not None
    savings = round(gp2_cost - gp3_cost, 2)
    # 500 * (0.10 - 0.08) = $10.00
    assert savings == 10.00


def test_monthly_storage_cost_unknown_type_returns_none():
    assert monthly_storage_cost("io99", 100) is None


def test_monthly_storage_cost_zero_size_is_zero_not_none():
    """A 0 GB volume costs $0.00, which is a valid computed value
    (not None). The rule's noise filter handles the $0 case."""
    assert monthly_storage_cost("gp2", 0) == 0.00


# ---------------------------------------------------------------------------
# Snapshot pricing
# ---------------------------------------------------------------------------


def test_snapshot_standard_pricing():
    p = ebs_snapshot_price_per_gb_month("standard")
    assert p is not None
    assert p.usd_per_gb_month == 0.05


def test_snapshot_archive_pricing():
    """Archive tier is 4x cheaper than standard but has retrieval fees.
    For the V1 ebs_gp2_to_gp3 rule we don't use this; included for
    future snapshot.orphan / ebs.snapshot_old rules."""
    p = ebs_snapshot_price_per_gb_month("archive")
    assert p is not None
    assert p.usd_per_gb_month == 0.0125


def test_unknown_snapshot_tier_returns_none():
    assert ebs_snapshot_price_per_gb_month("deep_archive") is None
