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
    billing_currency: str = "USD",
) -> FocusCharge:
    billed_d = Decimal(billed)
    amortized_d = Decimal(amortized)
    # Migration 0020: tags and per_row_costs are parallel lists. An
    # untagged row has tags=[{}] so the resolver attributes the cost
    # to UNTAGGED via "tag_key not in {}". When the test passes N
    # tag dicts, we default to N rows of equal cost (billed / N each).
    # Tests that need heterogeneous per-row costs should build
    # FocusCharge directly.
    if not tags:
        tags = [{}]
    n = len(tags)
    per_billed = billed_d / Decimal(n)
    per_amortized = amortized_d / Decimal(n)
    per_row_costs = [(per_billed, per_amortized) for _ in range(n)]
    return FocusCharge(
        account_id=account,
        account_name=f"acct-{account}",
        service=service,
        region=region,
        pricing_category=pricing,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=billed_d,
        amortized_cost=amortized_d,
        resource_id=None,
        sub_account_id=None,
        tags=tags,
        billing_currency=billing_currency,
        per_row_costs=per_row_costs,
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


# ---------------------------------------------------------------------------
# Migration 0020: cost-weighted tag attribution (audit committee fix)
# ---------------------------------------------------------------------------


def test_aggregate_by_tag_attributes_by_cost_not_count():
    """The audit committee's deal-breaker: tag attribution must
    follow the cost, not the row count. 1 web row of 3 EUR + 1 api
    row of 97 EUR (total 100 EUR) must give web=3, api=97 — not
    50/50 (V1 even split) and not the row-count-weighted equivalent
    (also 50/50 when row counts are equal).

    Per-row cost attribution (migration 0020): the resolver reads
    (tag_dict, (billed, amortized)) pairs from the FocusCharge and
    attributes each row's own cost to its tag value. A 3 EUR web
    row and a 97 EUR api row give web=3, api=97, exactly what
    the audit committee asked for.
    """
    web_charge = FocusCharge(
        account_id="111",
        account_name="acct-111",
        service="AmazonRDS",
        region="eu-west-1",
        pricing_category="On-Demand",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal("3"),
        amortized_cost=Decimal("3"),
        resource_id=None,
        sub_account_id=None,
        tags=[{"Application": "web"}],
        billing_currency="EUR",
        per_row_costs=[(Decimal("3"), Decimal("3"))],
    )
    api_charge = FocusCharge(
        account_id="111",
        account_name="acct-111",
        service="AmazonRDS",
        region="eu-west-1",
        pricing_category="On-Demand",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal("97"),
        amortized_cost=Decimal("97"),
        resource_id=None,
        sub_account_id=None,
        tags=[{"Application": "api"}],
        billing_currency="EUR",
        per_row_costs=[(Decimal("97"), Decimal("97"))],
    )

    # Use aggregate_by_tag directly (no FOCUS storage round-trip).
    # The two charges have the same (account, service, period) so
    # aggregate_by_period would collapse them into 1 bucket; we
    # test the per-row attribution at the resolver level.
    agg = aggregate_by_tag([web_charge, api_charge], tag_key="Application")

    by_value = {a.tag_value: a.billed_cost for a in agg}
    # 3 EUR web + 97 EUR api -> 3% / 97% (not 50% / 50%, not 25% / 75%).
    assert by_value == {"web": Decimal("3"), "api": Decimal("97")}
