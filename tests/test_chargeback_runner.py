"""Tests for the chargeback runner (account-scope FOCUS aggregation)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from constat_api.insights.runner import run_chargeback
from constat_api.orm import AccountORM, FocusChargeORM, InsightORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import focus_charges as focus_charges_repo
from constat_api.settings import DEFAULT_TENANT_ID
from sqlalchemy.orm import Session


def _account(session: Session, external_id: str = "111111111111") -> AccountORM:
    acc = accounts_repo.get_or_create(session, external_id)
    return acc


def _add_focus(
    session: Session,
    account_id,
    *,
    service: str = "AmazonRDS",
    billed: str = "100.00",
    amortized: str = "100.00",
    pricing: str = "On-Demand",
    period_start: date = date(2026, 7, 1),
    period_end: date = date(2026, 7, 31),
) -> None:
    focus_charges_repo.upsert_aggregated(
        session,
        account_id,
        [
            FocusChargeORM(  # type: ignore[call-arg]
                tenant_id=DEFAULT_TENANT_ID,
                account_id=account_id,
                service=service,
                period_start=period_start,
                period_end=period_end,
                region="eu-west-1",
                pricing_category=pricing,
                billed_cost=Decimal(billed),
                amortized_cost=Decimal(amortized),
                resource_id=None,
                sub_account_id=None,
                charge_count=1,
            )
        ],
    )
    session.commit()


def test_chargeback_runner_emits_no_insights_when_no_focus(session: Session) -> None:
    """No FOCUS data = no insights, no errors. Empty is a valid state."""
    _account(session, "111111111111")
    result = run_chargeback(session)
    assert result.resources_scanned == 0
    assert result.insights_emitted == 0
    assert result.errors == []


def test_chargeback_runner_emits_one_insight_per_service(session: Session) -> None:
    """One (account, service) tuple -> one insight."""
    acc = _account(session, "111111111111")
    _add_focus(session, acc.id, service="AmazonRDS", billed="100", amortized="100")
    _add_focus(session, acc.id, service="AmazonEC2", billed="200", amortized="180")

    result = run_chargeback(session)
    assert result.resources_scanned == 1
    assert result.insights_emitted == 2
    assert result.errors == []

    rows = session.query(InsightORM).all()
    services = {r.payload["service"] for r in rows}
    assert services == {"AmazonRDS", "AmazonEC2"}


def test_chargeback_runner_aggregates_across_periods(session: Session) -> None:
    """V1: aggregate across all periods per (account, service)."""
    acc = _account(session, "111111111111")
    _add_focus(
        session,
        acc.id,
        service="AmazonRDS",
        billed="100",
        amortized="100",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
    )
    _add_focus(
        session,
        acc.id,
        service="AmazonRDS",
        billed="150",
        amortized="150",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
    )

    result = run_chargeback(session)
    assert result.insights_emitted == 1  # one tuple, two periods aggregated
    insight = session.query(InsightORM).one()
    assert insight.payload["service"] == "AmazonRDS"
    assert insight.payload["billed_cost_usd"] == 250.0
    assert insight.payload["amortized_cost_usd"] == 250.0
    assert insight.payload["period_label"] == "all-time"


def test_chargeback_runner_emits_drift_with_correct_severity(session: Session) -> None:
    """Drift > 1000 USD -> CRITICAL severity."""
    acc = _account(session, "111111111111")
    _add_focus(session, acc.id, service="AmazonRDS", billed="1000", amortized="2500")
    run_chargeback(session)
    insight = session.query(InsightORM).one()
    assert insight.severity == "critical"
    assert insight.payload["drift_amortized_minus_billed_usd"] == 1500.0


def test_chargeback_runner_handles_multiple_accounts(session: Session) -> None:
    """Each account is processed independently."""
    _account(session, "111111111111")
    _account(session, "222222222222")
    _add_focus(
        session,
        session.query(AccountORM).filter_by(external_id="111111111111").one().id,
        service="AmazonRDS",
    )
    _add_focus(
        session,
        session.query(AccountORM).filter_by(external_id="222222222222").one().id,
        service="AmazonEC2",
    )

    result = run_chargeback(session)
    assert result.resources_scanned == 2
    assert result.insights_emitted == 2


def test_chargeback_runner_records_insight_run_metadata(session: Session) -> None:
    acc = _account(session, "111111111111")
    _add_focus(session, acc.id, service="AmazonRDS", billed="100", amortized="100")
    result = run_chargeback(session, period_label="2026-07")

    from constat_api.orm import InsightRunORM

    run = session.query(InsightRunORM).one()
    assert run.rule_name == "chargeback"
    assert run.status == "success"
    assert run.resources_scanned == 1
    assert run.insights_emitted == 1
    assert result.period_label == "2026-07"
