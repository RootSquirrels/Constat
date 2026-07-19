"""EBS pricing catalog, per region.

Last reviewed: 2026-07-19. Update when AWS publishes changes.

Sources:
- EBS pricing: https://aws.amazon.com/ebs/pricing/ (region selector)
- EBS volume types: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ebs-volume-types.html
- Snapshot pricing: https://aws.amazon.com/ebs/pricing/#Snapshots
- Cross-checked against the AWS Price List API bulk files
  (AmazonEC2 offer, publication date 2026-07-17) — the same data the
  pricing page's region selector serves.

The catalog covers the regions the V1 pilot actually scans
(us-east-1, eu-west-1, eu-west-3). EBS storage pricing is NOT
region-uniform (eu-west-1 gp3 is 10% above us-east-1), so a money
figure priced on the wrong grid is not defensible in front of a CFO.
Lookups for an uncatalogued region fall back to the us-east-1 grid and
the returned price row carries the region it was actually priced in —
callers must surface `price_region_exact=False` instead of silently
presenting a us-east-1 number as local.

EBS pricing has two components per volume type:
- Storage cost ($/GB-month) — what you pay for the provisioned size.
- Provisioned IOPS/throughput cost — only for io1/io2/gp3 (gp3 includes
  a baseline 3000 IOPS / 125 MB/s; extra IOPS/throughput is metered).

This catalog exposes only the storage rate. The IOPS/throughput
charges are workload-dependent (we don't observe the workload) and
belong in a future catalog extension.

Storage pricing table ($/GB-month, reviewed 2026-07-19):

    type      us-east-1   eu-west-1   eu-west-3
    gp2       0.10        0.11        0.116
    gp3       0.08        0.088       0.0928
    io1       0.125       0.138       0.145     (+ provisioned IOPS)
    io2       0.125       0.138       0.145     (+ provisioned IOPS)
    st1       0.045       0.05        0.053
    sc1       0.015       0.0168      0.0174
    standard  0.05        0.055       0.058

EBS Snapshots ($/GB-month of data stored, reviewed 2026-07-19):

    tier      us-east-1   eu-west-1   eu-west-3
    standard  0.05        0.05        0.053
    archive   0.0125      0.0125      0.01325   (retrieval fee separate)
"""

from __future__ import annotations

from dataclasses import dataclass

# Version stamp shared with catalog/aws.py — both are reviewed on the
# same day. Bump the global CATALOG_VERSION when this goes out of
# sync with the RDS catalog (different review cadences).
EBS_CATALOG_VERSION = "2026-07-19"

# The fallback grid when a region isn't catalogued. us-east-1 is the
# region AWS uses in its own pricing examples, so it stays the default
# for region-less callers (backward compatibility).
DEFAULT_REGION = "us-east-1"

_EBS_PRICING_URL = "https://aws.amazon.com/ebs/pricing/"
_EBS_SNAPSHOT_PRICING_URL = "https://aws.amazon.com/ebs/pricing/#Snapshots"
_REVIEW_DATE = "2026-07-19"


@dataclass(frozen=True)
class EbsPrice:
    """Per-GB-month storage cost for one EBS volume type in one region.

    `region` is the region the price actually applies to — after a
    fallback lookup this differs from the requested region, which is
    exactly what `price_region_exact` surfaces. `source_url` is the AWS
    pricing page used at review time; `review_date` is when this row
    was last cross-checked against the source.
    """

    volume_type: str
    usd_per_gb_month: float
    source_url: str
    review_date: str  # ISO date string
    region: str = DEFAULT_REGION


