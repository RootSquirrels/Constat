"""EBS snapshot orphan insight.

A snapshot is "orphan" when its source volume no longer exists:
the snapshot keeps paying storage ($/GB-month) for a volume
nobody can restore from. AWS does NOT delete snapshots when the
source volume is deleted, so accounts accumulate them silently.

The cross-resource proof comes from the collector's correlation
post-pass: `aws.ec2.snapshot.volume_exists` is True/False only
when the region's volume scan succeeded (absence of the fact =
scope not proven = INCONCLUSIVE, never a guessed MATCH).

MATCH: state="completed" + volume_exists=False + description
  does NOT reference an AMI -> 1 insight with monthly cost.
NO_MATCH: volume exists, any other state (pending, error, ...),
  or the description references an AMI. AMI-owned snapshots are
  conservative NO_MATCH: AWS writes "ami-..." into the
  description of snapshots backing an AMI, and deleting those
  breaks the AMI — we cannot prove orphanhood without
  DescribeImages. Corroborating AMI-owned snapshots against
  DescribeImages (is the AMI itself still registered?) is the V2
  improvement.
INCONCLUSIVE: missing state / size / volume_exists / description
  / region fact, malformed values, or a storage tier not in the
  catalog (defensive against future AWS tiers). A missing
  description is INCONCLUSIVE, not "no AMI reference": without
  it we cannot rule out AMI ownership. The region fact is
  mandatory: snapshot pricing is not region-uniform, so we
  can't price honestly without it. A region the catalog doesn't
  cover still matches on the us-east-1 fallback grid, with
  `price_region_exact: false` in the payload.

Severity matches the ebs_unattached thresholds ($500/CRITICAL,
$50/WARNING) for dashboard consistency.

Chantier III.2 of the roadmap consolidation: the
`size_gb x $/GB-month` arithmetic, the $500/$50 severity
thresholds, and the payload assembly live in
`constat_core.insights.storage`. This file is the rule-specific
config (which correlation facts, what "orphan" means, age
plumbing) + a thin wrapper. The existing test suite passes
unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import cast
from uuid import UUID

from constat_core.catalog.ebs import (
    EBS_CATALOG_VERSION,
    ebs_snapshot_price_per_gb_month,
    price_region_exact,
)
from constat_core.insights.storage import (
    StorageCost,
    StorageInconclusiveError,
    StorageInsightResult,
    StorageRuleConfig,
    evaluate_storage,
)
from constat_core.models import Fact, ValueState

RULE_NAME = "snapshot_orphan"
SOURCE_NAME = "aws_ec2"

# Only a "completed" snapshot is a stable, billable asset. "pending"
# is transient (creation in flight), "error"/"recovering" are for
# the operator to investigate — not waste candidates.
COMPLETED_STATE = "completed"

# AWS writes the AMI id into the description of snapshots it
# creates for an AMI ("Created by CreateImage(i-...) for ami-...").
# Presence of the token means "AMI-owned -> cannot prove orphan
# -> NO_MATCH".
AMI_REFERENCE_TOKEN = "ami-"


def _should_emit(idx: dict[str, Fact]) -> bool:
    """Three NO_MATCH checks (state / volume_exists / AMI token) +
    one malformed-but-present check (volume_exists.value must be
    bool). Raises `StorageInconclusiveError` for the malformed case."""
    if cast(str, idx["aws.ec2.snapshot.state"].value) != COMPLETED_STATE:
        return False
    volume_exists_value = idx["aws.ec2.snapshot.volume_exists"].value
    if not isinstance(volume_exists_value, bool):
        # The correlation fact was written with a non-bool value
        # (e.g. None or a string from a buggy collector). Surfacing
        # the catalog gap is the honest move.
        raise StorageInconclusiveError("aws.ec2.snapshot.volume_exists.malformed") from None
    if volume_exists_value:
        # The source volume still exists -> the snapshot has a
        # consumer.
        return False
    description = idx["aws.ec2.snapshot.description"].value
    return not (isinstance(description, str) and AMI_REFERENCE_TOKEN in description.lower())


def _age_days(start_time_raw: object, today: date) -> int | None:
    """Days between the snapshot's StartTime (ISO string) and
    today. None when the value is missing; raises ValueError on a
    malformed string (the caller turns that into INCONCLUSIVE,
    never a silent drop)."""
    if not start_time_raw:
        return None
    if not isinstance(start_time_raw, str):
        raise ValueError(f"start_time is not an ISO string: {start_time_raw!r}")
    return (today - datetime.fromisoformat(start_time_raw).date()).days


def _compute_cost(idx: dict[str, Fact], today: date) -> StorageCost:
    """The single multiplication `size_gb x usd_per_gb_month` lives
    in this helper; the shared function never sees the
    arithmetic. Tier defaults to AWS's "standard" when the fact
    is absent; a tier the catalog doesn't know is INCONCLUSIVE,
    not $0. Age is payload evidence, not a match gate: a missing
    start_time -> age_days=None; a malformed start_time ->
    INCONCLUSIVE (a value we can't parse is a data problem worth
    surfacing)."""
    try:
        size_gb = int(idx["aws.ec2.snapshot.size_gb"].value)
    except (TypeError, ValueError) as exc:
        raise StorageInconclusiveError("aws.ec2.snapshot.size_gb.malformed") from exc

    tier_fact = idx.get("aws.ec2.snapshot.storage_tier")
    tier = "standard"
    if (
        tier_fact is not None
        and tier_fact.value_state == ValueState.KNOWN
        and tier_fact.value
    ):
        tier = str(tier_fact.value)
    region = cast(str, idx["aws.ec2.snapshot.region"].value)
    price = ebs_snapshot_price_per_gb_month(tier, region)
    if price is None:
        raise StorageInconclusiveError("catalog.snapshot_tier_price_missing") from None
    monthly_cost = round(price.usd_per_gb_month * size_gb, 2)

    start_time_fact = idx.get("aws.ec2.snapshot.start_time")
    try:
        age_days = _age_days(
            start_time_fact.value if start_time_fact is not None else None, today
        )
    except ValueError as exc:
        raise StorageInconclusiveError("aws.ec2.snapshot.start_time.malformed") from exc

    return StorageCost(
        monthly_usd=monthly_cost,
        monetary_payload_key="orphan_snapshot_monthly_usd",
        pricing_region=price.region,
        price_region_exact=price_region_exact(region, price),
        extras={
            "snapshot_size_gb": size_gb,
            "storage_tier": tier,
            "snapshot_age_days": age_days,
            "state": COMPLETED_STATE,
        },
    )


def _build_title(idx: dict[str, Fact], cost: StorageCost) -> str:
    age_days = cost.extras["snapshot_age_days"]
    age_label = f"{age_days} days old" if age_days is not None else "unknown age"
    return (
        f"EBS snapshot ({cost.extras['snapshot_size_gb']} GB, {age_label}) is orphaned — "
        f"its source volume no longer exists — costing ${cost.monthly_usd:.2f}/month"
    )


def _build_recommendation(idx: dict[str, Fact], cost: StorageCost) -> str:
    return (
        "Delete the snapshot — its source volume is gone, so it cannot "
        "serve a restore of that volume. Verify it is not referenced by "
        "any backup policy or AMI first: aws ec2 describe-images "
        "--filters Name=block-device-mapping.snapshot-id,Values=<id>, "
        "then aws ec2 delete-snapshot --snapshot-id <id>."
    )


CONFIG = StorageRuleConfig(
    rule_name=RULE_NAME,
    # The 5 facts the rule needs to conclude. `storage_tier` and
    # `start_time` are present-but-optional: the rule reads them
    # from the indexed fact dict inside `should_emit` /
    # `compute_cost` (default tier = "standard"; missing
    # start_time -> age_days=None, malformed -> INCONCLUSIVE).
    required_facts=(
        "aws.ec2.snapshot.state",
        "aws.ec2.snapshot.size_gb",
        "aws.ec2.snapshot.volume_exists",
        "aws.ec2.snapshot.description",
        "aws.ec2.snapshot.region",
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
    """Evaluate one EBS snapshot for orphan waste. Returns the
    same InsightResult shape as the per-rule evaluator did before
    the refactor."""
    return evaluate_storage(
        resource_id,
        facts,
        CONFIG,
        today=today,
        catalog_version=EBS_CATALOG_VERSION,
    )
