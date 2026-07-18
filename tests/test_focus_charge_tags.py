"""V2 tests for the per-row FOCUS Tags storage (P3 item 11 fix).

Covers:
- Repository: per-row tags are written, deleted-on-update, queryable
- Runner: per-row data drives proportional cost attribution
- End-to-end: FOCUS file with heterogeneous tags ingests and
  re-aggregates by tag correctly (proportional, not even split)
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from constat_api.cli.focus import ingest_focus_file
from constat_api.insights.runner import run_chargeback
from constat_api.orm import FocusChargeORM, FocusChargeTagORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import focus_charges as focus_charges_repo
from constat_focus.aggregator import AggregatedFocusCharge, aggregate_for_storage
from constat_focus.loader import FocusCharge
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Repository: per-row tags are written
# ---------------------------------------------------------------------------


def test_upsert_writes_per_row_tags(session: Session) -> None:
    """A focus_charge with N per-row tag dicts produces N focus_charge_tags rows."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    agg = AggregatedFocusCharge(
        service="AmazonRDS",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal("200"),
        amortized_cost=Decimal("200"),
        charge_count=3,
        region="eu-west-1",
        pricing_category="On-Demand",
        resource_id=None,
        sub_account_id=None,
        tags=[{"Application": "web"}, {"Application": "api"}],
        per_row_tag_dicts=[
            {"Application": "web"},
            {"Application": "web"},
            {"Application": "api"},
        ],
    )
    inserted, _ = focus_charges_repo.upsert_aggregated(session, acc.id, [agg])
    assert inserted == 1

    fc = session.query(FocusChargeORM).one()
    tag_rows = (
        session.query(FocusChargeTagORM).filter(FocusChargeTagORM.focus_charge_id == fc.id).all()
    )
    # 3 input rows -> 3 focus_charge_tags rows
    assert len(tag_rows) == 3
    # Each row is one input row's tag dict
    keys_values = sorted((r.key, r.value) for r in tag_rows)
    assert keys_values == [
        ("Application", "api"),
        ("Application", "web"),
        ("Application", "web"),
    ]


def test_upsert_deletes_old_tags_on_re_ingest(session: Session) -> None:
    """Re-ingesting the same (account, service, period) replaces the tag
    rows (the new ingest is the source of truth, not the old one)."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    agg_v1 = AggregatedFocusCharge(
        service="AmazonRDS",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal("100"),
        amortized_cost=Decimal("100"),
        charge_count=2,
        region="eu-west-1",
        pricing_category="On-Demand",
        resource_id=None,
        sub_account_id=None,
        tags=[{"Application": "web"}],
        per_row_tag_dicts=[{"Application": "web"}, {"Application": "web"}],
    )
    focus_charges_repo.upsert_aggregated(session, acc.id, [agg_v1])
    session.commit()

    fc = session.query(FocusChargeORM).one()
    old_tags = (
        session.query(FocusChargeTagORM).filter(FocusChargeTagORM.focus_charge_id == fc.id).all()
    )
    assert len(old_tags) == 2
    assert all(t.value == "web" for t in old_tags)

    # Re-ingest with a different tag distribution
    agg_v2 = AggregatedFocusCharge(
        service="AmazonRDS",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal("300"),
        amortized_cost=Decimal("300"),
        charge_count=3,
        region="eu-west-1",
        pricing_category="On-Demand",
        resource_id=None,
        sub_account_id=None,
        tags=[{"Application": "api"}],
        per_row_tag_dicts=[
            {"Application": "api"},
            {"Application": "api"},
            {"Application": "api"},
        ],
    )
    focus_charges_repo.upsert_aggregated(session, acc.id, [agg_v2])
    session.commit()

    new_tags = (
        session.query(FocusChargeTagORM).filter(FocusChargeTagORM.focus_charge_id == fc.id).all()
    )
    # Old web tags are gone, new api tags are in.
    assert len(new_tags) == 3
    assert all(t.value == "api" for t in new_tags)


def test_upsert_with_empty_per_row_tags_writes_nothing(session: Session) -> None:
    """A focus_charge with no tags still gets a row in focus_charges,
    but no rows in focus_charge_tags."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    agg = AggregatedFocusCharge(
        service="AmazonRDS",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal("100"),
        amortized_cost=Decimal("100"),
        charge_count=1,
        region="eu-west-1",
        pricing_category="On-Demand",
        resource_id=None,
        sub_account_id=None,
        tags=[],
        per_row_tag_dicts=[],
    )
    focus_charges_repo.upsert_aggregated(session, acc.id, [agg])
    assert session.query(FocusChargeTagORM).count() == 0


# ---------------------------------------------------------------------------
# Aggregator: per_row_tag_dicts is populated correctly
# ---------------------------------------------------------------------------


