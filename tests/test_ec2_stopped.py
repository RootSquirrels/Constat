"""Tests for the ec2_stopped_with_storage insight rule + collector correlation + runner wiring.

Three concerns: the 3-state contract (MATCH / NO_MATCH / INCONCLUSIVE),
the cross-resource correlation fact the rule depends on
(`aws.ec2.instance.attached_volumes`, written by the collector
post-pass), and the runner integration (registration + source binding +
end-to-end).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from constat_api.insights.runner import (
    RESOURCE_RULES,
    RULE_SOURCES,
    run_ec2_stopped_with_storage,
)
from constat_api.orm import (
    AccountORM,
    FactORM,
    InsightORM,
    ResourceORM,
    SourceRunORM,
)
from constat_api.settings import DEFAULT_TENANT_ID
from constat_aws_ec2.collector import correlation_facts, instance_to_facts
from constat_core.catalog.ebs import EBS_CATALOG_VERSION
from constat_core.models import Fact, Severity, ValueState
from constat_ec2_stopped_with_storage.resolver import RULE_NAME, evaluate
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Resolver fixtures
# ---------------------------------------------------------------------------


def _fact(key: str, value: Any, *, value_state: ValueState = ValueState.KNOWN) -> Fact:
    return Fact(
        resource_id=uuid4(),
        account_id="111111111111",
        namespace="aws.ec2.instance",
        key=key,
        value=value,
        value_state=value_state,
        source="aws_ec2",
        observed_at=datetime.now(tz=UTC),
    )


def _stopped_facts(
    *,
    state: str = "stopped",
    attached_volumes: list[dict[str, Any]] | None = None,
) -> list[Fact]:
    """Minimal fact set for one instance as the collector emits it.
    attached_volumes=None means the fact is absent (volume scope not
    proven); pass [] for a proven-empty list."""
    facts = [_fact("state", state)]
    if attached_volumes is not None:
        facts.append(_fact("attached_volumes", attached_volumes))
    return facts


# ---------------------------------------------------------------------------
# Rule: MATCH
# ---------------------------------------------------------------------------


def test_stopped_instance_with_two_volumes_sums_correctly() -> None:
    """The headline case: stopped instance, two attached volumes, cost
    is the SUM of per-volume storage."""
    facts = _stopped_facts(
        attached_volumes=[
            {"volume_id": "vol-1", "size_gb": 100, "volume_type": "gp2"},
            {"volume_id": "vol-2", "size_gb": 50, "volume_type": "gp3"},
        ]
    )
    result = evaluate(uuid4(), facts)

    assert result.is_conclusive
    assert result.has_gap
    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight.rule_name == RULE_NAME
    # 100 GB gp2 ($0.10) + 50 GB gp3 ($0.08) = $10 + $4 = $14/month
    assert insight.payload["stopped_storage_monthly_usd"] == 14.00
    assert insight.payload["volume_count"] == 2
    assert len(insight.payload["volumes"]) == 2
    assert insight.payload["pricing_incomplete"] is False
    assert insight.payload["elastic_ip_cost_excluded"] is True
    assert insight.payload["value_basis"] == "ESTIMATED"
    assert insight.payload["catalog_version"] == EBS_CATALOG_VERSION


def test_uncatalogued_volume_type_degrades_amount_not_finding() -> None:
    """Decided semantics: an unknown volume type is skipped from the
    sum and flagged via pricing_incomplete — the finding "instance
    stopped, paying storage" is certain, only the amount is degraded.
    (Different from ebs_unattached, where the unknown type IS the
    whole finding and goes INCONCLUSIVE.)"""
    facts = _stopped_facts(
        attached_volumes=[
            {"volume_id": "vol-1", "size_gb": 100, "volume_type": "gp2"},
            {"volume_id": "vol-2", "size_gb": 50, "volume_type": "io99"},
        ]
    )
    result = evaluate(uuid4(), facts)

    assert result.is_conclusive
    assert result.has_gap
    insight = result.insights[0]
    # Only the gp2 volume is priced: 100 * $0.10 = $10
    assert insight.payload["stopped_storage_monthly_usd"] == 10.00
    assert insight.payload["pricing_incomplete"] is True
    priced = {v["volume_id"]: v["monthly_usd"] for v in insight.payload["volumes"]}
    assert priced["vol-1"] == 10.00
    assert priced["vol-2"] is None


def test_severity_thresholds_match_ebs_unattached() -> None:
    """Same severity scale as ebs_unattached for dashboard consistency.
    >= $500 = CRITICAL, >= $50 = WARNING, else INFO."""
    # 100 GB gp2 = $10 -> INFO
    r1 = evaluate(
        uuid4(),
        _stopped_facts(attached_volumes=[{"volume_id": "v", "size_gb": 100, "volume_type": "gp2"}]),
    )
    assert r1.insights[0].severity == Severity.INFO
    # 1000 GB gp2 = $100 -> WARNING
    r2 = evaluate(
        uuid4(),
        _stopped_facts(
            attached_volumes=[{"volume_id": "v", "size_gb": 1000, "volume_type": "gp2"}]
        ),
    )
    assert r2.insights[0].severity == Severity.WARNING
    # 10000 GB gp2 = $1000 -> CRITICAL
    r3 = evaluate(
        uuid4(),
        _stopped_facts(
            attached_volumes=[{"volume_id": "v", "size_gb": 10000, "volume_type": "gp2"}]
        ),
    )
    assert r3.insights[0].severity == Severity.CRITICAL


# ---------------------------------------------------------------------------
# Rule: NO_MATCH
# ---------------------------------------------------------------------------


def test_running_instance_emits_nothing() -> None:
    """A running instance's storage is working. NO_MATCH — and the
    attached_volumes fact is not required (the collector only writes it
    for stopped instances)."""
    result = evaluate(uuid4(), _stopped_facts(state="running"))
    assert result.is_conclusive
    assert not result.has_gap
    assert result.insights == []


def test_terminated_instance_emits_nothing() -> None:
    result = evaluate(uuid4(), _stopped_facts(state="terminated"))
    assert result.is_conclusive
    assert not result.has_gap


def test_proven_zero_attached_volumes_emits_nothing() -> None:
    """An empty list IS a proven observation (instance-store only):
    NO_MATCH, distinct from a missing fact (INCONCLUSIVE)."""
    result = evaluate(uuid4(), _stopped_facts(attached_volumes=[]))
    assert result.is_conclusive
    assert not result.has_gap
    assert result.inconclusive_reasons == []


# ---------------------------------------------------------------------------
# Rule: INCONCLUSIVE
# ---------------------------------------------------------------------------


def test_missing_attached_volumes_emits_inconclusive() -> None:
    """Stopped instance but no attached_volumes fact = the volume scope
    was not proven in this collection. INCONCLUSIVE, never a guessed
    "no volumes"."""
    result = evaluate(uuid4(), _stopped_facts())
    assert not result.is_conclusive
    assert "aws.ec2.instance.attached_volumes" in result.inconclusive_reasons


def test_unknown_state_emits_inconclusive() -> None:
    facts = [
        _fact("state", None, value_state=ValueState.UNKNOWN),
        _fact("attached_volumes", [{"volume_id": "v", "size_gb": 100, "volume_type": "gp2"}]),
    ]
    result = evaluate(uuid4(), facts)
    assert not result.is_conclusive
    assert "aws.ec2.instance.state" in result.inconclusive_reasons


def test_malformed_attached_volumes_emits_inconclusive() -> None:
    result = evaluate(uuid4(), _stopped_facts(attached_volumes="vol-1,vol-2"))
    assert not result.is_conclusive
    assert "aws.ec2.instance.attached_volumes.malformed" in result.inconclusive_reasons


# ---------------------------------------------------------------------------
# Connector: instance_to_facts + the correlation post-pass
# ---------------------------------------------------------------------------


def _inst_raw(
    instance_id: str = "i-1",
    state: str = "stopped",
    volume_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "InstanceId": instance_id,
        "InstanceType": "t3.medium",
        "State": {"Name": state, "Code": 80 if state == "stopped" else 16},
        "ImageId": "ami-abc",
        "LaunchTime": datetime(2024, 6, 1, tzinfo=UTC),
        "BlockDeviceMappings": [
            {"DeviceName": f"/dev/sd{chr(97 + i)}", "Ebs": {"VolumeId": vid}}
            for i, vid in enumerate(volume_ids or [])
        ],
        "Tags": [],
    }


def _vol_raw(volume_id: str, size: int = 100, volume_type: str = "gp2") -> dict[str, Any]:
    return {
        "VolumeId": volume_id,
        "VolumeType": volume_type,
        "State": "in-use",
        "Size": size,
        "Tags": [],
    }


def test_instance_to_facts_produces_block_device_volume_ids() -> None:
    """The per-item facts factory emits state/type/launch_time and the
    raw block-device volume ids (unresolved — sizes come from the
    post-pass)."""
    facts = instance_to_facts(
        uuid4(), "111111111111", _inst_raw(volume_ids=["vol-1", "vol-2"]), datetime.now(tz=UTC)
    )
    by_key = {f.key: f for f in facts}
    assert by_key["state"].value == "stopped"
    assert by_key["instance_type"].value == "t3.medium"
    assert by_key["launch_time"].value_state == ValueState.KNOWN
    assert by_key["block_device_volume_ids"].value == ["vol-1", "vol-2"]
    # attached_volumes is NOT a per-item fact — the post-pass writes it.
    assert "attached_volumes" not in by_key


def test_correlation_resolves_attached_volumes_from_volume_scan() -> None:
    """Stopped instance: BlockDeviceMappings volume ids are resolved to
    sizes/types from the region's volume raws. A volume id the scan
    didn't see is skipped (deleted since, or cross-account)."""
    inst = (uuid4(), _inst_raw(volume_ids=["vol-1", "vol-2", "vol-gone"]))
    facts = correlation_facts(
        volumes=[(uuid4(), _vol_raw("vol-1", 100, "gp2")), (uuid4(), _vol_raw("vol-2", 50, "gp3"))],
        snapshots=[],
        instances=[inst],
        account_id="111111111111",
        observed_at=datetime.now(tz=UTC),
    )
    assert len(facts) == 1
    fact = facts[0]
    assert fact.resource_id == inst[0]
    assert fact.key == "attached_volumes"
    assert fact.value == [
        {"volume_id": "vol-1", "size_gb": 100, "volume_type": "gp2"},
        {"volume_id": "vol-2", "size_gb": 50, "volume_type": "gp3"},
    ]


