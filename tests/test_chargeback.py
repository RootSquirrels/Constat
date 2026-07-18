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
    tags: list[dict[str, str]] | None = None,
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
        tags=tags if tags is not None else [],
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


def test_build_insights_caps_large_drift_at_info():
    # Audit F-13: amortized-vs-billed drift is normal RI mechanics — no
    # severity escalation, even for large drift. Magnitude stays in payload.
    charges = [_charge(billed="1000", amortized="1120")]
    insights = build_insights(aggregate(charges))

    assert len(insights) == 1
    assert insights[0].severity == Severity.INFO
    assert insights[0].payload["drift_amortized_minus_billed_usd"] == 120.0


def test_build_insights_caps_critical_sized_drift_at_info():
    charges = [_charge(billed="1000", amortized="2500")]
    insights = build_insights(aggregate(charges))

    assert len(insights) == 1
    assert insights[0].severity == Severity.INFO


def test_build_insights_emits_info_for_small_drift():
    charges = [_charge(billed="100", amortized="110")]
    insights = build_insights(aggregate(charges))

    assert insights[0].severity == Severity.INFO


# ---------------------------------------------------------------------------
# Tag-based aggregation
# ---------------------------------------------------------------------------


from constat_chargeback.resolver import (  # noqa: E402
    UNTAGGED,
    aggregate_by_tag,
)


def test_aggregate_by_tag_splits_cost_evenly_across_matching_values():
    """One (account, service, period) row with two tag values for the key:
    the cost is split evenly (1/N) across the values."""
    # Single charge with cost 200 and two unique tag dicts.
    # Cost should split: web=100, api=100.
    charges = [
        _charge(
            billed="200", amortized="200", tags=[{"Application": "web"}, {"Application": "api"}]
        )
    ]
    agg = aggregate_by_tag(charges, tag_key="Application")

    assert len(agg) == 2
    by_value = {a.tag_value: a for a in agg}
    assert set(by_value.keys()) == {"web", "api"}
    assert by_value["web"].billed_cost == Decimal("100")
    assert by_value["api"].billed_cost == Decimal("100")
    assert by_value["web"].amortized_cost == Decimal("100")
    assert by_value["api"].amortized_cost == Decimal("100")


def test_aggregate_by_tag_untagged_bucket_for_missing_key():
    """When a row's tag dicts don't contain the requested key, the cost
    goes to the UNTAGGED bucket with full cost (no split)."""
    charges = [
        _charge(billed="100", tags=[{"CostCenter": "42"}]),  # no Application
    ]
    agg = aggregate_by_tag(charges, tag_key="Application")

    assert len(agg) == 1
    assert agg[0].tag_value == UNTAGGED
    assert agg[0].billed_cost == Decimal("100")


def test_aggregate_by_tag_handles_no_tags_at_all():
    """A row with empty tags list -> UNTAGGED with full cost."""
    charges = [_charge(billed="50", tags=[])]
    agg = aggregate_by_tag(charges, tag_key="Application")
    assert len(agg) == 1
    assert agg[0].tag_value == UNTAGGED
    assert agg[0].billed_cost == Decimal("50")


def test_aggregate_by_tag_groups_by_account_service_period_tag():
    """Two different (account, service, period) rows, each with one tag value
    -> 2 buckets, one per row."""
    charges = [
        _charge(
            account="111",
            service="AmazonRDS",
            billed="100",
            tags=[{"Application": "web"}],
        ),
        _charge(
            account="111",
            service="AmazonEC2",
            billed="200",
            tags=[{"Application": "web"}],
        ),
    ]
    agg = aggregate_by_tag(charges, tag_key="Application")
    assert len(agg) == 2
    by_service = {a.service: a for a in agg}
    assert by_service["AmazonRDS"].billed_cost == Decimal("100")
    assert by_service["AmazonEC2"].billed_cost == Decimal("200")
    assert all(a.tag_value == "web" for a in agg)
    assert all(a.tag_key == "Application" for a in agg)


def test_aggregate_by_tag_rejects_empty_key():
    with __import__("pytest").raises(ValueError, match="tag_key must be a non-empty string"):
        aggregate_by_tag([], tag_key="")


def test_build_insights_includes_tag_key_and_value_in_title_and_payload():
    charges = [_charge(billed="100", tags=[{"Application": "web"}])]
    agg = aggregate_by_tag(charges, tag_key="Application")
    insights = build_insights(agg)

    assert len(insights) == 1
    assert "[Application=web]" in insights[0].title
    assert insights[0].payload["tag_key"] == "Application"
    assert insights[0].payload["tag_value"] == "web"


def test_build_insights_untagged_title():
    charges = [_charge(billed="50", tags=[])]
    agg = aggregate_by_tag(charges, tag_key="Application")
    insights = build_insights(agg)
    assert "__untagged__" in insights[0].title
    assert insights[0].payload["tag_value"] == "__untagged__"
