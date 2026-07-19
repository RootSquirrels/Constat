"""EBS snapshot orphan insight.

A snapshot is "orphan" when its source volume no longer exists: the
snapshot keeps paying storage ($/GB-month) for a volume nobody can
restore from. AWS does NOT delete snapshots when the source volume is
deleted, so accounts accumulate them silently.

The cross-resource proof comes from the collector's correlation
post-pass: `aws.ec2.snapshot.volume_exists` is True/False only when
the region's volume scan succeeded (absence of the fact = scope not
proven = INCONCLUSIVE, never a guessed MATCH).

MATCH: state="completed" + volume_exists=False + description does NOT
  reference an AMI -> 1 insight with monthly cost.
NO_MATCH: volume exists, any other state (pending, error, ...), or the
  description references an AMI. AMI-owned snapshots are conservative
  NO_MATCH: AWS writes "ami-..." into the description of snapshots
  backing an AMI, and deleting those breaks the AMI — we cannot prove
  orphanhood without DescribeImages. Corroborating AMI-owned snapshots
  against DescribeImages (is the AMI itself still registered?) is the
  V2 improvement.
INCONCLUSIVE: missing state / size / volume_exists / description /
  region fact, malformed values, or a storage tier not in the catalog
  (defensive against future AWS tiers). A missing description is
  INCONCLUSIVE, not "no AMI reference": without it we cannot rule out
  AMI ownership. The region fact is mandatory: snapshot pricing is not
  region-uniform, so we can't price honestly without it. A region the
  catalog doesn't cover still matches on the us-east-1 fallback grid,
  with `price_region_exact: false` in the payload.

Severity matches the ebs_unattached thresholds ($500/CRITICAL,
$50/WARNING) for dashboard consistency. value_basis=ESTIMATED until
FOCUS reconciles. catalog_version stamped on every insight payload.
Amounts are USD (`source_currency`); the EUR conversion happens at
export/display.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID

from constat_core.catalog.ebs import (
    EBS_CATALOG_VERSION,
    ebs_snapshot_price_per_gb_month,
    price_region_exact,
)
from constat_core.models import Fact, Insight, Severity, ValueState

RULE_NAME = "snapshot_orphan"
SOURCE_NAME = "aws_ec2"

# Only a "completed" snapshot is a stable, billable asset. "pending"
# is transient (creation in flight), "error"/"recovering" are for the
# operator to investigate — not waste candidates.
COMPLETED_STATE = "completed"

# AWS writes the AMI id into the description of snapshots it creates
# for an AMI ("Created by CreateImage(i-...) for ami-..."). Presence of
# the token means "AMI-owned -> cannot prove orphan -> NO_MATCH".
AMI_REFERENCE_TOKEN = "ami-"


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


def _age_days(start_time_raw: object, today: date) -> int | None:
    """Days between the snapshot's StartTime (ISO string) and today.

    None when the value is missing; raises ValueError on a malformed
    string (the caller turns that into INCONCLUSIVE, never a silent
    drop).
    """
    if not start_time_raw:
        return None
    if not isinstance(start_time_raw, str):
        raise ValueError(f"start_time is not an ISO string: {start_time_raw!r}")
    return (today - datetime.fromisoformat(start_time_raw).date()).days


def evaluate(
    resource_id: UUID,
    facts: Iterable[Fact],
    *,
    today: date | None = None,
) -> InsightResult:
    """Evaluate one EBS snapshot for orphan waste.

    Returns an InsightResult with:
    - insights: a single Insight if the snapshot is a proven orphan
      with a real cost, else [].
    - inconclusive_reasons: missing facts that block assessment.
    """
    if today is None:
        today = date.today()
    idx = _index_facts(facts)

    state_fact = _get(idx, "aws.ec2.snapshot.state")
    size_fact = _get(idx, "aws.ec2.snapshot.size_gb")
    tier_fact = _get(idx, "aws.ec2.snapshot.storage_tier")
    exists_fact = _get(idx, "aws.ec2.snapshot.volume_exists")
    description_fact = _get(idx, "aws.ec2.snapshot.description")
    start_time_fact = _get(idx, "aws.ec2.snapshot.start_time")
    region_fact = _get(idx, "aws.ec2.snapshot.region")

    inconclusive: list[str] = []

    # Gate 1: state must be KNOWN.
    if state_fact is None or state_fact.value_state != ValueState.KNOWN:
        inconclusive.append("aws.ec2.snapshot.state")
    # Gate 2: size must be KNOWN (we can't price without it).
    if size_fact is None or size_fact.value_state != ValueState.KNOWN:
        inconclusive.append("aws.ec2.snapshot.size_gb")
    # Gate 3: volume_exists must be present — its existence IS the
    # scope proof (written only when the region's volume scan ran).
    if exists_fact is None:
        inconclusive.append("aws.ec2.snapshot.volume_exists")
    elif not isinstance(exists_fact.value, bool):
        inconclusive.append("aws.ec2.snapshot.volume_exists.malformed")
    # Gate 4: description must be KNOWN — without it we cannot rule
    # out AMI ownership, and matching an AMI-owned snapshot would be
    # a destructive recommendation.
    if description_fact is None or description_fact.value_state != ValueState.KNOWN:
        inconclusive.append("aws.ec2.snapshot.description")
    # Gate 5: region must be KNOWN — snapshot pricing is not
    # region-uniform, so we can't price honestly without it.
    if region_fact is None or region_fact.value_state != ValueState.KNOWN:
        inconclusive.append("aws.ec2.snapshot.region")

    if inconclusive:
        # Missing facts — never silent, always INCONCLUSIVE.
        return InsightResult(insights=[], inconclusive_reasons=inconclusive)

    # NO_MATCH for anything that isn't "completed" (pending is
    # transient; error is for the operator, surfaced in the inventory
    # view, not here).
    state = state_fact.value  # type: ignore[union-attr]
    if state != COMPLETED_STATE:
        return InsightResult()

    # The source volume still exists -> the snapshot has a consumer.
    if exists_fact.value:  # type: ignore[union-attr]
        return InsightResult()

    # AMI-owned snapshot: conservative NO_MATCH (see module docstring).
    description = description_fact.value  # type: ignore[union-attr]
    if isinstance(description, str) and AMI_REFERENCE_TOKEN in description.lower():
        return InsightResult()

    try:
        size_gb = int(size_fact.value)  # type: ignore[arg-type, union-attr]
    except (TypeError, ValueError):
        return InsightResult(
            insights=[], inconclusive_reasons=["aws.ec2.snapshot.size_gb.malformed"]
        )

    # Tier: from facts when collected, else AWS's default ("standard").
    # A tier the catalog doesn't know is INCONCLUSIVE, not $0.
    tier = "standard"
    if tier_fact is not None and tier_fact.value_state == ValueState.KNOWN and tier_fact.value:
        tier = str(tier_fact.value)
    region = str(region_fact.value)  # type: ignore[union-attr]
    price = ebs_snapshot_price_per_gb_month(tier, region)
    if price is None:
        return InsightResult(
            insights=[],
            inconclusive_reasons=["catalog.snapshot_tier_price_missing"],
        )
    monthly_cost = round(price.usd_per_gb_month * size_gb, 2)

    # Age is payload evidence, not a match gate: the cost doesn't
    # depend on it. Missing start_time -> age_days=None; malformed
    # start_time -> INCONCLUSIVE (a value we can't parse is a data
    # problem worth surfacing).
    try:
        age_days = _age_days(start_time_fact.value if start_time_fact is not None else None, today)
    except ValueError:
        return InsightResult(
            insights=[],
            inconclusive_reasons=["aws.ec2.snapshot.start_time.malformed"],
        )

    return InsightResult(
        insights=[
            _make_insight(
                resource_id=resource_id,
                account_id=state_fact.account_id,  # type: ignore[union-attr]
                size_gb=size_gb,
                tier=tier,
                age_days=age_days,
                monthly_cost_usd=monthly_cost,
                pricing_region=price.region,
                price_region_exact=price_region_exact(region, price),
            )
        ]
    )


def _make_insight(
    *,
    resource_id: UUID,
    account_id: str | None,
    size_gb: int,
    tier: str,
    age_days: int | None,
    monthly_cost_usd: float,
    pricing_region: str,
    price_region_exact: bool,
) -> Insight:
    # Same severity thresholds as ebs_unattached for dashboard
    # consistency: the operator reads INFO/WARNING/CRITICAL the same
    # way across all cost-savings insights.
    if monthly_cost_usd >= 500:
        severity = Severity.CRITICAL
    elif monthly_cost_usd >= 50:
        severity = Severity.WARNING
    else:
        severity = Severity.INFO

    age_label = f"{age_days} days old" if age_days is not None else "unknown age"
    title = (
        f"EBS snapshot ({size_gb} GB, {age_label}) is orphaned — its source "
        f"volume no longer exists — costing ${monthly_cost_usd:.2f}/month"
    )
    recommendation = (
        "Delete the snapshot — its source volume is gone, so it cannot "
        "serve a restore of that volume. Verify it is not referenced by "
        "any backup policy or AMI first: aws ec2 describe-images "
        "--filters Name=block-device-mapping.snapshot-id,Values=<id>, "
        "then aws ec2 delete-snapshot --snapshot-id <id>."
    )

    return Insight(
        rule_name=RULE_NAME,
        resource_id=resource_id,
        account_id=account_id,
        severity=severity,
        title=title,
        payload={
            "snapshot_size_gb": size_gb,
            "storage_tier": tier,
            "snapshot_age_days": age_days,
            "state": COMPLETED_STATE,
            "orphan_snapshot_monthly_usd": monthly_cost_usd,
            "value_basis": "ESTIMATED",
            "pricing_region": pricing_region,
            "price_region_exact": price_region_exact,
            "source_currency": "USD",
            "recommendation": recommendation,
            "catalog_version": EBS_CATALOG_VERSION,
        },
    )