def test_correlation_skips_non_stopped_instances() -> None:
    """attached_volumes is written for stopped instances only — the
    rule NO_MATCHes other states on the state fact alone."""
    facts = correlation_facts(
        volumes=[(uuid4(), _vol_raw("vol-1"))],
        snapshots=[],
        instances=[(uuid4(), _inst_raw(state="running", volume_ids=["vol-1"]))],
        account_id="111111111111",
        observed_at=datetime.now(tz=UTC),
    )
    assert facts == []


def test_correlation_writes_nothing_when_volume_job_missing() -> None:
    """volumes=None means the volume job didn't run / failed in the
    region: NO correlation fact at all -> the rule goes INCONCLUSIVE."""
    facts = correlation_facts(
        volumes=None,
        snapshots=[],
        instances=[(uuid4(), _inst_raw(volume_ids=["vol-1"]))],
        account_id="111111111111",
        observed_at=datetime.now(tz=UTC),
    )
    assert facts == []


# ---------------------------------------------------------------------------
# Runner: registration + source + end-to-end
# ---------------------------------------------------------------------------


def test_ec2_stopped_with_storage_registered_in_resource_rules() -> None:
    assert "ec2_stopped_with_storage" in RESOURCE_RULES


def test_ec2_stopped_with_storage_source_is_aws_ec2() -> None:
    """Scope-completeness uses the aws_ec2 source, NOT aws_rds."""
    assert RULE_SOURCES["ec2_stopped_with_storage"] == "aws_ec2"


