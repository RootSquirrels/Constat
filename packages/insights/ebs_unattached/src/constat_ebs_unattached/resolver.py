"""EBS unattached insight.

An EBS volume is "unattached" when it has no consumer
(state="available", no Attachments). The volume is paying storage
cost for nothing.

MATCH: state="available" + size+type known -> 1 insight with
  monthly waste.
NO_MATCH: any other state (in-use, creating, deleting, error,
  deleted). Error state is intentionally NO_MATCH, not
  INCONCLUSIVE: the volume still costs money, but we don't know
  if it's "transiently broken" or "permanently dead" — the
  operator can investigate from the inventory view.
INCONCLUSIVE: missing state / size / type / region fact, or a
  volume type not in the catalog (defensive against future AWS
  types). The region fact is mandatory: EBS storage pricing is
  not region-uniform, so a waste figure priced without knowing
  the region is not defensible. A region the catalog doesn't
  cover still matches, on the us-east-1 fallback grid, with
  `price_region_exact: false` — the payload says which grid
  priced it (`pricing_region`).

Severity matches the gp2_to_gp3 thresholds ($500/CRITICAL,
$50/WARNING) for dashboard consistency.

Chantier III.2 of the roadmap consolidation: the
`size_gb x $/GB-month` arithmetic, the $500/$50 severity
thresholds, and the payload assembly live in
`constat_core.insights.storage`. This file is the rule-specific
config (which state counts as unattached, what the
recommendation says) + a thin wrapper. The existing test suite
passes unchanged.
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

RULE_NAME = "ebs_unattached"
SOURCE_NAME = "aws_ec2"

# An "available" EBS volume is unattached. Other states (in-use,
# creating, deleting, error) are not candidates — the operator
# shouldn't see noise from transient states.
UNATTACHED_STATE = "available"


def _should_emit(idx: dict[str, Fact]) -> bool:
    """NO_MATCH for anything that isn't "available". We don't emit
    insights for in-use (working), creating/deleting (transient),
    or error (operator should investigate, but the volume still
    costs money — surfaced in the inventory view, not here)."""
    return cast(str, idx["aws.ec2.volume.state"].value) == UNATTACHED_STATE


def _compute_cost(idx: dict[str, Fact], today: date) -> StorageCost:
    """Look up the price for the volume's type on the volume's
    region grid. The single multiplication `size_gb x
    usd_per_gb_month` lives in this helper; the shared function
    never sees the arithmetic."""
    volume_type = cast(str, idx["aws.ec2.volume.volume_type"].value)
    try:
        size_gb = int(idx["aws.ec2.volume.size_gb"].value)
    except (TypeError, ValueError) as exc:
        raise StorageInconclusiveError("aws.ec2.volume.size_gb.malformed") from exc
    region = cast(str, idx["aws.ec2.volume.region"].value)
    price = ebs_price_per_gb_month(volume_type, region)
    if price is None:
        # Volume type not in the catalog (e.g. a future io3). Don't
        # emit a "free" insight; surface the catalog gap.
        raise StorageInconclusiveError("catalog.volume_type_price_missing") from None
    monthly_waste = round(price.usd_per_gb_month * size_gb, 2)
    return StorageCost(
        monthly_usd=monthly_waste,
        monetary_payload_key="monthly_waste_usd",
        pricing_region=price.region,
        price_region_exact=price_region_exact(region, price),
        extras={
            "volume_size_gb": size_gb,
            "volume_type": volume_type,
            "state": UNATTACHED_STATE,
        },
    )


def _build_title(idx: dict[str, Fact], cost: StorageCost) -> str:
    return (
        f"EBS {cost.extras['volume_type']} volume ({cost.extras['volume_size_gb']} GB) is "
        f"unattached, wasting ${cost.monthly_usd:.2f}/month"
    )


def _build_recommendation(idx: dict[str, Fact], cost: StorageCost) -> str:
    return (
        "Delete the volume (after snapshotting if needed) — it has no "
        "consumer. Snapshot first: aws ec2 create-snapshot "
        "--volume-id <id>, then aws ec2 delete-volume --volume-id <id>."
    )


CONFIG = StorageRuleConfig(
    rule_name=RULE_NAME,
    required_facts=(
        "aws.ec2.volume.state",
        "aws.ec2.volume.size_gb",
        "aws.ec2.volume.volume_type",
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
    """Evaluate one EBS volume for unattached waste. Returns the
    same InsightResult shape as the per-rule evaluator did before
    the refactor."""
    return evaluate_storage(
        resource_id,
        facts,
        CONFIG,
        today=today,
        catalog_version=EBS_CATALOG_VERSION,
    )
