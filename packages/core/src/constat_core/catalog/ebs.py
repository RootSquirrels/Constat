"""EBS pricing catalog.

Last reviewed: 2026-07-18. Update when AWS publishes changes.

Sources:
- EBS pricing: https://aws.amazon.com/ebs/pricing/
- EBS volume types: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ebs-volume-types.html
- Snapshot pricing: https://aws.amazon.com/ebs/pricing/#Snapshots

The catalog is US East (N. Virginia). Other regions have small
premiums (1-3%), so insights flag `value_basis=ESTIMATED` to surface
the approximation. Multi-region pricing is not yet catalogued.

EBS pricing has two components per volume type:
- Storage cost ($/GB-month) — what you pay for the provisioned size.
- Provisioned IOPS/throughput cost — only for io1/io2/gp3 (gp3 includes
  a baseline 3000 IOPS / 125 MB/s; extra IOPS/throughput is metered).

This catalog exposes only the storage rate. The IOPS/throughput
charges are workload-dependent (we don't observe the workload) and
belong in a future catalog extension.

Pricing table (US East, 2026-07-18):

    gp2   $0.10/GB-month
    gp3   $0.08/GB-month  (saving of 20% on storage)
    io1   $0.125/GB-month + $0.065/provisioned IOPS-month
    io2   $0.125/GB-month + $0.065/provisioned IOPS-month
    st1   $0.045/GB-month  (throughput-optimized HDD)
    sc1   $0.015/GB-month  (cold HDD)
    standard (magnetic) $0.05/GB-month

EBS Snapshots:
    Standard: $0.05/GB-month (data stored)
    Archive: $0.0125/GB-month (data stored, retrieval fee separate)
"""

from __future__ import annotations

from dataclasses import dataclass

# Version stamp shared with catalog/aws.py — both are reviewed on the
# same day. Bump the global CATALOG_VERSION when this goes out of
# sync with the RDS catalog (different review cadences).
EBS_CATALOG_VERSION = "2026-07-18"


@dataclass(frozen=True)
class EbsPrice:
    """Per-GB-month storage cost for one EBS volume type in one region.

    `source_url` is the AWS pricing page used at review time. The
    `review_date` is when this row was last cross-checked against the
    source.
    """

    volume_type: str
    usd_per_gb_month: float
    source_url: str
    review_date: str  # ISO date string


# US East (N. Virginia) pricing.
EBS_PRICING: dict[str, EbsPrice] = {
    "gp2": EbsPrice(
        volume_type="gp2",
        usd_per_gb_month=0.10,
        source_url="https://aws.amazon.com/ebs/pricing/",
        review_date="2026-07-18",
    ),
    "gp3": EbsPrice(
        volume_type="gp3",
        usd_per_gb_month=0.08,
        source_url="https://aws.amazon.com/ebs/pricing/",
        review_date="2026-07-18",
    ),
    "io1": EbsPrice(
        volume_type="io1",
        usd_per_gb_month=0.125,
        source_url="https://aws.amazon.com/ebs/pricing/",
        review_date="2026-07-18",
    ),
    "io2": EbsPrice(
        volume_type="io2",
        usd_per_gb_month=0.125,
        source_url="https://aws.amazon.com/ebs/pricing/",
        review_date="2026-07-18",
    ),
    "st1": EbsPrice(
        volume_type="st1",
        usd_per_gb_month=0.045,
        source_url="https://aws.amazon.com/ebs/pricing/",
        review_date="2026-07-18",
    ),
    "sc1": EbsPrice(
        volume_type="sc1",
        usd_per_gb_month=0.015,
        source_url="https://aws.amazon.com/ebs/pricing/",
        review_date="2026-07-18",
    ),
    "standard": EbsPrice(
        volume_type="standard",
        usd_per_gb_month=0.05,
        source_url="https://aws.amazon.com/ebs/pricing/",
        review_date="2026-07-18",
    ),
}


@dataclass(frozen=True)
class EbsSnapshotPrice:
    """Per-GB-month storage cost for one EBS snapshot tier in one region."""

    tier: str  # "standard" or "archive"
    usd_per_gb_month: float
    source_url: str
    review_date: str


EBS_SNAPSHOT_PRICING: dict[str, EbsSnapshotPrice] = {
    "standard": EbsSnapshotPrice(
        tier="standard",
        usd_per_gb_month=0.05,
        source_url="https://aws.amazon.com/ebs/pricing/#Snapshots",
        review_date="2026-07-18",
    ),
    "archive": EbsSnapshotPrice(
        tier="archive",
        usd_per_gb_month=0.0125,
        source_url="https://aws.amazon.com/ebs/pricing/#Snapshots",
        review_date="2026-07-18",
    ),
}


def ebs_price_per_gb_month(volume_type: str) -> EbsPrice | None:
    """Return the price entry for an EBS volume type, or None if unknown.

    "unknown" here means AWS published a new type we haven't catalogued
    yet (e.g. a future io3). Callers should treat None as INCONCLUSIVE,
    not as a free $0/GB-month.
    """
    return EBS_PRICING.get(volume_type)


def ebs_snapshot_price_per_gb_month(tier: str) -> EbsSnapshotPrice | None:
    """Return the snapshot price entry for a tier (standard / archive), or None."""
    return EBS_SNAPSHOT_PRICING.get(tier)


def monthly_storage_cost(volume_type: str, size_gb: int) -> float | None:
    """$/month for a volume of `size_gb` of `volume_type`. Storage line
    only — does not include provisioned IOPS/throughput charges, which
    are workload-dependent and not catalogued here.

    Returns None if the volume type isn't catalogued.
    """
    price = ebs_price_per_gb_month(volume_type)
    if price is None or size_gb is None:
        return None
    return round(price.usd_per_gb_month * size_gb, 2)
