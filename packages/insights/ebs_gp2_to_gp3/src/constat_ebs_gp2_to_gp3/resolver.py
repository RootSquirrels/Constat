"""EBS gp2 → gp3 insight.

The cheapest, most defensible FinOps win: AWS EBS gp2 costs $0.10/GB-month,
gp3 costs $0.08/GB-month for the same storage. A 20% storage saving, with
no behavior change and no migration window — gp3 is the default for new
volumes since 2021 and is API-compatible (resize / change-type in place,
online, no downtime).

MATCH: a volume with type=gp2 and a real saving > $0.50/month.
NO_MATCH: any other volume type.
INCONCLUSIVE: a fact is missing (no type, no size) or the catalog
  can't price the volume.

Payload carries the monthly saving stamped `value_basis=ESTIMATED`
(catalog-derived until a FOCUS line confirms the actual charge).
Price basis is US East; non-us-east-1 prospects see estimates that are
1-3% off the real number.

The dedupe rule "one volume = one insight" is enforced by the runner's
delete-and-replace. Re-running the rule does not accumulate duplicates.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from constat_core.catalog.ebs import (
    EBS_CATALOG_VERSION,
    ebs_price_per_gb_month,
    monthly_storage_cost,
)
from constat_core.models import Fact, Insight, Severity, ValueState

RULE_NAME = "ebs_gp2_to_gp3"
SOURCE_NAME = "aws_ec2"

# Minimum monthly savings to emit (filters out tiny volumes that round
# to $0.00 and would create noise on the dashboard). $0.50/month is
# roughly a 64GB gp2 → gp3 move — below that, the operational cost
# of the migration (tickets, change windows) probably exceeds the
# savings. The operator can still see all volumes in the inventory;
# the insight is for "what to act on this quarter".
MIN_SAVINGS_USD_PER_MONTH = 0.50


@dataclass(frozen=True)
class InsightResult:
    """Outcome of evaluating one resource.

    - insights: gaps that should be surfaced (will be inserted into insights table)
    - inconclusive_reasons: facts that, if present, would let us conclude.
      A non-empty list means "we don't know yet" and produces an Inconclusive
      record — never a silent skip.
    """

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
    """Evaluate one EBS volume.

    Returns an InsightResult with:
    - insights: a single Insight if the volume is gp2 with a real saving,
      else [].
    - inconclusive_reasons: missing facts that block assessment.

    NO_MATCH semantics: gp3/io1/io2/st1/sc1/standard/unknown — these are
    not migration candidates, so we emit nothing. The runner turns an
    empty insights list into a NO_MATCH for the caller.
    """
    idx = _index_facts(facts)

    volume_type_fact = _get(idx, "aws.ec2.volume.volume_type")
    size_fact = _get(idx, "aws.ec2.volume.size_gb")

    inconclusive: list[str] = []

    # Gate 1: volume_type must be KNOWN.
    if volume_type_fact is None or volume_type_fact.value_state != ValueState.KNOWN:
        inconclusive.append("aws.ec2.volume.volume_type")
    # Gate 2: size must be KNOWN.
    if size_fact is None or size_fact.value_state != ValueState.KNOWN:
        inconclusive.append("aws.ec2.volume.size_gb")

    if inconclusive:
        # Missing facts — never silent, always INCONCLUSIVE so the user
        # sees the gap in their data.
        return InsightResult(insights=[], inconclusive_reasons=inconclusive)

    volume_type = volume_type_fact.value  # type: ignore[union-attr]
    size_gb = int(size_fact.value)  # type: ignore[arg-type]

    # NO_MATCH for everything that isn't gp2. We only emit insights
    # for migration candidates.
    if volume_type != "gp2":
        return InsightResult()

    # gp2 → gp3 comparison. Both prices must be in the catalog; if not,
    # we don't have a defensible saving number, so INCONCLUSIVE.
    gp2_price = ebs_price_per_gb_month("gp2")
    gp3_price = ebs_price_per_gb_month("gp3")
    if gp2_price is None or gp3_price is None:
        return InsightResult(
            insights=[],
            inconclusive_reasons=["catalog.gp2_or_gp3_price_missing"],
        )

    current_monthly = monthly_storage_cost("gp2", size_gb)
    target_monthly = monthly_storage_cost("gp3", size_gb)
    if current_monthly is None or target_monthly is None:
        return InsightResult(
            insights=[],
            inconclusive_reasons=["catalog.monthly_storage_cost_uncomputable"],
        )
    savings = round(current_monthly - target_monthly, 2)

    if savings < MIN_SAVINGS_USD_PER_MONTH:
        # Below the noise threshold: NO_MATCH, not an insight. Keeps
        # the dashboard clean for fleets with many tiny scratch volumes.
        return InsightResult()

    return InsightResult(
        insights=[
            _make_insight(
                resource_id=resource_id,
                account_id=volume_type_fact.account_id,  # type: ignore[union-attr]
                size_gb=size_gb,
                current_monthly_usd=current_monthly,
                target_monthly_usd=target_monthly,
                savings_monthly_usd=savings,
            )
        ]
    )


def _make_insight(
    *,
    resource_id: UUID,
    account_id: str | None,
    size_gb: int,
    current_monthly_usd: float,
    target_monthly_usd: float,
    savings_monthly_usd: float,
) -> Insight:
    # Severity thresholds: $50/month is "a real number" the operator
    # notices; $500/month is "a fleet-level problem". The dashboard
    # sorts by $ to surface the biggest wins regardless of severity.
    if savings_monthly_usd >= 500:
        severity = Severity.CRITICAL
    elif savings_monthly_usd >= 50:
        severity = Severity.WARNING
    else:
        severity = Severity.INFO

    title = f"EBS gp2 volume ({size_gb} GB) costs ${savings_monthly_usd:.2f}/month more than gp3"
    recommendation = (
        f"Migrate to gp3 (online, no downtime): ${current_monthly_usd:.2f}/month "
        f"→ ${target_monthly_usd:.2f}/month. Same API, no behavior change, "
        f"~20% storage saving."
    )

    return Insight(
        rule_name=RULE_NAME,
        resource_id=resource_id,
        account_id=account_id,
        severity=severity,
        title=title,
        payload={
            "volume_size_gb": size_gb,
            "current_volume_type": "gp2",
            "target_volume_type": "gp3",
            "current_monthly_usd": current_monthly_usd,
            "target_monthly_usd": target_monthly_usd,
            "savings_monthly_usd": savings_monthly_usd,
            "savings_pct": round(100 * savings_monthly_usd / current_monthly_usd, 1)
            if current_monthly_usd > 0
            else 0.0,
            "value_basis": "ESTIMATED",
            "recommendation": recommendation,
            "catalog_version": EBS_CATALOG_VERSION,
        },
    )
