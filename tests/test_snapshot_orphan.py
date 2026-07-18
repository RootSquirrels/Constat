"""Tests for the snapshot_orphan insight rule + collector correlation + runner wiring.

Three concerns: the 3-state contract (MATCH / NO_MATCH / INCONCLUSIVE),
the cross-resource correlation facts the rule depends on
(`aws.ec2.snapshot.volume_exists`, written by the collector post-pass),
and the runner integration (registration + source binding + end-to-end).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from constat_api.insights.runner import (
    RESOURCE_RULES,
    RULE_SOURCES,
    run_snapshot_orphan,
)
from constat_api.orm import (
    AccountORM,
    FactORM,
    InsightORM,
    ResourceORM,
    SourceRunORM,
)
from constat_api.settings import DEFAULT_TENANT_ID
from constat_aws_ec2.collector import correlation_facts, snapshot_to_facts
from constat_core.catalog.ebs import EBS_CATALOG_VERSION
from constat_core.models import Fact, Severity, ValueState
from constat_snapshot_orphan.resolver import RULE_NAME, evaluate
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Resolver fixtures
# ---------------------------------------------------------------------------


def _fact(key: str, value: Any, *, value_state: ValueState = ValueState.KNOWN) -> Fact:
    return Fact(
        resource_id=uuid4(),
        account_id="111111111111",
        namespace="aws.ec2.snapshot",
        key=key,
        value=value,
        value_state=value_state,
        source="aws_ec2",
        observed_at=datetime.now(tz=UTC),
    )


def _orphan_facts(
    *,
    size_gb: int = 200,
    state: str = "completed",
    volume_exists: bool = False,
    tier: str | None = "standard",
    description: str | None = "manual backup before migration",
    start_time: str | None = "2026-01-01T00:00:00+00:00",
) -> list[Fact]:
    """Minimal fact set for one snapshot as the collector emits it."""
    facts = [
        _fact("state", state),
        _fact("size_gb", size_gb),
        _fact("volume_exists", volume_exists),
    ]
    if tier is not None:
        facts.append(_fact("storage_tier", tier))
    if description is not None:
        facts.append(_fact("description", description))
    if start_time is not None:
        facts.append(_fact("start_time", start_time))
    return facts


TODAY = date(2026, 7, 18)


# ---------------------------------------------------------------------------
# Rule: MATCH
# ---------------------------------------------------------------------------


def test_completed_orphan_emits_match() -> None:
    """The headline case: completed snapshot, volume gone, no AMI
    reference -> one insight with the monthly cost and the age."""
    result = evaluate(uuid4(), _orphan_facts(size_gb=200), today=TODAY)

    assert result.is_conclusive
    assert result.has_gap
    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight.rule_name == RULE_NAME
    # 200 GB * $0.05/GB-month (standard tier) = $10/month
    assert insight.payload["orphan_snapshot_monthly_usd"] == 10.00
    assert insight.payload["storage_tier"] == "standard"
    # 2026-01-01 -> 2026-07-18 = 198 days
    assert insight.payload["snapshot_age_days"] == 198
    assert insight.payload["value_basis"] == "ESTIMATED"
    assert insight.payload["catalog_version"] == EBS_CATALOG_VERSION


def test_missing_tier_fact_defaults_to_standard() -> None:
    """No storage_tier fact collected -> AWS's default tier (standard),
    not INCONCLUSIVE."""
    result = evaluate(uuid4(), _orphan_facts(size_gb=200, tier=None), today=TODAY)
    assert result.has_gap
    assert result.insights[0].payload["orphan_snapshot_monthly_usd"] == 10.00


def test_severity_thresholds_match_ebs_unattached() -> None:
    """Same severity scale as ebs_unattached for dashboard consistency.
    >= $500 = CRITICAL, >= $50 = WARNING, else INFO."""
    # 100 GB standard = 100 * 0.05 = $5 -> INFO
    r1 = evaluate(uuid4(), _orphan_facts(size_gb=100), today=TODAY)
    assert r1.insights[0].severity == Severity.INFO
    # 1000 GB standard = $50 -> WARNING
    r2 = evaluate(uuid4(), _orphan_facts(size_gb=1000), today=TODAY)
    assert r2.insights[0].severity == Severity.WARNING
    # 10000 GB standard = $500 -> CRITICAL
    r3 = evaluate(uuid4(), _orphan_facts(size_gb=10000), today=TODAY)
    assert r3.insights[0].severity == Severity.CRITICAL


# ---------------------------------------------------------------------------
# Rule: NO_MATCH
# ---------------------------------------------------------------------------


def test_volume_still_exists_emits_nothing() -> None:
    """volume_exists=True -> the snapshot has a consumer. NO_MATCH."""
    result = evaluate(uuid4(), _orphan_facts(volume_exists=True), today=TODAY)
    assert result.is_conclusive
    assert not result.has_gap
    assert result.insights == []


def test_ami_referenced_description_emits_nothing() -> None:
    """AWS writes the AMI id into descriptions of AMI-owned snapshots
    ("Created by CreateImage(i-...) for ami-..."). We cannot prove
    orphanhood of those without DescribeImages -> conservative NO_MATCH."""
    facts = _orphan_facts(
        description="Created by CreateImage(i-0123456789abcdef0) for ami-0abcdef1234567890"
    )
    result = evaluate(uuid4(), facts, today=TODAY)
    assert result.is_conclusive
    assert not result.has_gap
    assert result.insights == []


def test_non_completed_state_emits_nothing() -> None:
    """pending/error snapshots are transient or operator business, not
    waste candidates. NO_MATCH, not INCONCLUSIVE."""
    result = evaluate(uuid4(), _orphan_facts(state="pending"), today=TODAY)
    assert result.is_conclusive
    assert not result.has_gap
    assert result.inconclusive_reasons == []


# ---------------------------------------------------------------------------
# Rule: INCONCLUSIVE
# ---------------------------------------------------------------------------


def test_missing_volume_exists_emits_inconclusive() -> None:
    """No volume_exists fact = the volume scope was not proven in this
    collection (the volume job didn't run or failed). Absence of proof,
    not proof of absence -> INCONCLUSIVE, never a guessed MATCH."""
    facts = [f for f in _orphan_facts() if f.key != "volume_exists"]
    result = evaluate(uuid4(), facts, today=TODAY)
    assert not result.is_conclusive
    assert "aws.ec2.snapshot.volume_exists" in result.inconclusive_reasons


def test_unknown_state_emits_inconclusive() -> None:
    facts = [
        _fact("state", None, value_state=ValueState.UNKNOWN),
        _fact("size_gb", 200),
        _fact("volume_exists", False),
        _fact("description", "backup"),
    ]
    result = evaluate(uuid4(), facts, today=TODAY)
    assert not result.is_conclusive
    assert "aws.ec2.snapshot.state" in result.inconclusive_reasons


def test_unknown_size_emits_inconclusive() -> None:
    facts = [
        _fact("state", "completed"),
        _fact("size_gb", None, value_state=ValueState.UNKNOWN),
        _fact("volume_exists", False),
        _fact("description", "backup"),
    ]
    result = evaluate(uuid4(), facts, today=TODAY)
    assert not result.is_conclusive
    assert "aws.ec2.snapshot.size_gb" in result.inconclusive_reasons


def test_missing_description_emits_inconclusive() -> None:
    """Without the description we cannot rule out AMI ownership, and
    matching an AMI-owned snapshot would be a destructive
    recommendation -> INCONCLUSIVE, not "no AMI reference found"."""
    facts = _orphan_facts(description=None)
    result = evaluate(uuid4(), facts, today=TODAY)
    assert not result.is_conclusive
    assert "aws.ec2.snapshot.description" in result.inconclusive_reasons


def test_unknown_tier_emits_inconclusive() -> None:
    """A storage tier not in the catalog (e.g. a future tier) is
    INCONCLUSIVE, not a free $0/GB-month surprise. Same defensive
    pattern as ebs_unattached's unknown volume type."""
    result = evaluate(uuid4(), _orphan_facts(tier="glacier-deep"), today=TODAY)
    assert not result.is_conclusive
    assert "catalog.snapshot_tier_price_missing" in result.inconclusive_reasons


def test_archive_tier_prices_at_archive_rate() -> None:
    """Archive-tier snapshots price at $0.0125/GB-month."""
    result = evaluate(uuid4(), _orphan_facts(size_gb=1000, tier="archive"), today=TODAY)
    assert result.has_gap
    assert result.insights[0].payload["orphan_snapshot_monthly_usd"] == 12.50


def test_malformed_start_time_emits_inconclusive() -> None:
    result = evaluate(uuid4(), _orphan_facts(start_time="not-a-date"), today=TODAY)
    assert not result.is_conclusive
    assert "aws.ec2.snapshot.start_time.malformed" in result.inconclusive_reasons


# ---------------------------------------------------------------------------
# Connector: snapshot_to_facts + the correlation post-pass
# ---------------------------------------------------------------------------


def _snap_raw(
    snapshot_id: str = "snap-1",
    volume_id: str | None = "vol-1",
    state: str = "completed",
) -> dict[str, Any]:
    return {
        "SnapshotId": snapshot_id,
        "State": state,
        "VolumeSize": 200,
        "VolumeId": volume_id,
        "Encrypted": True,
        "OwnerId": "111111111111",
        "StartTime": datetime(2026, 1, 1, tzinfo=UTC),
        "Description": "manual backup",
        "StorageTier": "standard",
        "Tags": [],
    }


def _vol_raw(volume_id: str = "vol-1") -> dict[str, Any]:
    return {
        "VolumeId": volume_id,
        "VolumeType": "gp2",
        "State": "available",
        "Size": 100,
        "Tags": [],
    }


def test_snapshot_to_facts_produces_rule_inputs() -> None:
    """The per-item facts factory emits the keys the rule reads."""
    facts = snapshot_to_facts(uuid4(), "111111111111", _snap_raw(), datetime.now(tz=UTC))
    by_key = {f.key: f for f in facts}
    assert by_key["state"].value == "completed"
    assert by_key["size_gb"].value == 200
    assert by_key["storage_tier"].value == "standard"
    assert by_key["volume_id"].value == "vol-1"
    assert by_key["start_time"].value_state == ValueState.KNOWN
    assert by_key["description"].value == "manual backup"
    # volume_exists is NOT a per-item fact — the post-pass writes it.
    assert "volume_exists" not in by_key


def test_correlation_marks_volume_exists_true_and_false() -> None:
    """A snapshot whose VolumeId was scanned -> True; one whose VolumeId
    the volume scan did NOT see -> False. Both are proven facts."""
    snap_present = (uuid4(), _snap_raw("snap-1", volume_id="vol-1"))
    snap_gone = (uuid4(), _snap_raw("snap-2", volume_id="vol-deleted"))
    facts = correlation_facts(
        volumes=[(uuid4(), _vol_raw("vol-1"))],
        snapshots=[snap_present, snap_gone],
        instances=[],
        account_id="111111111111",
        observed_at=datetime.now(tz=UTC),
    )
    by_resource = {f.resource_id: f for f in facts}
    assert by_resource[snap_present[0]].key == "volume_exists"
    assert by_resource[snap_present[0]].value is True
    assert by_resource[snap_gone[0]].value is False


def test_correlation_writes_nothing_when_volume_job_missing() -> None:
    """volumes=None means the volume job didn't run / failed in the
    region: NO correlation fact at all. The rule then goes INCONCLUSIVE
    (missing fact) instead of matching on a guessed False."""
    facts = correlation_facts(
        volumes=None,
        snapshots=[(uuid4(), _snap_raw())],
        instances=[],
        account_id="111111111111",
        observed_at=datetime.now(tz=UTC),
    )
    assert facts == []


# ---------------------------------------------------------------------------
# Runner: registration + source + end-to-end
# ---------------------------------------------------------------------------


def test_snapshot_orphan_registered_in_resource_rules() -> None:
    assert "snapshot_orphan" in RESOURCE_RULES


def test_snapshot_orphan_source_is_aws_ec2() -> None:
    """Scope-completeness uses the aws_ec2 source, NOT aws_rds."""
    assert RULE_SOURCES["snapshot_orphan"] == "aws_ec2"


def _seed_account(session: Session) -> AccountORM:
    acc = AccountORM(external_id="111111111111", name="prod")
    session.add(acc)
    session.commit()
    return acc


def _seed_snapshot_scope_proof(
    session: Session, account: AccountORM, region: str = "eu-west-1"
) -> None:
    run = SourceRunORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=account.id,
        region=region,
        resource_type="AWS::EC2::Snapshot",
        source="aws_ec2",
        status="success",
        started_at=datetime.now(tz=UTC),
        finished_at=datetime.now(tz=UTC),
        resources_found=1,
    )
    session.add(run)
    session.commit()