# Raw grids ($/GB-month). Kept as plain dicts so a price update is a
# one-line diff; the EbsPrice rows below are derived from them.
_STORAGE_GRID_USD: dict[str, dict[str, float]] = {
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

EBS_PRICING_BY_REGION: dict[str, dict[str, EbsPrice]] = {
    region: {
        volume_type: EbsPrice(
            volume_type=volume_type,
            usd_per_gb_month=rate,
            source_url=_EBS_PRICING_URL,
            review_date=_REVIEW_DATE,
            region=region,
        )
        for volume_type, rate in grid.items()
    }
    for region, grid in _STORAGE_GRID_USD.items()
}

# Backward-compatible alias: the us-east-1 grid.
EBS_PRICING: dict[str, EbsPrice] = EBS_PRICING_BY_REGION[DEFAULT_REGION]


@dataclass(frozen=True)
class EbsSnapshotPrice:
    """Per-GB-month storage cost for one EBS snapshot tier in one region.

    Same `region` semantics as EbsPrice (fallback lookups carry the
    region the price was actually taken from).
    """

    tier: str  # "standard" or "archive"
    usd_per_gb_month: float
    source_url: str
    review_date: str
    region: str = DEFAULT_REGION


_SNAPSHOT_GRID_USD: dict[str, dict[str, float]] = {
    "us-east-1": {"standard": 0.05, "archive": 0.0125},
    "eu-west-1": {"standard": 0.05, "archive": 0.0125},
    "eu-west-3": {"standard": 0.053, "archive": 0.01325},
}

EBS_SNAPSHOT_PRICING_BY_REGION: dict[str, dict[str, EbsSnapshotPrice]] = {
    region: {
        tier: EbsSnapshotPrice(
            tier=tier,
            usd_per_gb_month=rate,
            source_url=_EBS_SNAPSHOT_PRICING_URL,
            review_date=_REVIEW_DATE,
            region=region,
        )
        for tier, rate in grid.items()
    }
    for region, grid in _SNAPSHOT_GRID_USD.items()
}

# Backward-compatible alias: the us-east-1 grid.
EBS_SNAPSHOT_PRICING: dict[str, EbsSnapshotPrice] = EBS_SNAPSHOT_PRICING_BY_REGION[DEFAULT_REGION]


def ebs_price_per_gb_month(volume_type: str, region: str | None = None) -> EbsPrice | None:
    """Return the price entry for an EBS volume type, or None if unknown.

    `region=None` prices on the us-east-1 grid (backward compatibility).
    A catalogued region prices on its own grid; an uncatalogued region
    (or a type missing from that region's grid) falls back to us-east-1
    and the returned row's `region` says so — use `price_region_exact`
    to tell an exact match from a fallback.

    "unknown" here means AWS published a new type we haven't catalogued
    yet (e.g. a future io3). Callers should treat None as INCONCLUSIVE,
    not as a free $0/GB-month.
    """
    if region is not None:
        price = EBS_PRICING_BY_REGION.get(region, {}).get(volume_type)
        if price is not None:
            return price
    return EBS_PRICING_BY_REGION[DEFAULT_REGION].get(volume_type)


def ebs_snapshot_price_per_gb_month(
    tier: str, region: str | None = None
) -> EbsSnapshotPrice | None:
    """Return the snapshot price entry for a tier (standard / archive), or None.

    Same region semantics as `ebs_price_per_gb_month`.
    """
    if region is not None:
        price = EBS_SNAPSHOT_PRICING_BY_REGION.get(region, {}).get(tier)
        if price is not None:
            return price
    return EBS_SNAPSHOT_PRICING_BY_REGION[DEFAULT_REGION].get(tier)


def price_region_exact(requested_region: str | None, price: EbsPrice | EbsSnapshotPrice) -> bool:
    """True when `price` was taken from the grid of `requested_region`.

    `requested_region=None` means the caller didn't ask for a region:
    the us-east-1 default is what they wanted, so it is exact by
    definition. A False result means the catalog fell back to the
    us-east-1 grid — the amount is an estimate on a foreign grid and
    the payload must say so (`price_region_exact: false`).
    """
    if requested_region is None:
        return True
    return price.region == requested_region


def monthly_storage_cost(volume_type: str, size_gb: int, region: str | None = None) -> float | None:
    """$/month for a volume of `size_gb` of `volume_type`. Storage line
    only — does not include provisioned IOPS/throughput charges, which
    are workload-dependent and not catalogued here.

    Returns None if the volume type isn't catalogued. Region semantics
    as in `ebs_price_per_gb_month` (None -> us-east-1, exact).
    """
    price = ebs_price_per_gb_month(volume_type, region)
    if price is None or size_gb is None:
        return None
    return round(price.usd_per_gb_month * size_gb, 2)
