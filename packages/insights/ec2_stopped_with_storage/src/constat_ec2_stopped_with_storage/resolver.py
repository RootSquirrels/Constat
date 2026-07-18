"""EC2 stopped-with-storage insight.

A stopped EC2 instance bills $0 for compute, but its attached EBS
volumes keep billing storage — the classic "I stopped it months ago"
line item.

The cross-resource proof comes from the collector's correlation
post-pass: `aws.ec2.instance.attached_volumes` (list of
{volume_id, size_gb, volume_type}) is written only for stopped
instances and only when the region's volume scan succeeded. Absence
of the fact = scope not proven = INCONCLUSIVE, never a guessed MATCH.

MATCH: state="stopped" + attached_volumes present and non-empty ->
  1 insight with the summed monthly storage cost.
NO_MATCH: any other state (running, terminated, pending, ...), or a
  proven-empty attached_volumes list (instance-store only).
INCONCLUSIVE: missing state or attached_volumes fact, or a malformed
  attached_volumes value.

Partial pricing (decided): if ANY attached volume's type is not in the
catalog (e.g. a future io3), the insight is still emitted with the
partial sum and `pricing_incomplete: true` — the finding "instance
stopped, paying storage" is certain; only the amount is degraded.
This differs from ebs_unattached (unknown type -> INCONCLUSIVE)
because there the unknown type IS the whole finding.

Out of scope (stated, not silent): Elastic IP cost. A stopped instance
with an associated Elastic IP also bills for the idle IP, but
DescribeAddresses is NOT collected in V1, so the amount is excluded —
`elastic_ip_cost_excluded: true` is stamped on every payload.

Severity matches the ebs_unattached thresholds ($500/CRITICAL,
$50/WARNING) for dashboard consistency. value_basis=ESTIMATED until
FOCUS reconciles. catalog_version stamped on every insight payload.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from constat_core.catalog.ebs import EBS_CATALOG_VERSION, monthly_storage_cost
from constat_core.models import Fact, Insight, Severity, ValueState

RULE_NAME = "ec2_stopped_with_storage"
SOURCE_NAME = "aws_ec2"

STOPPED_STATE = "stopped"


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
    """Evaluate one EC2 instance for stopped-with-storage waste.

    Returns an InsightResult with:
    - insights: a single Insight if the instance is stopped with
      attached, billable volumes, else [].
    - inconclusive_reasons: missing facts that block assessment.
    """
    idx = _index_facts(facts)

    state_fact = _get(idx, "aws.ec2.instance.state")

    # Gate 1: state must be KNOWN.
    if state_fact is None or state_fact.value_state != ValueState.KNOWN:
        return InsightResult(insights=[], inconclusive_reasons=["aws.ec2.instance.state"])

    # NO_MATCH for anything that isn't "stopped" — a running instance's
    # storage is working, a terminated one's volumes are deleted or
    # orphaned (the ebs_unattached rule picks those up).
    state = state_fact.value
    if state != STOPPED_STATE:
        return InsightResult()

    # Gate 2: attached_volumes must be present — its existence IS the
    # scope proof (written only when the region's volume scan ran).
    volumes_fact = _get(idx, "aws.ec2.instance.attached_volumes")
    if volumes_fact is None:
        return InsightResult(
            insights=[], inconclusive_reasons=["aws.ec2.instance.attached_volumes"]
        )
    attached = volumes_fact.value
    if not isinstance(attached, list) or any(not isinstance(v, dict) for v in attached):
        return InsightResult(
            insights=[], inconclusive_reasons=["aws.ec2.instance.attached_volumes.malformed"]
        )

    # Proven zero attached volumes -> NO_MATCH (instance-store only).
    if not attached:
        return InsightResult()

    breakdown: list[dict[str, Any]] = []
    pricing_incomplete = False
    total = 0.0
    for vol in attached:
        volume_id = vol.get("volume_id")
        volume_type = vol.get("volume_type")
        size_gb = vol.get("size_gb")
        monthly = (
            monthly_storage_cost(str(volume_type), int(size_gb))
            if volume_type is not None and size_gb is not None
            else None
        )
        if monthly is None:
            # Uncatalogued type (or missing size): skip from the sum
            # but degrade the amount honestly — the finding stands.
            pricing_incomplete = True
        else:
            total += monthly
        breakdown.append(
            {
                "volume_id": volume_id,
                "size_gb": size_gb,
                "volume_type": volume_type,
                "monthly_usd": monthly,
            }
        )
    total = round(total, 2)

    return InsightResult(
        insights=[
            _make_insight(
                resource_id=resource_id,
                account_id=state_fact.account_id,
                volumes=breakdown,
                pricing_incomplete=pricing_incomplete,
                monthly_cost_usd=total,
            )
        ]
    )


def _make_insight(
    *,
    resource_id: UUID,
    account_id: str | None,
    volumes: list[dict[str, Any]],
    pricing_incomplete: bool,
    monthly_cost_usd: float,
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

    count = len(volumes)
    title = (
        f"EC2 instance is stopped but {count} attached EBS volume(s) still "
        f"cost ${monthly_cost_usd:.2f}/month"
    )
    recommendation = (
        "If the instance is decommissioned, snapshot the volumes (if the "
        "data matters), then terminate the instance with DeleteOnTermination "
        "or delete the volumes: aws ec2 delete-volume --volume-id <id>. "
        "If it restarts on a schedule, consider moving cold data to "
        "cheaper storage (sc1) or S3. Note: an associated Elastic IP "
        "also bills while the instance is stopped — not included here."
    )

    return Insight(
        rule_name=RULE_NAME,
        resource_id=resource_id,
        account_id=account_id,
        severity=severity,
        title=title,
        payload={
            "state": STOPPED_STATE,
            "volume_count": count,
            "volumes": volumes,
            "stopped_storage_monthly_usd": monthly_cost_usd,
            "pricing_incomplete": pricing_incomplete,
            "elastic_ip_cost_excluded": True,
            "value_basis": "ESTIMATED",
            "recommendation": recommendation,
            "catalog_version": EBS_CATALOG_VERSION,
        },
    )
