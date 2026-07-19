"""EBS unattached insight.

An EBS volume is "unattached" when it has no consumer (state="available",
no Attachments). The volume is paying storage cost for nothing.

MATCH: state="available" + size+type known -> 1 insight with monthly waste.
NO_MATCH: any other state (in-use, creating, deleting, error, deleted).
  Error state is intentionally NO_MATCH, not INCONCLUSIVE: the volume
  still costs money, but we don't know if it's "transiently broken" or
  "permanently dead" — the operator can investigate from the inventory
  view.
INCONCLUSIVE: missing state / size / type / region fact, or a volume
  type not in the catalog (defensive against future AWS types). The
  region fact is mandatory: EBS storage pricing is not region-uniform,
  so a waste figure priced without knowing the region is not
  defensible. A region the catalog doesn't cover still matches, on the
  us-east-1 fallback grid, with `price_region_exact: false` — the
  payload says which grid priced it (`pricing_region`).

Severity matches the gp2_to_gp3 thresholds ($500/CRITICAL, $50/WARNING)
for dashboard consistency. value_basis=ESTIMATED until FOCUS reconciles.
catalog_version stamped on every insight payload. Amounts are USD
(`source_currency`); the EUR conversion happens at export/display.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from constat_core.catalog.ebs import (
    EBS_CATALOG_VERSION,
    ebs_price_per_gb_month,
    price_region_exact,
)
from constat_core.models import Fact, Insight, Severity, ValueState

RULE_NAME = "ebs_unattached"
SOURCE_NAME = "aws_ec2"

# An "available" EBS volume is unattached. Other states (in-use,
# creating, deleting, error) are not candidates — the operator
# shouldn't see noise from transient states.
UNATTACHED_STATE = "available"


@dataclass(frozen=True)
class InsightResult:
    insights: list[Insight] = field(default_factory=list)
    inconclusive_reasons: list[str] = field(default_factory=list)

    @property
    def is_conclusive(self) -> bool:
        return not self.inconclusive_reasons

    @property
    def has_gap(self) -> bool:
        return bool(self.insights)


def _index_facts(facts: Iterable[Fact]) -> dict[str, Fact]:
    return {f"{f.namespace}.{f.key}": f for f in facts}


def _get(idx: dict[str, Fact], dotted_key: str) -> Fact | None:
    return idx.get(dotted_key)


def evaluate(
    resource_id: UUID,
    facts: Iterable[Fact],
    *,
    today: date | None = None,
) -> InsightResult:
    """Evaluate one EBS volume for unattached waste.

    Returns an InsightResult with:
    - insights: a single Insight if the volume is unattached with a real
      cost, else [].
    - inconclusive_reasons: missing facts that block assessment.
    """
    idx = _index_facts(facts)

    state_fact = _get(idx, "aws.ec2.volume.state")
    size_fact = _get(idx, "aws.ec2.volume.size_gb")
    type_fact = _get(idx, "aws.ec2.volume.volume_type")
    region_fact = _get(idx, "aws.ec2.volume.region")

    inconclusive: list[str] = []

    # Gate 1: state must be KNOWN.
    if state_fact is None or state_fact.value_state != ValueState.KNOWN:
        inconclusive.append("aws.ec2.volume.state")
    # Gate 2: size must be KNOWN (we can't price without it).
    if size_fact is None or size_fact.value_state != ValueState.KNOWN:
        inconclusive.append("aws.ec2.volume.size_gb")
    # Gate 3: type must be KNOWN (we can't price without it).
    if type_fact is None or type_fact.value_state != ValueState.KNOWN:
        inconclusive.append("aws.ec2.volume.volume_type")
    # Gate 4: region must be KNOWN — EBS pricing is not region-uniform,
    # so we can't price honestly without knowing the region.
    if region_fact is None or region_fact.value_state != ValueState.KNOWN:
        inconclusive.append("aws.ec2.volume.region")

    if inconclusive:
        # Missing facts — never silent, always INCONCLUSIVE.
        return InsightResult(insights=[], inconclusive_reasons=inconclusive)

    # NO_MATCH for anything that isn't "available". We don't emit
    # insights for in-use (working), creating/deleting (transient),
    # or error (operator should investigate, but the volume still
    # costs money — surfaced in the inventory view, not here).
    state = state_fact.value  # type: ignore[union-attr]
    if state != UNATTACHED_STATE:
        return InsightResult()

    size_gb = int(size_fact.value)  # type: ignore[arg-type]
    volume_type = type_fact.value  # type: ignore[union-attr]
    region = str(region_fact.value)  # type: ignore[union-attr]

    price = ebs_price_per_gb_month(volume_type, region)
    if price is None:
        # Volume type not in the catalog (e.g. a future io3). Don't
        # emit a "free" insight; surface the catalog gap.
        return InsightResult(
            insights=[],
            inconclusive_reasons=["catalog.volume_type_price_missing"],
        )
    monthly_waste = round(price.usd_per_gb_month * size_gb, 2)

    return InsightResult(
        insights=[
            _make_insight(
                resource_id=resource_id,
                account_id=state_fact.account_id,  # type: ignore[union-attr]
                volume_type=volume_type,
                size_gb=size_gb,
                monthly_waste_usd=monthly_waste,
                pricing_region=price.region,
                price_region_exact=price_region_exact(region, price),
            )
        ]
    )


def _make_insight(
    *,
    resource_id: UUID,
    account_id: str | None,
    volume_type: str,
    size_gb: int,
    monthly_waste_usd: float,
    pricing_region: str,
    price_region_exact: bool,
) -> Insight:
    # Same severity thresholds as gp2_to_gp3 for dashboard consistency:
    # operator reads "INFO/WARNING/CRITICAL" the same way across all
    # cost-savings insights. The dashboard sorts by $ to surface the
    # biggest wins regardless of severity.
    if monthly_waste_usd >= 500:
        severity = Severity.CRITICAL
    elif monthly_waste_usd >= 50:
        severity = Severity.WARNING
    else:
        severity = Severity.INFO

    title = (
        f"EBS {volume_type} volume ({size_gb} GB) is unattached, "
        f"wasting ${monthly_waste_usd:.2f}/month"
    )
    recommendation = (
        "Delete the volume (after snapshotting if needed) — it has no "
        "consumer. Snapshot first: aws ec2 create-snapshot "
        "--volume-id <id>, then aws ec2 delete-volume --volume-id <id>."
    )

    return Insight(
        rule_name=RULE_NAME,
        resource_id=resource_id,
        account_id=account_id,
        severity=severity,
        title=title,
        payload={
            "volume_size_gb": size_gb,
            "volume_type": volume_type,
            "state": UNATTACHED_STATE,
            "monthly_waste_usd": monthly_waste_usd,
            "value_basis": "ESTIMATED",
            "pricing_region": pricing_region,
            "price_region_exact": price_region_exact,
            "source_currency": "USD",
            "recommendation": recommendation,
            "catalog_version": EBS_CATALOG_VERSION,
        },
    )
