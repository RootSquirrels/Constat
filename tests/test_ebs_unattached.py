"""Tests for the ebs_unattached insight rule + runner wiring.

Coverage target: 13 tests. The 5 ebs_gp2_to_gp3 test files (62 tests)
over-tested this kind of rule; here we focus on what proves the contract:
- 3-state contract (MATCH / NO_MATCH / INCONCLUSIVE)
- The key behavioral difference from gp2_to_gp3 (state-driven, not type-driven)
- The catalog version stamp (defensibility for sales)
- The runner integration (registration + source + end-to-end)
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from constat_api.insights.runner import (
    RESOURCE_RULES,
    RULE_SOURCES,
    run_ebs_unattached,
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
from constat_core.catalog.ebs import EBS_CATALOG_VERSION
from constat_core.models import Fact, Severity, ValueState
from constat_ebs_unattached.resolver import RULE_NAME, evaluate
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fact(key: str, value, *, value_state: ValueState = ValueState.KNOWN) -> Fact:
    return Fact(
        resource_id=uuid4(),
        account_id="111111111111",
        namespace="aws.ec2.volume",
        key=key,
        value=value,
        value_state=value_state,
        source="aws_ec2",
        observed_at=datetime.now(tz=UTC),
    )


def _available_facts(
    *, size_gb: int = 100, volume_type: str = "gp2", state: str = "available"
) -> list[Fact]:
    """Minimal fact set for one EBS volume in a given state."""
    return [
        _fact("state", state),
        _fact("size_gb", size_gb),
        _fact("volume_type", volume_type),
    ]


# ---------------------------------------------------------------------------
# Rule: MATCH
# ---------------------------------------------------------------------------


def test_available_gp2_volume_emits_match() -> None:
    """The headline case: an unattached gp2 volume = monthly waste."""
    result = evaluate(uuid4(), _available_facts(size_gb=100, volume_type="gp2"))

    assert result.is_conclusive
    assert result.has_gap
    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight.rule_name == RULE_NAME
    # 100 GB * $0.10/GB-month = $10/month wasted
    assert insight.payload["monthly_waste_usd"] == 10.00
    assert insight.payload["volume_type"] == "gp2"
    assert insight.payload["state"] == "available"
    assert insight.payload["value_basis"] == "ESTIMATED"
    assert insight.payload["catalog_version"] == EBS_CATALOG_VERSION


def test_severity_thresholds_match_gp2_to_gp3() -> None:
    """Same severity scale as gp2_to_gp3 for dashboard consistency.
    >= $500 = CRITICAL, >= $50 = WARNING, else INFO."""
    # 500 GB gp3 = 500 * 0.08 = $40 -> INFO
    r1 = evaluate(uuid4(), _available_facts(size_gb=500, volume_type="gp3"))
    assert r1.insights[0].severity == Severity.INFO
    # 2500 GB gp2 = 2500 * 0.10 = $250 -> WARNING
    r2 = evaluate(uuid4(), _available_facts(size_gb=2500, volume_type="gp2"))
    assert r2.insights[0].severity == Severity.WARNING
    # 25000 GB gp2 = $2500 -> CRITICAL
    r3 = evaluate(uuid4(), _available_facts(size_gb=25000, volume_type="gp2"))
    assert r3.insights[0].severity == Severity.CRITICAL


# ---------------------------------------------------------------------------
# Rule: NO_MATCH
# ---------------------------------------------------------------------------


def test_in_use_volume_emits_nothing() -> None:
    """state=in-use means attached. NO_MATCH (working as intended)."""
    result = evaluate(uuid4(), _available_facts(state="in-use"))
    assert result.is_conclusive
    assert not result.has_gap
    assert result.insights == []


@pytest.mark.parametrize("state", ["creating", "deleting", "deleted", "error"])
def test_transient_or_dead_states_emit_nothing(state: str) -> None:
    """Any state other than 'available' is NO_MATCH, not INCONCLUSIVE.
    We have the state fact, we just don't classify it as a waste case.
    Error state: the operator should investigate, but the volume may
    still cost money — that's surfaced in the inventory view, not here."""
    result = evaluate(uuid4(), _available_facts(state=state))
    assert result.is_conclusive
    assert not result.has_gap
    assert result.inconclusive_reasons == []


# ---------------------------------------------------------------------------
# Rule: INCONCLUSIVE
# ---------------------------------------------------------------------------


def test_missing_state_emits_inconclusive() -> None:
    """Unknown state = we can't decide if it's unattached. INCONCLUSIVE."""
    facts = [
        _fact("state", None, value_state=ValueState.UNKNOWN),
        _fact("size_gb", 100),
        _fact("volume_type", "gp2"),
    ]
    result = evaluate(uuid4(), facts)
    assert not result.is_conclusive
    assert "aws.ec2.volume.state" in result.inconclusive_reasons


