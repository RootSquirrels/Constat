"""EBS gp2 → gp3 insight.

The cheapest, most defensible FinOps win: AWS EBS gp2 costs
$0.10/GB-month, gp3 costs $0.08/GB-month for the same storage.
A 20% storage saving, with no behavior change and no migration
window — gp3 is the default for new volumes since 2021 and is
API-compatible (resize / change-type in place, online, no
downtime).

MATCH: a volume with type=gp2 and a real saving > MIN_SAVINGS.
NO_MATCH: any other volume type, or a gp2 below the noise
  threshold.
INCONCLUSIVE: a fact is missing or the catalog can't price the
  volume. The region fact is mandatory — the gp2/gp3 delta is not
  region-uniform, so a saving priced without knowing the region
  is not defensible. A region the catalog doesn't cover still
  matches on the us-east-1 fallback grid, with
  `price_region_exact: false` in the payload.

Chantier III.2 of the roadmap consolidation: the
`size_gb x $/GB-month` arithmetic, the $500/$50 severity
thresholds, and the payload assembly live in
`constat_core.insights.storage`. This file is the rule-specific
config (which volume types, which savings breakdown) + a thin
wrapper. The existing test suite passes unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import cast
from uuid import UUID

from constat_core.catalog.ebs import (
    EBS_CATALOG_VERSION,
    ebs_price_per_gb_month,
    price_region_exact,
)
from constat_core.insights.storage import (
    StorageCost,
    StorageInconclusiveError,
    StorageInsightResult,
    StorageRuleConfig,
    evaluate_storage,
)
from constat_core.models import Fact

RULE_NAME = "ebs_gp2_to_gp3"
SOURCE_NAME = "aws_ec2"

# Minimum monthly savings to emit (filters out tiny volumes that
# round to $0.00 and would create noise on the dashboard).
# $0.50/month is roughly a 64GB gp2 → gp3 move — below that, the
# operational cost of the migration (tickets, change windows)
# probably exceeds the savings. The operator can still see all
# volumes in the inventory; the insight is for "what to act on
# this quarter".
MIN_SAVINGS_USD_PER_MONTH = 0.50


def _should_emit(idx: dict[str, Fact]) -> bool:
    """NO_MATCH for everything that isn't gp2. Only migration
    candidates emit."""
    return cast(str, idx["aws.ec2.volume.volume_type"].value) == "gp2"


def _compute_cost(idx: dict[str, Fact], today: date) -> StorageCost | None:
    """The gp2 → gp3 comparison on the volume's region grid. Both
    prices must be in the catalog; if not, INCONCLUSIVE."""
    region = cast(str, idx["aws.ec2.volume.region"].value)
    try:
        size_gb = int(idx["aws.ec2.volume.size_gb"].value)
    except (TypeError, ValueError) as exc:
        raise StorageInconclusiveError("aws.ec2.volume.size_gb.malformed") from exc
    gp2_price = ebs_price_per_gb_month("gp2", region)
    gp3_price = ebs_price_per_gb_month("gp3", region)
    if gp2_price is None or gp3_price is None:
        raise StorageInconclusiveError("catalog.gp2_or_gp3_price_missing") from None

    current_monthly = round(gp2_price.usd_per_gb_month * size_gb, 2)
    target_monthly = round(gp3_price.usd_per_gb_month * size_gb, 2)
    savings = round(current_monthly - target_monthly, 2)

    if savings < MIN_SAVINGS_USD_PER_MONTH:
        # Below the noise threshold: NO_MATCH, not an insight. Keeps
        # the dashboard clean for fleets with many tiny scratch
        # volumes. The shared function treats compute_cost returning
        # None as a NO_MATCH.
        return None

    return StorageCost(
        monthly_usd=savings,
        monetary_payload_key="savings_monthly_usd",
        pricing_region=gp2_price.region,
        price_region_exact=(
            price_region_exact(region, gp2_price)
            and price_region_exact(region, gp3_price)
        ),
        extras={
            "volume_size_gb": size_gb,
            "current_volume_type": "gp2",
            "target_volume_type": "gp3",
            "current_monthly_usd": current_monthly,
            "target_monthly_usd": target_monthly,
            "savings_pct": round(100 * savings / current_monthly, 1)
            if current_monthly > 0
            else 0.0,
        },
    )


def _build_title(idx: dict[str, Fact], cost: StorageCost) -> str:
    """The insight title goes onto the operator's dashboard. It
    must answer 'what is this about' and 'how much will I save'
    at a glance."""
    size_gb = cost.extras["volume_size_gb"]
    return f"EBS gp2 volume ({size_gb} GB) costs ${cost.monthly_usd:.2f}/month more than gp3"


def _build_recommendation(idx: dict[str, Fact], cost: StorageCost) -> str:
    """Migrate to gp3 (online, no downtime)."""
    return (
        f"Migrate to gp3 (online, no downtime): ${cost.extras['current_monthly_usd']:.2f}/month "
        f"→ ${cost.extras['target_monthly_usd']:.2f}/month. Same API, no behavior change, "
        f"~20% storage saving."
    )


CONFIG = StorageRuleConfig(
    rule_name=RULE_NAME,
    required_facts=(
        "aws.ec2.volume.volume_type",
        "aws.ec2.volume.size_gb",
        "aws.ec2.volume.region",
    ),
    should_emit=_should_emit,
    compute_cost=_compute_cost,
    build_title=_build_title,
    build_recommendation=_build_recommendation,
)


# Re-export so the rule's test file (which imports `InsightResult`
# from this module) keeps working without touching the test.
InsightResult = StorageInsightResult


def evaluate(
    resource_id: UUID,
    facts: Iterable[Fact],
    *,
    today: date | None = None,
) -> StorageInsightResult:
    """Evaluate one EBS volume. Returns the same InsightResult
    shape as the per-rule evaluator did before the refactor."""
    return evaluate_storage(
        resource_id,
        facts,
        CONFIG,
        today=today,
        catalog_version=EBS_CATALOG_VERSION,
    )
