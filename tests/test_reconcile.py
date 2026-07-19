"""Tests for ESTIMATED -> ACTUAL reconciliation against FOCUS (roadmap 2.3).

An estimate is invoice-confirmed when FOCUS has cost lines for the SAME
resource (insights.resource_id -> resources.native_id == focus_charges.resource_id)
from the rule's own service, over the latest available period. The insight's
payload then flips to value_basis ACTUAL with the invoice-backed amount.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from constat_api.insights.reconcile import reconcile_with_focus
from constat_api.insights.runner import run_resource_rule
from constat_api.orm import AccountORM, InsightORM, ResourceORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import focus_charges as focus_charges_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_core.models import Fact, ValueState
from constat_core.monetary import (
    MonetaryKind,
    ValueBasis,
    monetary_kind,
    monthly_cost_and_basis,
)
from constat_focus.aggregator import AggregatedFocusCharge
from sqlalchemy.orm import Session

RDS_SERVICE = "Amazon Relational Database Service"
EC2_SERVICE = "Amazon Elastic Compute Cloud - Compute"
NATIVE_ID = "arn:aws:rds:eu-west-1:111111111111:db:pg11"

# PG11, 2 vCPU, 2026-07-18 -> year-3 tier: 2 x $0.20 x 730h = $292.00.
ESTIMATE = 292.0


def _bootstrap_pg11(session: Session) -> tuple[AccountORM, ResourceORM]:
    """Account + PG11 resource + successful scope proof + facts (2 vCPU)."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id=NATIVE_ID,
    )
    session.add(resource)
    session.commit()

    run = source_runs_repo.start_run(
        session,
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    source_runs_repo.finish_run(session, run, status="success", resources_found=1)
    session.commit()

    facts_repo.upsert_facts(
        session,
        [
            Fact(
                resource_id=resource.id,
                account_id=str(acc.id),
                namespace="aws.rds",
                key=key,
                value=value,
                value_state=ValueState.KNOWN,
                source="aws_rds",
                observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            )
            for key, value in [
                ("engine", "postgres"),
                ("engine_version", "11.22"),
                ("instance_class", "db.m5.xlarge"),
                ("vcpu", 2),
                ("region", "us-east-1"),
            ]
        ],
        source_run_id=run.id,
    )
    session.commit()
    return acc, resource


def _add_focus(
    session: Session,
    account_id,
    *,
    resource_id: str | None = NATIVE_ID,
    service: str = RDS_SERVICE,
    amortized: str = "300.00",
    period_start: date = date(2026, 6, 1),
    period_end: date = date(2026, 6, 30),
) -> None:
    agg = AggregatedFocusCharge(
        service=service,
        period_start=period_start,
        period_end=period_end,
        billed_cost=Decimal(amortized),
        amortized_cost=Decimal(amortized),
        charge_count=1,
        region="eu-west-1",
        pricing_category="On-Demand",
        resource_id=resource_id,
        sub_account_id=None,
        tags=[],
        per_row_tag_dicts=[],
    )
    focus_charges_repo.upsert_aggregated(session, account_id, [agg])
    session.commit()


def _run(session: Session) -> InsightORM:
    result = run_resource_rule(session, "rds_eol", today=date(2026, 7, 18))
    assert result.insights_emitted == 1
    assert result.errors == []
    return session.query(InsightORM).one()


# ---- Confirmation paths -----------------------------------------------------


def test_focus_line_confirms_estimate(session: Session) -> None:
    """FOCUS line for the resource (June, $300 over 30 days) -> ACTUAL $300."""
    acc, _resource = _bootstrap_pg11(session)
    _add_focus(session, acc.id, amortized="300.00")

    insight = _run(session)

    payload = insight.payload
    assert payload["focus_confirmed"] is True
    assert payload["value_basis"] == "ACTUAL"
    assert payload["focus_actual_monthly_usd"] == pytest.approx(300.0)
    assert payload["focus_period"] == "2026-06-01..2026-06-30"
    # The catalog estimate is kept in the payload (evidence trail).
    assert payload["extended_support_monthly_usd"] == ESTIMATE


def test_no_focus_line_stays_estimated(session: Session) -> None:
    _bootstrap_pg11(session)
    insight = _run(session)
    assert "focus_confirmed" not in insight.payload
    cost, basis = monthly_cost_and_basis("rds_eol", insight.payload)
    assert cost == ESTIMATE
    assert basis == ValueBasis.ESTIMATED.value


def test_wrong_service_does_not_confirm(session: Session) -> None:
    """A rule only trusts FOCUS lines from its own ServiceName."""
    acc, _resource = _bootstrap_pg11(session)
    _add_focus(session, acc.id, service=EC2_SERVICE)

    insight = _run(session)
    assert "focus_confirmed" not in insight.payload


def test_other_resource_does_not_confirm(session: Session) -> None:
    acc, _resource = _bootstrap_pg11(session)
    _add_focus(session, acc.id, resource_id="arn:aws:rds:eu-west-1:111111111111:db:other")

    insight = _run(session)
    assert "focus_confirmed" not in insight.payload


def test_latest_period_wins(session: Session) -> None:
    """Two periods: the latest one (June $300) is the actual, May is ignored."""
    acc, _resource = _bootstrap_pg11(session)
    _add_focus(
        session,
        acc.id,
        amortized="100.00",
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
    )
    _add_focus(session, acc.id, amortized="300.00")

    insight = _run(session)
    assert insight.payload["focus_actual_monthly_usd"] == pytest.approx(300.0)
    assert insight.payload["focus_period"] == "2026-06-01..2026-06-30"


def test_monthly_normalization_prorates_short_periods(session: Session) -> None:
    """$100 over a 10-day period normalizes to $300/month (inclusive days)."""
    acc, _resource = _bootstrap_pg11(session)
    _add_focus(
        session,
        acc.id,
        amortized="100.00",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 10),
    )

    insight = _run(session)
    assert insight.payload["focus_actual_monthly_usd"] == pytest.approx(300.0)


