"""Tests for the FOCUS aggregator (pure logic)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from constat_focus.aggregator import aggregate_for_storage
from constat_focus.loader import FocusCharge


def _charge(
    service: str = "AmazonRDS",
    ps: date = date(2026, 7, 1),
    pe: date = date(2026, 7, 31),
    billed: str = "100.00",
    amortized: str = "100.00",
    effective: str = "100.00",
    region: str = "eu-west-1",
    pricing: str = "On-Demand",
) -> FocusCharge:
    return FocusCharge(
        account_id="111111111111",
        account_name="prod",
        service=service,
        region=region,
        pricing_category=pricing,
        period_start=ps,
        period_end=pe,
        billed_cost=Decimal(billed),
        amortized_cost=Decimal(amortized),
        effective_cost=Decimal(effective),
    )


def test_aggregates_by_service_and_period():
    rows = [
        _charge(service="AmazonRDS", billed="100", amortized="100"),
        _charge(service="AmazonRDS", billed="50", amortized="50"),
        _charge(service="AmazonEC2", billed="200", amortized="180"),
    ]
    agg = aggregate_for_storage(rows)

    assert len(agg) == 2
    rds = next(a for a in agg if a.service == "AmazonRDS")
    assert rds.billed_cost == Decimal("150")
    assert rds.amortized_cost == Decimal("150")
    assert rds.charge_count == 2


def test_aggregator_separates_by_period():
    rows = [
        _charge(ps=date(2026, 6, 1), pe=date(2026, 6, 30), billed="100"),
        _charge(ps=date(2026, 7, 1), pe=date(2026, 7, 31), billed="200"),
    ]
    agg = aggregate_for_storage(rows)
    assert len(agg) == 2


def test_aggregator_picks_mode_for_region():
    rows = [
        _charge(region="eu-west-1"),
        _charge(region="eu-west-1"),
        _charge(region="us-east-1"),
    ]
    agg = aggregate_for_storage(rows)
    assert len(agg) == 1
    assert agg[0].region == "eu-west-1"


def test_aggregator_picks_mode_for_pricing_category():
    rows = [
        _charge(pricing="On-Demand"),
        _charge(pricing="Reservation"),
        _charge(pricing="On-Demand"),
    ]
    agg = aggregate_for_storage(rows)
    assert agg[0].pricing_category == "On-Demand"


def test_aggregator_handles_all_null_region_and_pricing():
    rows = [
        FocusCharge(
            account_id="x",
            account_name="x",
            service="AmazonRDS",
            region=None,
            pricing_category=None,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            billed_cost=Decimal("10"),
            amortized_cost=Decimal("10"),
            effective_cost=Decimal("10"),
        )
    ]
    agg = aggregate_for_storage(rows)
    assert agg[0].region is None
    assert agg[0].pricing_category is None
    assert agg[0].charge_count == 1