def test_aggregator_populates_per_row_tag_dicts() -> None:
    """When 3 FOCUS rows aggregate, per_row_tag_dicts has 3 elements
    (one per row, with multiplicity preserved)."""
    rows = [
        FocusCharge(
            account_id="111",
            account_name="p",
            service="AmazonRDS",
            region="eu-west-1",
            pricing_category="On-Demand",
            resource_id="arn:rds:1",
            sub_account_id="222",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            billed_cost=Decimal("10"),
            amortized_cost=Decimal("10"),
            tags=[{"Application": "web"}],
        ),
        FocusCharge(
            account_id="111",
            account_name="p",
            service="AmazonRDS",
            region="eu-west-1",
            pricing_category="On-Demand",
            resource_id="arn:rds:2",
            sub_account_id="222",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            billed_cost=Decimal("20"),
            amortized_cost=Decimal("20"),
            tags=[{"Application": "web"}],
        ),
        FocusCharge(
            account_id="111",
            account_name="p",
            service="AmazonRDS",
            region="eu-west-1",
            pricing_category="On-Demand",
            resource_id="arn:rds:3",
            sub_account_id="222",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            billed_cost=Decimal("30"),
            amortized_cost=Decimal("30"),
            tags=[{"Application": "api"}],
        ),
    ]
    agg_list = aggregate_for_storage(rows)
    assert len(agg_list) == 1
    agg = agg_list[0]
    # tags is the unique deduped list (for the JSONB column)
    assert sorted(tuple(sorted(d.items())) for d in agg.tags) == sorted(
        tuple(sorted(d.items())) for d in [{"Application": "web"}, {"Application": "api"}]
    )
    # per_row_tag_dicts preserves multiplicity: 3 elements, 2 web + 1 api
    assert len(agg.per_row_tag_dicts) == 3
    counts = {d["Application"]: 0 for d in agg.per_row_tag_dicts}
    for d in agg.per_row_tag_dicts:
        counts[d["Application"]] += 1
    assert counts == {"web": 2, "api": 1}


# ---------------------------------------------------------------------------
# Runner: proportional cost attribution (the V2 fix)
# ---------------------------------------------------------------------------


def test_runner_proportional_split_with_heterogeneous_tags(
    session: Session,
) -> None:
    """The V2 runner attributes cost proportionally to per-row tag counts.
    3 input rows for Application=web (cost 60) and 1 for Application=api
    (cost 40) gives web=45, api=15. The V1 even split would have given
    30/30."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    agg = AggregatedFocusCharge(
        service="AmazonRDS",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal("60"),
        amortized_cost=Decimal("60"),
        charge_count=4,
        region="eu-west-1",
        pricing_category="On-Demand",
        resource_id=None,
        sub_account_id=None,
        tags=[{"Application": "web"}, {"Application": "api"}],
        per_row_tag_dicts=[
            {"Application": "web"},
            {"Application": "web"},
            {"Application": "web"},
            {"Application": "api"},
        ],
    )
    focus_charges_repo.upsert_aggregated(session, acc.id, [agg])
    session.commit()

    result = run_chargeback(session, tag_key="Application")
    assert result.insights_emitted == 2

    from constat_api.orm import InsightORM

    rows = session.query(InsightORM).all()
    by_value = {r.payload["tag_value"]: r.payload["billed_cost_usd"] for r in rows}
    # Proportional: web gets 3/4 of 60 = 45, api gets 1/4 of 60 = 15.
    # V1 even split would have been 30/30.
    assert by_value == {"web": 45.0, "api": 15.0}


def test_runner_proportional_split_with_even_tags_matches_v1(
    session: Session,
) -> None:
    """When per-row tags are evenly distributed (2 web + 2 api), the
    proportional split coincides with the V1 even split."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    agg = AggregatedFocusCharge(
        service="AmazonRDS",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal("100"),
        amortized_cost=Decimal("100"),
        charge_count=4,
        region="eu-west-1",
        pricing_category="On-Demand",
        resource_id=None,
        sub_account_id=None,
        tags=[{"Application": "web"}, {"Application": "api"}],
        per_row_tag_dicts=[
            {"Application": "web"},
            {"Application": "web"},
            {"Application": "api"},
            {"Application": "api"},
        ],
    )
    focus_charges_repo.upsert_aggregated(session, acc.id, [agg])
    session.commit()

    result = run_chargeback(session, tag_key="Application")
    assert result.insights_emitted == 2

    from constat_api.orm import InsightORM

    rows = session.query(InsightORM).all()
    by_value = {r.payload["tag_value"]: r.payload["billed_cost_usd"] for r in rows}
    assert by_value == {"web": 50.0, "api": 50.0}


# ---------------------------------------------------------------------------
# End-to-end: ingest FOCUS file with heterogeneous tags
# ---------------------------------------------------------------------------


