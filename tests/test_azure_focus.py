"""Azure FOCUS 1.0 ingestion proof.

Answers the product question: "are we ready to ingest FOCUS data coming
from an Azure subscription?" — with a working test, not an opinion.

The fixture (`tests/fixtures/focus_azure_v1_0.csv`) is the Azure twin of
the AWS golden file: full FOCUS 1.0 column set, ProviderName "Microsoft",
ARM-format ResourceIds, GUID SubAccountIds, Azure region names, and a
realistic mixed-currency export (EUR rows + a USD-billed subscription).

What this file proves:
- The loader is provider-agnostic: an Azure-shaped export ingests with
  zero skipped rows, and `BillingCurrency` is preserved per row.
- The chargeback pipeline does NOT silently treat EUR as USD: every
  chargeback insight payload carries `billing_currency`, and mixed
  currencies in one (account, service, period) bucket are never summed
  into one number — they produce one insight per currency.
- The FOCUS reconcile pass is a no-op (not an error) for Azure data:
  Azure ServiceNames are absent from RULE_FOCUS_SERVICES and ARM
  resource ids never match collected AWS native_ids, so no
  `focus_confirmed` context and no ACTUAL flip is possible.

The two fixes this test drove:
- The chargeback resolver used to group by (account, service, period)
  only, summing EUR + USD into a single `*_usd`-labeled amount. It now
  groups currency-aware and labels every payload with `billing_currency`.
- `aggregate_for_storage` used to sum a mixed-currency (service, period)
  bucket and label it with the majority currency (its docstring claimed
  the loader prevented this — it doesn't). The V1 storage key cannot
  hold two currencies for one bucket, so the aggregator now refuses
  loud (ValueError with operator guidance) instead of mislabeling.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from constat_api.cli.focus import ingest_focus_file
from constat_api.insights.reconcile import reconcile_with_focus
from constat_api.insights.runner import run_chargeback, run_resource_rule
from constat_api.orm import InsightORM, ResourceORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import focus_charges as focus_charges_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_chargeback.resolver import aggregate_by_period, aggregate_by_tag, build_insights
from constat_core.models import Fact, ValueState
from constat_focus.aggregator import AggregatedFocusCharge
from constat_focus.loader import load_focus_csv
from sqlalchemy.orm import Session

FIXTURE = Path(__file__).parent / "golden" / "focus_azure.csv"
GOLDEN = Path(__file__).parent / "golden" / "focus_aws.csv"

ROW_COUNT = 18

VM = "Virtual Machines"
PG = "Azure Database for PostgreSQL"
ST = "Storage Accounts"
# Roadmap-consolidation §II.1: the FOCUS loader resolves each
# provider's ServiceName to a cross-provider canonical via the
# service catalog. The aggregator dedups by canonical, the runner
# stores it, and the chargeback resolver emits one insight per
# canonical (the rule never sees the provider's native name).
CANONICAL_VM = "compute_vm"
CANONICAL_PG = "managed_postgres"
CANONICAL_ST = "object_storage"
PG = "Azure Database for PostgreSQL"
ST = "Storage Accounts"

ARM_VM_WEB1 = (
    "/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg-app"
    "/providers/Microsoft.Compute/virtualMachines/web-1"
)
AWS_NATIVE_ID = "arn:aws:rds:eu-west-1:111111111111:db:pg11"


def _load(path: Path) -> tuple[list, list[int]]:
    skips: list[int] = []
    rows = list(load_focus_csv(path, on_skip=lambda line_no, exc: skips.append(line_no)))
    return rows, skips


# ---- Dataset shape ---------------------------------------------------------


def test_azure_fixture_is_spec_shaped_and_azure_flavored() -> None:
    """Same 43-column FOCUS 1.0 header as the AWS golden file; Azure values."""
    with FIXTURE.open(newline="", encoding="utf-8") as f:
        fieldnames = csv.DictReader(f).fieldnames
    with GOLDEN.open(newline="", encoding="utf-8") as f:
        golden_fieldnames = csv.DictReader(f).fieldnames
    assert fieldnames == golden_fieldnames

    with FIXTURE.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == ROW_COUNT
    assert {r["ProviderName"] for r in rows} == {"Microsoft"}
    assert {r["ServiceName"] for r in rows} == {VM, PG, ST}
    assert {r["BillingCurrency"] for r in rows} == {"EUR", "USD"}
    assert {r["RegionId"] for r in rows if r["RegionId"]} == {"westeurope", "francecentral"}
    # Same row-type coverage as the AWS golden: usage, commitment
    # amortization, commitment purchase, credit.
    assert {"Usage", "Purchase", "Credit"} <= {r["ChargeCategory"] for r in rows}
    assert any(r["PricingCategory"] == "Committed" for r in rows)
    # ARM resource ids, and at least one tagged resource.
    arm_ids = [r["ResourceId"] for r in rows if r["ResourceId"]]
    assert arm_ids and all(i.startswith("/subscriptions/") for i in arm_ids)
    assert any(r["Tags"].startswith("{") for r in rows)


# ---- Loader ----------------------------------------------------------------


def test_azure_fixture_loads_with_zero_skips() -> None:
    """A spec-conformant Azure FOCUS 1.0 export must ingest cleanly.

    The loader never reads ProviderName — the FOCUS 1.0 required-column
    set is provider-agnostic, so an Azure export is accepted by shape.
    """
    rows, skips = _load(FIXTURE)
    assert skips == []
    assert len(rows) == ROW_COUNT
    # BillingCurrency is preserved per row (not coerced to USD).
    assert {c.billing_currency for c in rows} == {"EUR", "USD"}
    # Azure region ids survive verbatim.
    assert {c.region for c in rows if c.region} == {"westeurope", "francecentral"}


def test_azure_totals_per_service_per_currency() -> None:
    """Hand-computed totals from tests/fixtures/focus_azure_v1_0.csv.

    Virtual Machines, EUR rows (5), billed / effective:
      210.00/210.00 + 95.00/95.00 + 0.00/60.00 (reservation amortization)
      + 720.00/0.00 (reservation upfront purchase) - 15.00/15.00 (credit)
      => billed 1010.00, amortized 350.00
    Virtual Machines, USD rows (3), billed / effective:
      180.00/180.00 + 0.00/45.00 (savings-plan amortization)
      + 120.00/0.00 (savings-plan recurring purchase)
      => billed 300.00, amortized 225.00
    Azure Database for PostgreSQL, EUR rows (6), billed / effective:
      140.00/140.00 + 38.50/38.50 + 0.00/52.00 (reservation amortization)
      + 600.00/0.00 (reservation upfront purchase) - 25.00/25.00 (credit)
      + 76.00/76.00
      => billed 829.50, amortized 281.50
    Storage Accounts, EUR rows (3): 12.40 - 2.40 (credit) + 8.10
      => billed 18.10, amortized 18.10
    Storage Accounts, USD rows (1): 21.75 => billed 21.75, amortized 21.75
    """
    rows, _ = _load(FIXTURE)
    billed: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    amortized: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for c in rows:
        billed[(c.service, c.billing_currency)] += c.billed_cost
        amortized[(c.service, c.billing_currency)] += c.amortized_cost

    assert billed == {
        (VM, "EUR"): Decimal("1010.00"),
        (VM, "USD"): Decimal("300.00"),
        (PG, "EUR"): Decimal("829.50"),
        (ST, "EUR"): Decimal("18.10"),
        (ST, "USD"): Decimal("21.75"),
    }
    assert amortized == {
        (VM, "EUR"): Decimal("350.00"),
        (VM, "USD"): Decimal("225.00"),
        (PG, "EUR"): Decimal("281.50"),
        (ST, "EUR"): Decimal("18.10"),
        (ST, "USD"): Decimal("21.75"),
    }


# ---- Chargeback resolver (in-memory) ----------------------------------------


def test_chargeback_groups_currency_aware_no_mixed_sum() -> None:
    """EUR rows are NOT silently treated as USD, and a mixed-currency
    (account, service, period) bucket is never summed into one number.

    Before the fix, aggregate_by_period keyed on (account, service,
    period) and produced a single Virtual Machines insight totaling
    1310.00 (1010.00 EUR + 300.00 USD) labeled `*_usd` — a silent FX
    error. Now: one aggregate per currency, each carrying its code.
    """
    rows, _ = _load(FIXTURE)
    aggregated = aggregate_by_period(rows)
    by_key = {(a.service, a.billing_currency): a for a in aggregated}

    assert (VM, "EUR") in by_key and (VM, "USD") in by_key
    assert by_key[(VM, "EUR")].billed_cost == Decimal("1010.00")
    assert by_key[(VM, "USD")].billed_cost == Decimal("300.00")
    # The dishonest mixed sums must not exist anywhere.
    assert all(a.billed_cost != Decimal("1310.00") for a in aggregated)  # VM EUR+USD
    assert all(a.billed_cost != Decimal("39.85") for a in aggregated)  # ST EUR+USD

    insights = build_insights(aggregated)
    assert len(insights) == 5  # 3 services, VM and ST split by currency
    for insight in insights:
        assert insight.payload["billing_currency"] in {"EUR", "USD"}
    vm_eur = next(
        i for i in insights if i.payload["service"] == VM and i.payload["billing_currency"] == "EUR"
    )
    assert vm_eur.payload["billed_cost_usd"] == 1010.0
    assert vm_eur.payload["amortized_cost_usd"] == 350.0
    assert "EUR 660.00" in vm_eur.title  # drift 350 - 1010, labeled in EUR


def test_chargeback_tag_attribution_stays_single_currency() -> None:
    """Tag re-aggregation is currency-aware too: the web-tagged VM rows
    are all EUR, so there is exactly one Application=web bucket, in EUR."""
    rows, _ = _load(FIXTURE)
    web = [a for a in aggregate_by_tag(rows, tag_key="Application") if a.tag_value == "web"]
    assert len(web) == 1
    assert web[0].billing_currency == "EUR"
    assert web[0].billed_cost == Decimal("305.00")  # 210 + 95 + 0 (committed row)
    assert web[0].amortized_cost == Decimal("365.00")  # 210 + 95 + 60


# ---- Chargeback runner over ingested data (DB round-trip) -------------------


def _write_single_currency_extract(tmp_path: Path, currency: str) -> Path:
    """Filter the Azure fixture down to one currency (the operator's
    workaround for the V1 storage limit — see the mixed-currency test)."""
    out = tmp_path / f"focus_azure_{currency.lower()}.csv"
    with (
        FIXTURE.open(newline="", encoding="utf-8") as src,
        out.open("w", newline="", encoding="utf-8") as dst,
    ):
        reader = csv.DictReader(src)
        writer = csv.DictWriter(dst, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            if row["BillingCurrency"] == currency:
                writer.writerow(row)
    return out


def test_mixed_currency_export_refused_loud_at_ingest(session: Session) -> None:
    """The V1 storage natural key is (account, service, period) — one row,
    one currency. The Azure fixture mixes EUR and USD inside the same
    (service, period) buckets; summing them under one label is the
    silent-FX-error class the audit committee flagged. The aggregator
    refuses loud instead. (The resolver-level tests prove the grouping
    itself is currency-aware for storage that can hold both.)"""
    with pytest.raises(ValueError, match="mixes currencies"):
        ingest_focus_file(
            session=session,
            path=FIXTURE,
            account_external_id="87654321",
            account_name="contoso-ea",
        )


def test_ingest_then_run_chargeback_emits_currency_labeled_insights(
    session: Session, tmp_path: Path
) -> None:
    """Full pipeline: CLI ingest of the EUR extract -> run_chargeback ->
    one insight per (account, service, period), each labeled EUR — the
    amounts are never passed off as USD.

    Expected buckets (hand-computed from the fixture's EUR rows):
    - Virtual Machines: billed 1010.00, amortized 350.00 (5 rows)
    - Azure Database for PostgreSQL: billed 829.50, amortized 281.50 (6 rows)
    - Storage Accounts: billed 18.10, amortized 18.10 (3 rows)
    """
    eur_file = _write_single_currency_extract(tmp_path, "EUR")
    result = ingest_focus_file(
        session=session,
        path=eur_file,
        account_external_id="87654321",
        account_name="contoso-ea",
    )
    assert result.rows_total == 14
    assert result.rows_read == 14
    assert result.rows_skipped == 0

    run = run_chargeback(session)
    assert run.errors == []

    insights = session.query(InsightORM).filter(InsightORM.rule_name == "chargeback").all()
    assert len(insights) == 3
    by_service = {i.payload["service"]: i.payload for i in insights}
    # Roadmap-consolidation §II.1: the insight's `service` field is
    # the canonical (cross-provider stable name). The native name
    # lives on `service_native` for traceability.
    assert set(by_service) == {CANONICAL_VM, CANONICAL_PG, CANONICAL_ST}
    for payload in by_service.values():
        assert payload["billing_currency"] == "EUR"
    assert by_service[CANONICAL_VM]["billed_cost_usd"] == 1010.0
    assert by_service[CANONICAL_VM]["amortized_cost_usd"] == 350.0
    assert by_service[CANONICAL_PG]["billed_cost_usd"] == 829.5
    assert by_service[CANONICAL_PG]["amortized_cost_usd"] == 281.5
    assert by_service[CANONICAL_ST]["billed_cost_usd"] == 18.1


# ---- Reconcile: Azure FOCUS rows must be a no-op ----------------------------


def _bootstrap_aws_pg11(session: Session) -> None:
    """An AWS-collected PG11 resource with scope proof + facts (rds_eol fires)."""
    acc = accounts_repo.get_or_create(session, "111111111111")
    resource = ResourceORM(
        account_id=acc.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        native_id=AWS_NATIVE_ID,
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


def _add_azure_focus(session: Session) -> None:
    """An Azure FOCUS line (Azure service name, ARM resource id, EUR)."""
    acc = accounts_repo.get_or_create(session, "87654321", "contoso-ea")
    agg = AggregatedFocusCharge(
        service=PG,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        billed_cost=Decimal("140.00"),
        amortized_cost=Decimal("140.00"),
        charge_count=1,
        region="westeurope",
        pricing_category="On-Demand",
        resource_id=ARM_VM_WEB1,
        sub_account_id="11111111-2222-3333-4444-555555555555",
        tags=[],
        per_row_tag_dicts=[],
        billing_currency="EUR",
    )
    focus_charges_repo.upsert_aggregated(session, acc.id, [agg])
    session.commit()


def test_reconcile_with_azure_focus_is_noop_not_error(session: Session) -> None:
    """RULE_FOCUS_SERVICES maps rules to AWS ServiceNames only. With Azure
    FOCUS data in the base, running an AWS resource rule must:
    - not crash (Azure service names are simply absent from the map),
    - not attach `focus_confirmed` (ARM resource ids never equal AWS
      native_ids, and the Azure service is not the rule's trusted
      ServiceName),
    - never flip anything to ACTUAL.
    """
    _bootstrap_aws_pg11(session)
    _add_azure_focus(session)

    result = run_resource_rule(session, "rds_eol", today=date(2026, 7, 18))
    assert result.errors == []
    assert result.insights_emitted == 1

    insight = session.query(InsightORM).filter(InsightORM.rule_name == "rds_eol").one()
    assert "focus_confirmed" not in insight.payload
    assert insight.payload.get("value_basis", "estimated") == "estimated"

    # The reconcile pass itself, run against the Azure rows, matches nothing.
    assert reconcile_with_focus(session, "rds_eol") == 0
