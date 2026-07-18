"""Tests for the chargeback insight (FOCUS aggregation)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from constat_chargeback.resolver import aggregate, build_insights
from constat_core.models import Severity
from constat_focus.loader import FocusCharge


def _charge(
    account: str = "111111111111",
    service: str = "AmazonRDS",
    billed: str = "100.00",
    amortized: str = "100.00",
    pricing: str = "On-Demand",
    region: str = "eu-west-1",
    tags: dict[str, str] | None = None,
) -> FocusCharge:
    return FocusCharge(
        account_id=account,
        account_name=f"acct-{account}",
        service=service,
        region=region,
        pricing_category=pricing,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal(billed),
        amortized_cost=Decimal(amortized),
        resource_id=None,
        sub_account_id=None,
        tags=tags if tags is not None else {},
    )


def test_aggregate_groups_by_account_and_service():
    charges = [
        _charge(service="AmazonRDS", billed="100", amortized="100"),
        _charge(service="AmazonRDS", billed="50", amortized="50"),
        _charge(service="AmazonEC2", billed="200", amortized="180"),
    ]
    agg = aggregate(charges)

    assert len(agg) == 2
    rds = next(a for a in agg if a.service == "AmazonRDS")
    assert rds.billed_cost == Decimal("150")
    assert rds.amortized_cost == Decimal("150")
    assert rds.charge_count == 2


def test_build_insights_emits_warning_for_100_drift():
    # Drift of +120 USD amortized over billed -> WARNING (>= 100, < 1000)
    charges = [_charge(billed="1000", amortized="1120")]
    insights = build_insights(aggregate(charges))

    assert len(insights) == 1
    assert insights[0].severity == Severity.WARNING
    assert insights[0].payload["drift_amortized_minus_billed_usd"] == 120.0


def test_build_insights_emits_critical_for_1000_drift():
    charges = [_charge(billed="1000", amortized="2500")]
    insights = build_insights(aggregate(charges))

    assert len(insights) == 1
    assert insights[0].severity == Severity.CRITICAL


def test_build_insights_emits_info_for_small_drift():
    charges = [_charge(billed="100", amortized="110")]
    insights = build_insights(aggregate(charges))

    assert insights[0].severity == Severity.INFO