def _write_focus_csv(path: Path, rows: list[dict]) -> Path:
    """Write a FOCUS 1.0 CSV with the given rows. Each row must include
    a Tags column (JSON-encoded map)."""
    fieldnames = [
        "BillingAccountId",
        "BillingAccountName",
        "ServiceName",
        "ChargePeriodStart",
        "ChargePeriodEnd",
        "BilledCost",
        "EffectiveCost",
        "PricingCategory",
        "Region",
        "ResourceId",
        "SubAccountId",
        "Tags",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    return path


def test_e2e_ingest_and_chargeback_by_tag_uses_proportional_split(
    client: TestClient, session, tmp_path: Path
) -> None:
    """Full path: FOCUS CSV with heterogeneous tags -> ingest ->
    chargeback by tag -> proportional cost attribution."""
    rows = [
        # 3 web rows
        {
            "BillingAccountId": "111111111111",
            "BillingAccountName": "prod",
            "ServiceName": "AmazonRDS",
            "ChargePeriodStart": "2026-07-01T00:00:00Z",
            "ChargePeriodEnd": "2026-07-31T23:59:59Z",
            "BilledCost": "30",
            "EffectiveCost": "30",
            "PricingCategory": "On-Demand",
            "Region": "eu-west-1",
            "ResourceId": "arn:aws:rds:eu-west-1:111111111111:db:web1",
            "SubAccountId": "222222222222",
            "Tags": '{"Application": "web"}',
        },
        {
            "BillingAccountId": "111111111111",
            "BillingAccountName": "prod",
            "ServiceName": "AmazonRDS",
            "ChargePeriodStart": "2026-07-01T00:00:00Z",
            "ChargePeriodEnd": "2026-07-31T23:59:59Z",
            "BilledCost": "30",
            "EffectiveCost": "30",
            "PricingCategory": "On-Demand",
            "Region": "eu-west-1",
            "ResourceId": "arn:aws:rds:eu-west-1:111111111111:db:web2",
            "SubAccountId": "222222222222",
            "Tags": '{"Application": "web"}',
        },
        {
            "BillingAccountId": "111111111111",
            "BillingAccountName": "prod",
            "ServiceName": "AmazonRDS",
            "ChargePeriodStart": "2026-07-01T00:00:00Z",
            "ChargePeriodEnd": "2026-07-31T23:59:59Z",
            "BilledCost": "30",
            "EffectiveCost": "30",
            "PricingCategory": "On-Demand",
            "Region": "eu-west-1",
            "ResourceId": "arn:aws:rds:eu-west-1:111111111111:db:web3",
            "SubAccountId": "222222222222",
            "Tags": '{"Application": "web"}',
        },
        # 1 api row
        {
            "BillingAccountId": "111111111111",
            "BillingAccountName": "prod",
            "ServiceName": "AmazonRDS",
            "ChargePeriodStart": "2026-07-01T00:00:00Z",
            "ChargePeriodEnd": "2026-07-31T23:59:59Z",
            "BilledCost": "10",
            "EffectiveCost": "10",
            "PricingCategory": "On-Demand",
            "Region": "eu-west-1",
            "ResourceId": "arn:aws:rds:eu-west-1:111111111111:db:api1",
            "SubAccountId": "222222222222",
            "Tags": '{"Application": "api"}',
        },
    ]
    csv_path = _write_focus_csv(tmp_path / "focus.csv", rows)

    # Ingest via the CLI (full path, exercises aggregator + upsert).
    # Use the test session so the test's in-memory DB is the source of truth.
    result = ingest_focus_file(
        session=session,
        path=csv_path,
        account_external_id="111111111111",
    )
    assert result.inserted == 1
    assert result.rows_read == 4

    # Per-row tags must be in the table
    tag_rows = session.query(FocusChargeTagORM).all()
    # 4 per-row tag entries (3 web + 1 api)
    assert len(tag_rows) == 4
    web_count = sum(1 for r in tag_rows if r.value == "web")
    api_count = sum(1 for r in tag_rows if r.value == "api")
    assert web_count == 3
    assert api_count == 1

    # Now run the chargeback runner with tag_key=Application
    # Total cost: 30+30+30+10 = 100
    # V2: web gets 3/4 of 100 = 75, api gets 1/4 of 100 = 25
    response = client.post(
        "/insights/run",
        json={"rule": "chargeback", "tag_key": "Application"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["insights_emitted"] == 2

    list_response = client.get("/insights?rule_name=chargeback")
    assert list_response.status_code == 200
    insights = list_response.json()
    by_value = {i["payload"]["tag_value"]: i["payload"]["billed_cost_usd"] for i in insights}
    # V1 would have given 50/50. V2 gives 75/25.
    assert by_value == {"web": 75.0, "api": 25.0}
