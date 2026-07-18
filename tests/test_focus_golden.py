"""Golden FOCUS 1.0 dataset harness.

Why this file exists: the original home-grown CSV fixtures silently
diverged from the FOCUS 1.0 spec (AmortizedCost instead of EffectiveCost;
Region instead of RegionId/RegionName). The AmortizedCost rename was
caught and fixed in the loader; the Region rename was NOT — this harness
caught it (`loader.py` required a `Region` column that FOCUS 1.0 does
not define; spec §2.32/2.33 renames it `RegionId` + `RegionName`).

The loader bug is FIXED: `_validate_columns` accepts `RegionId` (1.0)
with a pre-1.0 `Region` fallback. The tests below run directly against
the spec-shaped golden file — no shim.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from constat_focus.loader import load_focus_csv

FIXTURE = Path(__file__).parent / "fixtures" / "focus_golden_v1_0.csv"

# The full official FOCUS 1.0 column set, in spec (alphabetical) order.
# https://focus.finops.org/focus-specification/v1-0/ section 2.
FOCUS_1_0_COLUMNS: list[str] = [
    "AvailabilityZone",
    "BilledCost",
    "BillingAccountId",
    "BillingAccountName",
    "BillingCurrency",
    "BillingPeriodEnd",
    "BillingPeriodStart",
    "ChargeCategory",
    "ChargeClass",
    "ChargeDescription",
    "ChargeFrequency",
    "ChargePeriodEnd",
    "ChargePeriodStart",
    "CommitmentDiscountCategory",
    "CommitmentDiscountId",
    "CommitmentDiscountName",
    "CommitmentDiscountStatus",
    "CommitmentDiscountType",
    "ConsumedQuantity",
    "ConsumedUnit",
    "ContractedCost",
    "ContractedUnitPrice",
    "EffectiveCost",
    "InvoiceIssuerName",
    "ListCost",
    "ListUnitPrice",
    "PricingCategory",
    "PricingQuantity",
    "PricingUnit",
    "ProviderName",
    "PublisherName",
    "RegionId",
    "RegionName",
    "ResourceId",
    "ResourceName",
    "ResourceType",
    "ServiceCategory",
    "ServiceName",
    "SkuId",
    "SkuPriceId",
    "SubAccountId",
    "SubAccountName",
    "Tags",
]

ROW_COUNT = 22

RDS = "Amazon Relational Database Service"
EC2 = "Amazon Elastic Compute Cloud - Compute"


def _load(path: Path) -> tuple[list, list[int]]:
    skips: list[int] = []
    rows = list(load_focus_csv(path, on_skip=lambda line_no, exc: skips.append(line_no)))
    return rows, skips


# ---- Dataset shape ---------------------------------------------------------


def test_golden_csv_header_matches_focus_1_0_column_set() -> None:
    """The golden file carries the FULL official FOCUS 1.0 column set.

    This is the tripwire the home-grown CSVs lacked: they omitted most
    columns and used pre-1.0 names (AmortizedCost, Region), so loader
    drift against the spec went unnoticed.
    """
    with FIXTURE.open(newline="", encoding="utf-8") as f:
        fieldnames = csv.DictReader(f).fieldnames
    assert fieldnames == FOCUS_1_0_COLUMNS
    # Explicit guards for the two renames that bit us before.
    assert "AmortizedCost" not in (fieldnames or [])  # renamed EffectiveCost in 1.0
    assert "Region" not in (fieldnames or [])  # renamed RegionId in 1.0


def test_golden_csv_has_expected_coverage() -> None:
    """~20-30 rows covering 2 services, 2 regions, usage + RI amortization
    + refund/credit rows, and at least one tagged resource."""
    with FIXTURE.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == ROW_COUNT
    assert {r["ServiceName"] for r in rows} == {RDS, EC2}
    assert {r["RegionId"] for r in rows if r["RegionId"]} == {"eu-west-1", "us-east-1"}
    categories = {r["ChargeCategory"] for r in rows}
    assert {"Usage", "Purchase", "Credit"} <= categories
    assert any(r["PricingCategory"] == "Committed" for r in rows)  # RI/SP amortization
    assert any(r["Tags"].startswith("{") for r in rows)  # tagged resource


# ---- Loader against the golden file ----------------------------------------


def test_golden_loads_with_zero_errors() -> None:
    """A spec-conformant FOCUS 1.0 file must ingest with zero skipped rows."""
    rows, skips = _load(FIXTURE)
    assert skips == []
    assert len(rows) == ROW_COUNT


def test_billed_and_amortized_totals_per_service() -> None:
    """Hand-computed totals from tests/fixtures/focus_golden_v1_0.csv.

    Amazon Relational Database Service (12 rows), billed / effective:
      100.00/100.00 + 60.00/60.00 + 0.00/40.00 + 0.00/35.00
      + 25.50/25.50 + 0.00/15.25 + 900.00/0.00 (RI upfront purchase)
      - 20.00/20.00 (credit) + 200.00/200.00 + 0.00/80.00
      + 45.00/45.00 + 150.00/0.00 (SP recurring purchase)
      => billed 1460.50, amortized 580.75

    Amazon Elastic Compute Cloud - Compute (10 rows), billed / effective:
      300.00/300.00 + 120.00/120.00 + 0.00/90.00 - 10.00/10.00 (credit)
      + 80.00/80.00 + 0.00/30.00 + 250.00/250.00 + 0.00/60.00
      + 33.33/33.33 + 500.00/0.00 (SP upfront purchase)
      => billed 1273.33, amortized 953.33

    Grand totals: billed 2733.83, amortized 1534.08.
    """
    rows, _ = _load(FIXTURE)
    billed: dict[str, Decimal] = defaultdict(Decimal)
    amortized: dict[str, Decimal] = defaultdict(Decimal)
    for c in rows:
        billed[c.service] += c.billed_cost
        amortized[c.service] += c.amortized_cost

    assert billed == {
        RDS: Decimal("1460.50"),
        EC2: Decimal("1273.33"),
    }
    assert amortized == {
        RDS: Decimal("580.75"),
        EC2: Decimal("953.33"),
    }
    assert sum(billed.values()) == Decimal("2733.83")
    assert sum(amortized.values()) == Decimal("1534.08")


def test_tagged_resource_tags_parsed() -> None:
    """db.myapp rows carry the Tags JSON; untagged rows yield an empty list."""
    rows, _ = _load(FIXTURE)
    tagged = [c for c in rows if c.resource_id and c.resource_id.endswith(":db:myapp")]
    assert len(tagged) == 6
    for c in tagged:
        assert c.tags == [{"Application": "web", "CostCenter": "42"}]
    untagged = [c for c in rows if c.resource_id and not c.resource_id.endswith(":db:myapp")]
    assert untagged
    for c in untagged:
        assert c.tags == []


def test_extra_focus_columns_not_silently_dropped() -> None:
    """With the full 43-column header present, every row must round-trip
    field-for-field: extra spec columns must not shift, misroute, or
    corrupt the columns the loader does read."""
    with FIXTURE.open(newline="", encoding="utf-8") as f:
        source_rows = list(csv.DictReader(f))
    rows, _ = _load(FIXTURE)
    assert len(rows) == len(source_rows)

    for charge, src in zip(rows, source_rows, strict=True):
        assert charge.account_id == src["BillingAccountId"]
        assert charge.account_name == src["BillingAccountName"]
        assert charge.service == src["ServiceName"]
        assert charge.billed_cost == Decimal(src["BilledCost"])
        assert charge.amortized_cost == Decimal(src["EffectiveCost"])
        # Loader maps RegionId (FOCUS 1.0); legacy Region is the fallback.
        expected_region = src["RegionId"] or None
        assert charge.region == expected_region
        assert charge.resource_id == (src["ResourceId"] or None)
        assert charge.sub_account_id == (src["SubAccountId"] or None)
        assert charge.pricing_category == (src["PricingCategory"] or None)