def _seed_account(session: Session) -> AccountORM:
    acc = AccountORM(external_id="111111111111", name="prod")
    session.add(acc)
    session.commit()
    return acc


def _seed_instance_scope_proof(
    session: Session, account: AccountORM, region: str = "eu-west-1"
) -> None:
    run = SourceRunORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=account.id,
        region=region,
        resource_type="AWS::EC2::Instance",
        source="aws_ec2",
        status="success",
        started_at=datetime.now(tz=UTC),
        finished_at=datetime.now(tz=UTC),
        resources_found=1,
    )
    session.add(run)
    session.commit()


def _seed_instance_with_facts(
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
        resource_type="AWS::EC2::Instance",
        native_id=native_id,
    )
    session.add(res)
    session.commit()
    for key, value in facts_by_key.items():
        fact = FactORM(
            tenant_id=DEFAULT_TENANT_ID,
            resource_id=res.id,
            account_id=account.id,
            namespace="aws.ec2.instance",
            key=key,
            value=value,
            value_state="KNOWN",
            source="aws_ec2",
            observed_at=datetime.now(tz=UTC),
        )
        session.add(fact)
    session.commit()
    return res


def _stopped_instance_facts() -> dict[str, object]:
    return {
        "state": "stopped",
        "attached_volumes": [
            {"volume_id": "vol-1", "size_gb": 500, "volume_type": "gp2"},
        ],
    }


def test_run_ec2_stopped_with_storage_emits_insight(session: Session) -> None:
    """End-to-end: a stopped instance with a 500 GB gp2 volume emits
    one insight under the right rule name."""
    acc = _seed_account(session)
    _seed_instance_scope_proof(session, acc, region="eu-west-1")
    _seed_instance_with_facts(session, acc, "eu-west-1", "i-1", _stopped_instance_facts())

    result = run_ec2_stopped_with_storage(session)

    assert result.rule_name == "ec2_stopped_with_storage"
    assert result.insights_emitted == 1
    insight = session.query(InsightORM).one()
    assert insight.rule_name == "ec2_stopped_with_storage"
    # 500 GB * $0.10 = $50/month -> WARNING
    assert insight.severity == "warning"
    assert insight.payload["stopped_storage_monthly_usd"] == 50.00


def test_run_ec2_stopped_with_storage_replaces_previous_insights(session: Session) -> None:
    """Delete-and-replace (audit F-03): three consecutive runs keep the
    insight count constant — re-runs never accumulate duplicates."""
    acc = _seed_account(session)
    _seed_instance_scope_proof(session, acc, region="eu-west-1")
    _seed_instance_with_facts(session, acc, "eu-west-1", "i-1", _stopped_instance_facts())

    for _ in range(3):
        result = run_ec2_stopped_with_storage(session)
        assert result.insights_emitted == 1
        assert session.query(InsightORM).count() == 1