def test_lines_in_same_period_are_summed(session: Session) -> None:
    """Several FOCUS lines for the resource sharing the latest period_end
    are summed (e.g. instance + storage split across two lines)."""
    acc, _resource = _bootstrap_pg11(session)
    # Two distinct upsert keys (period_start differs), same service,
    # same period_end -> both belong to the latest period.
    _add_focus(
        session,
        acc.id,
        amortized="100.00",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
    )
    _add_focus(
        session,
        acc.id,
        amortized="200.00",
        period_start=date(2026, 6, 15),
        period_end=date(2026, 6, 30),
    )

    insight = _run(session)
    # Sum $300 over the full window 06-01..06-30 (30 days) -> $300/month.
    assert insight.payload["focus_actual_monthly_usd"] == pytest.approx(300.0)


# ---- Extraction semantics (monetary registry) --------------------------------


def test_extraction_prefers_actual_and_kind_never_changes(session: Session) -> None:
    """ADR-13 note: basis becomes per-insight ACTUAL; kind stays AVOIDABLE_SAVING."""
    payload = {
        "extended_support_monthly_usd": ESTIMATE,
        "focus_confirmed": True,
        "focus_actual_monthly_usd": 300.0,
    }
    cost, basis = monthly_cost_and_basis("rds_eol", payload)
    assert cost == 300.0
    assert basis == ValueBasis.ACTUAL.value
    assert monetary_kind("rds_eol") == MonetaryKind.AVOIDABLE_SAVING


def test_confirmed_flag_without_numeric_amount_falls_back() -> None:
    """focus_confirmed with a garbage amount must not hide the estimate."""
    payload = {
        "extended_support_monthly_usd": ESTIMATE,
        "focus_confirmed": True,
        "focus_actual_monthly_usd": "300",
    }
    cost, basis = monthly_cost_and_basis("rds_eol", payload)
    assert cost == ESTIMATE
    assert basis == ValueBasis.ESTIMATED.value


def test_chargeback_is_not_reconciled(session: Session) -> None:
    """chargeback is ACTUAL by construction — the reconcile pass is a no-op."""
    assert reconcile_with_focus(session, "chargeback") == 0