def _seed_snapshot_with_facts(
    session: Session,
    account: AccountORM,
    region: str,
    native_id: str,
    facts_by_key: dict[str, object],
) -> ResourceORM:
    res = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=account.id,
        region=region,
        resource_type="AWS::EC2::Snapshot",
        native_id=native_id,
    )
    session.add(res)
    session.commit()
    for key, value in facts_by_key.items():
        fact = FactORM(
            tenant_id=DEFAULT_TENANT_ID,
            resource_id=res.id,
            account_id=account.id,
            namespace="aws.ec2.snapshot",
            key=key,
            value=value,
            value_state="KNOWN",
            source="aws_ec2",
            observed_at=datetime.now(tz=UTC),
        )
        session.add(fact)
    session.commit()
    return res


def test_run_snapshot_orphan_emits_insight_for_orphan(session: Session) -> None:
    """End-to-end: a 1000 GB standard-tier orphan snapshot emits one
    insight under the right rule name."""
    acc = _seed_account(session)
    _seed_snapshot_scope_proof(session, acc, region="eu-west-1")
    _seed_snapshot_with_facts(
        session,
        acc,
        "eu-west-1",
        "snap-1",
        {
            "state": "completed",
            "size_gb": 1000,
            "storage_tier": "standard",
            "volume_exists": False,
            "description": "weekly backup",
        },
    )

    result = run_snapshot_orphan(session)

    assert result.rule_name == "snapshot_orphan"
    assert result.insights_emitted == 1
    insight = session.query(InsightORM).one()
    assert insight.rule_name == "snapshot_orphan"
    # 1000 GB * $0.05 = $50/month -> WARNING
    assert insight.severity == "warning"
    assert insight.payload["orphan_snapshot_monthly_usd"] == 50.00


def test_run_snapshot_orphan_replaces_previous_insights(session: Session) -> None:
    """Delete-and-replace (audit F-03): three consecutive runs keep the
    insight count constant — re-runs never accumulate duplicates."""
    acc = _seed_account(session)
    _seed_snapshot_scope_proof(session, acc, region="eu-west-1")
    _seed_snapshot_with_facts(
        session,
        acc,
        "eu-west-1",
        "snap-1",
        {
            "state": "completed",
            "size_gb": 1000,
            "storage_tier": "standard",
            "volume_exists": False,
            "description": "weekly backup",
        },
    )

    for _ in range(3):
        result = run_snapshot_orphan(session)
        assert result.insights_emitted == 1
        assert session.query(InsightORM).count() == 1
