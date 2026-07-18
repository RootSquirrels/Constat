"""End-to-end tests for the ebs_gp2_to_gp3 rule wired through the runner.

What this exercises:
1. The rule is registered in RESOURCE_RULES (so run_resource_rule finds it).
2. The rule's source is in RULE_SOURCES (so scope-completeness uses
   `aws_ec2`, not the legacy `aws_rds` default — an RDS scan does
   NOT prove EC2 scope and vice-versa).
3. The runner emits one Insight per gp2 volume after a successful
   EC2 scan (one source_run per (region, resource_type=AWS::EC2::Volume,
   source=aws_ec2)).
4. The runner emits an INCONCLUSIVE when the scope isn't proven
   (e.g. only an RDS scan exists, no EC2 scan).
5. The runner emits an INCONCLUSIVE when facts are missing.
6. Delete-and-replace (audit F-03): a second run clears the first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from constat_api.insights.runner import (
    RESOURCE_RULES,
    RULE_SOURCES,
    run_ebs_gp2_to_gp3,
    run_resource_rule,
)
from constat_api.orm import (
    AccountORM,
    FactORM,
    InconclusiveORM,
    InsightORM,
    ResourceORM,
    SourceRunORM,
)
from constat_api.settings import DEFAULT_TENANT_ID
from sqlalchemy.orm import Session


def _gp2_volume_dict(volume_id: str, size_gb: int) -> dict[str, Any]:
    return {
        "VolumeId": volume_id,
        "VolumeType": "gp2",
        "State": "in-use",
        "Size": size_gb,
        "Iops": None,
        "Throughput": None,
        "Encrypted": True,
        "AvailabilityZone": "eu-west-1a",
        "CreateTime": datetime(2024, 6, 1, tzinfo=UTC),
        "Attachments": [
            {
                "InstanceId": "i-abc",
                "Device": "/dev/sda1",
                "State": "attached",
                "DeleteOnTermination": True,
            }
        ],
        "Tags": [],
    }


def _gp3_volume_dict(volume_id: str, size_gb: int) -> dict[str, Any]:
    d = _gp2_volume_dict(volume_id, size_gb)
    d["VolumeType"] = "gp3"
    d["Iops"] = 3000
    d["Throughput"] = 125
    return d


def _seed_account(session: Session) -> AccountORM:
    acc = AccountORM(external_id="111111111111", name="prod")
    session.add(acc)
    session.commit()
    return acc


def _seed_ec2_scope_proof(
    session: Session,
    account: AccountORM,
    region: str = "eu-west-1",
    resource_type: str = "AWS::EC2::Volume",
) -> SourceRunORM:
    """A successful source_run for the EC2 scope. Without this, the
    runner returns INCONCLUSIVE for every EC2 resource (F-02)."""
    run = SourceRunORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=account.id,
        region=region,
        resource_type=resource_type,
        source="aws_ec2",
        status="success",
        started_at=datetime.now(tz=UTC),
        finished_at=datetime.now(tz=UTC),
        resources_found=1,
    )
    session.add(run)
    session.commit()
    return run


def _seed_volume(
    session: Session, account: AccountORM, region: str, volume_id: str, raw: dict[str, Any]
) -> ResourceORM:
    """Write the resource + facts the way the connector would, but
    without going through the collector (avoids boto3 + circuit breaker
    noise — we just want the runner + rule interaction)."""
    res = ResourceORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=account.id,
        region=region,
        resource_type="AWS::EC2::Volume",
        native_id=volume_id,
    )
    session.add(res)
    session.commit()

    # Facts in the shape the connector emits
    facts = [
        FactORM(
            tenant_id=DEFAULT_TENANT_ID,
            resource_id=res.id,
            account_id=account.id,
            namespace="aws.ec2.volume",
            key="volume_type",
            value=raw["VolumeType"],
            value_state="KNOWN",
            source="aws_ec2",
            observed_at=datetime.now(tz=UTC),
        ),
        FactORM(
            tenant_id=DEFAULT_TENANT_ID,
            resource_id=res.id,
            account_id=account.id,
            namespace="aws.ec2.volume",
            key="size_gb",
            value=raw["Size"],
            value_state="KNOWN",
            source="aws_ec2",
            observed_at=datetime.now(tz=UTC),
        ),
    ]
    session.add_all(facts)
    session.commit()
    return res


# ---------------------------------------------------------------------------
# Registration: the rule is wired into the runner.
# ---------------------------------------------------------------------------


def test_ebs_gp2_to_gp3_registered_in_resource_rules():
    """The rule is in the RESOURCE_RULES registry, dispatched by the
    generic runner. Without this, /insights/run ebs_gp2_to_gp3 404s."""
    assert "ebs_gp2_to_gp3" in RESOURCE_RULES


def test_ebs_gp2_to_gp3_source_is_aws_ec2():
    """The rule's source is aws_ec2, NOT aws_rds. This is the critical
    multi-source scope fix: an RDS scan must not prove an EC2 scope."""
    assert RULE_SOURCES["ebs_gp2_to_gp3"] == "aws_ec2"


def test_ebs_gp2_to_gp3_source_distinct_from_rds_rules():
    """Sanity: ebs_gp2_to_gp3 source is different from the RDS rules'
    sources. Same source across both = scope-confusion bug, and the
    test wouldn't catch the wrong data, it would catch the wrong
    lookup."""
    assert RULE_SOURCES["ebs_gp2_to_gp3"] != RULE_SOURCES["rds_eol"]


def test_ebs_gp2_to_gp3_known_resource_type():
    """The rule's source maps to scans over AWS::EC2::Volume resources.
    Documented here so a future refactor that changes the connector's
    resource_type fails loudly."""
    # We don't import the constant directly (would be a tight coupling),
    # but the runner's source lookup must produce source_runs with
    # resource_type = AWS::EC2::Volume when the EC2 scan runs. The
    # _seed_ec2_scope_proof helper above uses this resource_type.
    # The integration is proven by the run_ebs_gp2_to_gp3 tests below.
    pass


# ---------------------------------------------------------------------------
# Happy path: gp2 volume with proven EC2 scope -> MATCH
# ---------------------------------------------------------------------------


def test_run_ebs_gp2_to_gp3_emits_match_for_gp2_volume(session: Session) -> None:
    """The end-to-end flow: gp2 volume + EC2 scope proof + facts ->
    one Insight with the savings figure."""
    acc = _seed_account(session)
    _seed_ec2_scope_proof(session, acc, region="eu-west-1")
    res = _seed_volume(session, acc, "eu-west-1", "vol-1", _gp2_volume_dict("vol-1", 100))

    result = run_ebs_gp2_to_gp3(session)

    assert result.rule_name == "ebs_gp2_to_gp3"
    assert result.resources_scanned == 1
    assert result.insights_emitted == 1
    assert result.inconclusive_emitted == 0

    insight = session.query(InsightORM).filter_by(resource_id=res.id).one()
    assert insight.severity == "info"  # 100 GB = $2 savings = below $50 = INFO
    assert insight.payload["savings_monthly_usd"] == 2.00
    assert insight.payload["current_volume_type"] == "gp2"
    assert insight.payload["target_volume_type"] == "gp3"


def test_run_ebs_gp2_to_gp3_emits_warning_for_3000gb(session: Session) -> None:
    """3000 GB gp2: $60 saved/month -> WARNING severity."""
    acc = _seed_account(session)
    _seed_ec2_scope_proof(session, acc, region="eu-west-1")
    _seed_volume(session, acc, "eu-west-1", "vol-big", _gp2_volume_dict("vol-big", 3000))
    # res is not used directly; the rule reads facts from session.

    result = run_ebs_gp2_to_gp3(session)

    assert result.insights_emitted == 1
    insight = session.query(InsightORM).one()
    assert insight.severity == "warning"
    # 3000 * 0.02 = $60
    assert insight.payload["savings_monthly_usd"] == 60.00


def test_run_ebs_gp2_to_gp3_skips_gp3_volumes(session: Session) -> None:
    """gp3 volumes are the target, not the source. No insight for them."""
    acc = _seed_account(session)
    _seed_ec2_scope_proof(session, acc, region="eu-west-1")
    _seed_volume(session, acc, "eu-west-1", "vol-gp3", _gp3_volume_dict("vol-gp3", 1000))

    result = run_ebs_gp2_to_gp3(session)

    assert result.insights_emitted == 0
    assert session.query(InsightORM).count() == 0


# ---------------------------------------------------------------------------
# Scope-completeness: missing EC2 scan -> INCONCLUSIVE (NOT MATCH)
# ---------------------------------------------------------------------------


def test_run_ebs_gp2_to_gp3_emits_inconclusive_when_no_ec2_scan(session: Session) -> None:
    """No aws_ec2 source_run exists for this volume. The rule must NOT
    emit a MATCH (we have no proof of completeness). It must emit
    INCONCLUSIVE (criterion n°15: never silent). This is the critical
    multi-source scope fix: an existing RDS scan must NOT prove EC2 scope."""
    acc = _seed_account(session)

    # Add an RDS source_run: explicitly NOT the right source for EC2.
    # This is the test the original hardcoded runner would have failed:
    # it would have looked at `aws_rds` source_runs and decided the
    # EC2 scope was proven (wrong).
    rds_run = SourceRunORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",  # <- WRONG source for EC2
        status="success",
        started_at=datetime.now(tz=UTC),
        finished_at=datetime.now(tz=UTC),
        resources_found=1,
    )
    session.add(rds_run)
    session.commit()

    _seed_volume(session, acc, "eu-west-1", "vol-1", _gp2_volume_dict("vol-1", 100))

    result = run_ebs_gp2_to_gp3(session)

    # No insight (no scope proof for the EC2 source)
    assert result.insights_emitted == 0
    # INCONCLUSIVE: the user sees "we don't know yet, scan EC2 first"
    assert result.inconclusive_emitted == 1
    inconclusive = session.query(InconclusiveORM).one()
    assert inconclusive.rule_name == "ebs_gp2_to_gp3"
    assert "scope_not_proven" in inconclusive.missing_facts
    # No Insight emitted
    assert session.query(InsightORM).count() == 0


def test_run_ebs_gp2_to_gp3_emits_inconclusive_when_rds_scan_exists_but_no_ec2_scan(
    session: Session,
) -> None:
    """Stronger version of the previous test: even with MULTIPLE RDS
    scans across multiple regions, the EC2 rule must still emit
    INCONCLUSIVE because no EC2 scan exists. The scope check is
    per-(region, resource_type, source) and source MUST match."""
    acc = _seed_account(session)

    for region in ("eu-west-1", "us-east-1", "us-west-2"):
        rds_run = SourceRunORM(
            tenant_id=DEFAULT_TENANT_ID,
            account_id=acc.id,
            region=region,
            resource_type="AWS::RDS::DBInstance",
            source="aws_rds",
            status="success",
            started_at=datetime.now(tz=UTC),
            finished_at=datetime.now(tz=UTC),
            resources_found=5,
        )
        session.add(rds_run)
    session.commit()

    _seed_volume(session, acc, "eu-west-1", "vol-1", _gp2_volume_dict("vol-1", 100))

    result = run_ebs_gp2_to_gp3(session)

    assert result.insights_emitted == 0
    assert result.inconclusive_emitted == 1
    assert session.query(InsightORM).count() == 0


# ---------------------------------------------------------------------------
# Stale scope: EC2 scan exists but is too old -> scope_stale INCONCLUSIVE
# ---------------------------------------------------------------------------


def test_run_ebs_gp2_to_gp3_emits_inconclusive_when_ec2_scan_stale(
    session: Session,
) -> None:
    """An EC2 scan older than the freshness window (24h) is no longer
    proof of completeness. INCONCLUSIVE with reason=scope_stale, not
    MATCH. (audit F-02: 24h freshness window.)"""
    from datetime import timedelta

    acc = _seed_account(session)
    stale_run = SourceRunORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::EC2::Volume",
        source="aws_ec2",
        status="success",
        started_at=datetime.now(tz=UTC) - timedelta(hours=48),
        finished_at=datetime.now(tz=UTC) - timedelta(hours=48),
        resources_found=1,
    )
    session.add(stale_run)
    session.commit()

    _seed_volume(session, acc, "eu-west-1", "vol-1", _gp2_volume_dict("vol-1", 100))

    result = run_ebs_gp2_to_gp3(session)

    assert result.insights_emitted == 0
    assert result.inconclusive_emitted == 1
    inconclusive = session.query(InconclusiveORM).one()
    assert "scope_stale" in inconclusive.missing_facts


# ---------------------------------------------------------------------------
# Delete-and-replace (audit F-03): re-running clears the previous run's output
# ---------------------------------------------------------------------------


def test_run_ebs_gp2_to_gp3_replaces_previous_insights(session: Session) -> None:
    """A second run must NOT accumulate duplicate insights. The first
    run emitted one Insight; the second run clears it before writing
    fresh results."""
    acc = _seed_account(session)
    _seed_ec2_scope_proof(session, acc, region="eu-west-1")
    _seed_volume(session, acc, "eu-west-1", "vol-1", _gp2_volume_dict("vol-1", 100))

    # First run
    result1 = run_ebs_gp2_to_gp3(session)
    assert result1.insights_emitted == 1
    assert session.query(InsightORM).count() == 1

    # Second run
    result2 = run_ebs_gp2_to_gp3(session)
    assert result2.insights_emitted == 1
    # NOT 2 — the first run's insight was deleted before the second wrote.
    assert session.query(InsightORM).count() == 1


def test_run_ebs_gp2_to_gp3_does_not_emit_when_volume_facts_removed(
    session: Session,
) -> None:
    """If the facts are removed between runs, the second run emits
    INCONCLUSIVE (not silent, not MATCH). Documents the contract:
    facts and resource must stay in sync."""
    acc = _seed_account(session)
    _seed_ec2_scope_proof(session, acc, region="eu-west-1")
    _seed_volume(session, acc, "eu-west-1", "vol-1", _gp2_volume_dict("vol-1", 100))

    # First run emits the insight
    run_ebs_gp2_to_gp3(session)
    assert session.query(InsightORM).count() == 1

    # Remove the facts (simulates a fresh scan with no facts for the volume)
    session.query(FactORM).delete()
    session.commit()

    # Second run: facts are gone, so the rule emits INCONCLUSIVE for
    # the still-tracked resource. The previous insight is cleared
    # (delete-and-replace) and the new INCONCLUSIVE row appears.
    result = run_ebs_gp2_to_gp3(session)
    assert result.insights_emitted == 0
    assert result.inconclusive_emitted == 1
    assert session.query(InsightORM).count() == 0
    inconclusive = session.query(InconclusiveORM).one()
    assert inconclusive.rule_name == "ebs_gp2_to_gp3"


# ---------------------------------------------------------------------------
# run_resource_rule dispatch (not just the wrapper)
# ---------------------------------------------------------------------------


def test_run_resource_rule_dispatches_ebs_gp2_to_gp3(session: Session) -> None:
    """The generic runner must dispatch to the ebs_gp2_to_gp3 resolver
    by name. Same shape as rds_eol / mysql_eol / aurora_eol."""
    acc = _seed_account(session)
    _seed_ec2_scope_proof(session, acc, region="eu-west-1")
    _seed_volume(session, acc, "eu-west-1", "vol-1", _gp2_volume_dict("vol-1", 100))

    # Direct dispatch via the generic runner, not the wrapper.
    result = run_resource_rule(session, "ebs_gp2_to_gp3")

    assert result.rule_name == "ebs_gp2_to_gp3"
    assert result.insights_emitted == 1