def test_missing_size_emits_inconclusive() -> None:
    facts = [
        _fact("state", "available"),
        _fact("size_gb", None, value_state=ValueState.UNKNOWN),
        _fact("volume_type", "gp2"),
    ]
    result = evaluate(uuid4(), facts)
    assert "aws.ec2.volume.size_gb" in result.inconclusive_reasons


def test_missing_type_emits_inconclusive() -> None:
    facts = [
        _fact("state", "available"),
        _fact("size_gb", 100),
        _fact("volume_type", None, value_state=ValueState.UNKNOWN),
    ]
    result = evaluate(uuid4(), facts)
    assert "aws.ec2.volume.volume_type" in result.inconclusive_reasons


def test_unknown_volume_type_emits_inconclusive() -> None:
    """A volume type not in the catalog (e.g. a future io3) is
    INCONCLUSIVE, not a free $0/GB-month surprise. Same defensive
    pattern as gp2_to_gp3."""
    facts = _available_facts(volume_type="io99")
    result = evaluate(uuid4(), facts)
    assert not result.is_conclusive
    assert "catalog.volume_type_price_missing" in result.inconclusive_reasons


# ---------------------------------------------------------------------------
# Runner: registration + source + end-to-end
# ---------------------------------------------------------------------------


def test_ebs_unattached_registered_in_resource_rules() -> None:
    """The rule is in the registry — the generic runner dispatches to it."""
    assert "ebs_unattached" in RESOURCE_RULES


def test_ebs_unattached_source_is_aws_ec2() -> None:
    """Scope-completeness uses aws_ec2 source, NOT aws_rds.
    A successful RDS scan must not prove an EBS volume's scope."""
    assert RULE_SOURCES["ebs_unattached"] == "aws_ec2"


def _seed_account(session: Session) -> AccountORM:
    acc = AccountORM(external_id="111111111111", name="prod")
    session.add(acc)
    session.commit()
    return acc


def _seed_ec2_scope_proof(session: Session, account: AccountORM, region: str = "eu-west-1") -> None:
    run = SourceRunORM(
        tenant_id=DEFAULT_TENANT_ID,
        account_id=account.id,
        region=region,
        resource_type="AWS::EC2::Volume",
        source="aws_ec2",
        status="success",
        started_at=datetime.now(tz=UTC),
        finished_at=datetime.now(tz=UTC),
        resources_found=1,
    )
    session.add(run)
    session.commit()


def _seed_volume_with_facts(
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
        resource_type="AWS::EC2::Volume",
        native_id=native_id,
    )
    session.add(res)
    session.commit()
    for key, value in facts_by_key.items():
        fact = FactORM(
            tenant_id=DEFAULT_TENANT_ID,
            resource_id=res.id,
            account_id=account.id,
            namespace="aws.ec2.volume",
            key=key,
            value=value,
            value_state="KNOWN",
            source="aws_ec2",
            observed_at=datetime.now(tz=UTC),
        )
        session.add(fact)
    session.commit()
    return res


def test_run_ebs_unattached_emits_insight_for_available_volume(session: Session) -> None:
    """End-to-end: a 1000 GB gp2 available volume emits one insight."""
    acc = _seed_account(session)
    _seed_ec2_scope_proof(session, acc, region="eu-west-1")
    _seed_volume_with_facts(
        session,
        acc,
        "eu-west-1",
        "vol-1",
        {"state": "available", "size_gb": 1000, "volume_type": "gp2"},
    )

    result = run_ebs_unattached(session)

    assert result.rule_name == "ebs_unattached"
    assert result.insights_emitted == 1
    assert result.inconclusive_emitted == 0
    insight = session.query(InsightORM).one()
    # 1000 GB * $0.10 = $100/month -> WARNING
    assert insight.severity == "warning"
    assert insight.payload["monthly_waste_usd"] == 100.00


def test_run_ebs_unattached_emits_inconclusive_without_ec2_scope(session: Session) -> None:
    """No aws_ec2 source_run -> INCONCLUSIVE for every available volume
    (multi-source scope fix: RDS scans don't prove EC2 scope)."""
    acc = _seed_account(session)
    # NO _seed_ec2_scope_proof call
    _seed_volume_with_facts(
        session,
        acc,
        "eu-west-1",
        "vol-1",
        {"state": "available", "size_gb": 100, "volume_type": "gp2"},
    )

    result = run_ebs_unattached(session)

    assert result.insights_emitted == 0
    assert result.inconclusive_emitted == 1
    assert session.query(InconclusiveORM).one().rule_name == "ebs_unattached"
