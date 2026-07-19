"""Tests for the FOCUS 1.0 CSV + Parquet loaders."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from constat_focus.loader import (
    BillingCurrencyError,
    load_focus,
    load_focus_csv,
    load_focus_parquet,
)


def _csv_fieldnames() -> list[str]:
    return [
        "BillingAccountId",
        "BillingAccountName",
        "ServiceName",
        "ChargePeriodStart",
        "ChargePeriodEnd",
        "BilledCost",
        "EffectiveCost",  # FOCUS 1.0
        "PricingCategory",
        "Region",
        "ResourceId",  # FOCUS 1.0
        "SubAccountId",  # FOCUS 1.0
        "BillingCurrency",  # FOCUS 1.0 spec §2.10 — required
        "Tags",  # FOCUS 1.0 (optional)
    ]


def _write_csv(tmp_path: Path, rows: list[dict[str, str]], *, include_tags: bool = False) -> Path:
    fieldnames = _csv_fieldnames() if include_tags else _csv_fieldnames()[:-1]
    p = tmp_path / "focus.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    return p


def _write_parquet(
    tmp_path: Path,
    rows: list[dict],
    *,
    include_tags: bool = True,
) -> Path:
    fieldnames_base = _csv_fieldnames()[:-1]
    fieldnames = _csv_fieldnames() if include_tags else fieldnames_base
    table = pa.table({k: [r.get(k, "") for r in rows] for k in fieldnames})
    p = tmp_path / "focus.parquet"
    pq.write_table(table, p)
    return p


def test_loads_valid_focus_1_0_row(tmp_path: Path) -> None:
    p = _write_csv(
        tmp_path,
        [
            {
                "BillingAccountId": "111111111111",
                "BillingAccountName": "prod",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01T00:00:00Z",
                "ChargePeriodEnd": "2026-07-31T23:59:59Z",
                "BilledCost": "100.50",
                "EffectiveCost": "120.00",  # FOCUS 1.0: amortized
                "PricingCategory": "On-Demand",
                "Region": "eu-west-1",
                "ResourceId": "arn:aws:rds:eu-west-1:111111111111:db:myapp",
                "SubAccountId": "222222222222",
                "BillingCurrency": "USD",
            }
        ],
    )

    rows = list(load_focus_csv(p))
    assert len(rows) == 1
    r = rows[0]
    assert r.account_id == "111111111111"
    assert r.service == "AmazonRDS"
    assert r.period_start == date(2026, 7, 1)
    assert r.billed_cost == Decimal("100.50")
    assert r.amortized_cost == Decimal("120.00")
    assert r.resource_id == "arn:aws:rds:eu-west-1:111111111111:db:myapp"
    assert r.sub_account_id == "222222222222"
    assert r.billing_currency == "USD"
    # Tags column is optional; missing -> empty list.
    assert r.tags == []


def test_missing_required_column_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.csv"
    p.write_text("BillingAccountId,ServiceName\n111,AmazonRDS\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing required columns"):
        list(load_focus_csv(p))


def test_rejects_amortized_cost_column_name(tmp_path: Path) -> None:
    """Regression: AmortizedCost was the v0.5 column, renamed to EffectiveCost
    in FOCUS 1.0. A file with the old column name should be rejected as
    non-conformant (fail-loud)."""
    fieldnames = [
        "BillingAccountId",
        "ServiceName",
        "ChargePeriodStart",
        "ChargePeriodEnd",
        "BilledCost",
        "AmortizedCost",  # wrong (FOCUS 0.5 name)
        "PricingCategory",
        "Region",
        "ResourceId",
        "SubAccountId",
    ]
    p = tmp_path / "old_focus.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerow({k: "x" for k in fieldnames})

    with pytest.raises(ValueError, match="missing required columns"):
        list(load_focus_csv(p))


def test_malformed_row_is_skipped(tmp_path: Path) -> None:
    # Second row has an unparseable cost
    p = _write_csv(
        tmp_path,
        [
            {
                "BillingAccountId": "111",
                "BillingAccountName": "x",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "100",
                "EffectiveCost": "100",
                "PricingCategory": "On-Demand",
                "Region": "eu-west-1",
                "ResourceId": "arn:rds:1",
                "SubAccountId": "111",
                "BillingCurrency": "USD",
            },
            {
                "BillingAccountId": "222",
                "BillingAccountName": "y",
                "ServiceName": "AmazonEC2",
                "ChargePeriodStart": "",  # missing -> raises
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "50",
                "EffectiveCost": "50",
                "PricingCategory": "On-Demand",
                "Region": "eu-west-1",
                "ResourceId": "arn:ec2:1",
                "SubAccountId": "222",
                "BillingCurrency": "USD",
            },
        ],
    )

    rows = list(load_focus_csv(p))
    assert len(rows) == 1
    assert rows[0].account_id == "111"


def test_csv_parses_tags_column(tmp_path: Path) -> None:
    p = _write_csv(
        tmp_path,
        [
            {
                "BillingAccountId": "111",
                "BillingAccountName": "prod",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "100",
                "EffectiveCost": "100",
                "PricingCategory": "On-Demand",
                "Region": "eu-west-1",
                "ResourceId": "arn:rds:1",
                "SubAccountId": "111",
                "BillingCurrency": "USD",
                "Tags": '{"Application": "web", "CostCenter": "42"}',
            }
        ],
        include_tags=True,
    )

    rows = list(load_focus_csv(p))
    assert len(rows) == 1
    # The loader wraps the per-row tag dict in a single-element list.
    assert rows[0].tags == [{"Application": "web", "CostCenter": "42"}]


def test_csv_treats_blank_tags_as_empty_list(tmp_path: Path) -> None:
    p = _write_csv(
        tmp_path,
        [
            {
                "BillingAccountId": "111",
                "BillingAccountName": "x",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "100",
                "EffectiveCost": "100",
                "PricingCategory": "On-Demand",
                "Region": "eu-west-1",
                "ResourceId": "arn:rds:1",
                "SubAccountId": "111",
                "BillingCurrency": "USD",
                "Tags": " ",  # FOCUS 1.0 NULL sentinel
            }
        ],
        include_tags=True,
    )
    rows = list(load_focus_csv(p))
    assert rows[0].tags == []


def test_csv_handles_invalid_tags_json(tmp_path: Path) -> None:
    """Bad Tags JSON is logged and treated as empty, not fatal."""
    p = _write_csv(
        tmp_path,
        [
            {
                "BillingAccountId": "111",
                "BillingAccountName": "x",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "100",
                "EffectiveCost": "100",
                "PricingCategory": "On-Demand",
                "Region": "eu-west-1",
                "ResourceId": "arn:rds:1",
                "SubAccountId": "111",
                "BillingCurrency": "USD",
                "Tags": "not json",
            }
        ],
        include_tags=True,
    )
    rows = list(load_focus_csv(p))
    assert len(rows) == 1
    assert rows[0].tags == []


# ---------------------------------------------------------------------------
# Parquet
# ---------------------------------------------------------------------------


def test_loads_valid_focus_1_0_parquet_row(tmp_path: Path) -> None:
    p = _write_parquet(
        tmp_path,
        [
            {
                "BillingAccountId": "111111111111",
                "BillingAccountName": "prod",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01T00:00:00Z",
                "ChargePeriodEnd": "2026-07-31T23:59:59Z",
                "BilledCost": "100.50",
                "EffectiveCost": "120.00",
                "PricingCategory": "On-Demand",
                "Region": "eu-west-1",
                "ResourceId": "arn:aws:rds:eu-west-1:111111111111:db:myapp",
                "SubAccountId": "222222222222",
                "BillingCurrency": "USD",
                "Tags": '{"Application": "web"}',
            }
        ],
    )

    rows = list(load_focus_parquet(p))
    assert len(rows) == 1
    r = rows[0]
    assert r.account_id == "111111111111"
    assert r.service == "AmazonRDS"
    assert r.period_start == date(2026, 7, 1)
    assert r.billed_cost == Decimal("100.50")
    assert r.amortized_cost == Decimal("120.00")
    assert r.resource_id == "arn:aws:rds:eu-west-1:111111111111:db:myapp"
    assert r.sub_account_id == "222222222222"
    assert r.tags == [{"Application": "web"}]


def test_parquet_missing_tags_column_yields_empty_list(tmp_path: Path) -> None:
    """Tags is optional in FOCUS 1.0. A Parquet file without the column
    should still load, with tags=[] on every row."""
    p = _write_parquet(
        tmp_path,
        [
            {
                "BillingAccountId": "111",
                "BillingAccountName": "prod",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "100",
                "EffectiveCost": "100",
                "PricingCategory": "On-Demand",
                "Region": "eu-west-1",
                "ResourceId": "arn:rds:1",
                "SubAccountId": "111",
                "BillingCurrency": "USD",
            }
        ],
        include_tags=False,
    )

    rows = list(load_focus_parquet(p))
    assert len(rows) == 1
    assert rows[0].tags == []


def test_parquet_missing_required_column_raises(tmp_path: Path) -> None:
    table = pa.table(
        {
            "BillingAccountId": ["111"],
            "ServiceName": ["AmazonRDS"],
            # missing: ChargePeriodStart, ChargePeriodEnd, BilledCost, EffectiveCost, ...
        }
    )
    p = tmp_path / "bad.parquet"
    pq.write_table(table, p)

    with pytest.raises(ValueError, match="missing required columns"):
        list(load_focus_parquet(p))


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_load_focus_dispatches_by_extension(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path,
        [
            {
                "BillingAccountId": "111",
                "BillingAccountName": "x",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "10",
                "EffectiveCost": "10",
                "PricingCategory": "On-Demand",
                "Region": "eu-west-1",
                "ResourceId": "arn:rds:1",
                "SubAccountId": "111",
                "BillingCurrency": "USD",
            }
        ],
    )
    parquet_path = _write_parquet(
        tmp_path,
        [
            {
                "BillingAccountId": "222",
                "BillingAccountName": "y",
                "ServiceName": "AmazonEC2",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "20",
                "EffectiveCost": "20",
                "PricingCategory": "On-Demand",
                "Region": "us-east-1",
                "ResourceId": "arn:ec2:1",
                "SubAccountId": "222",
                "BillingCurrency": "USD",
            }
        ],
    )

    csv_rows = list(load_focus(csv_path))
    parquet_rows = list(load_focus(parquet_path))

    assert len(csv_rows) == 1
    assert csv_rows[0].account_id == "111"
    assert len(parquet_rows) == 1
    assert parquet_rows[0].account_id == "222"


def test_load_focus_rejects_unknown_extension(tmp_path: Path) -> None:
    p = tmp_path / "focus.json"
    p.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported FOCUS file extension"):
        list(load_focus(p))


# ---------------------------------------------------------------------------
# BillingCurrency (FOCUS 1.0 spec §2.10) — required, preserved as-written.
# The audit committee (FinOps re-audit) flagged the product for treating
# all amounts as USD regardless of source currency; an EUR-billed export
# was labeled USD and then "converted" to EUR — a 10-20% silent error.
# ---------------------------------------------------------------------------


def test_billing_currency_usd_preserved(tmp_path: Path) -> None:
    p = _write_csv(
        tmp_path,
        [
            {
                "BillingAccountId": "111",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "100.00",
                "EffectiveCost": "100.00",
                "PricingCategory": "On-Demand",
                "ResourceId": "arn:rds:1",
                "SubAccountId": "111",
                "BillingCurrency": "USD",
            }
        ],
    )
    rows = list(load_focus_csv(p))
    assert len(rows) == 1
    assert rows[0].billing_currency == "USD"


def test_billing_currency_eur_preserved(tmp_path: Path) -> None:
    """The committee's headline case: a French export billed in EUR.
    The currency must round-trip to the loader output unchanged."""
    p = _write_csv(
        tmp_path,
        [
            {
                "BillingAccountId": "111",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "92.50",  # raw EUR amount, NOT converted
                "EffectiveCost": "100.00",
                "PricingCategory": "On-Demand",
                "ResourceId": "arn:rds:1",
                "SubAccountId": "111",
                "BillingCurrency": "EUR",
            }
        ],
    )
    rows = list(load_focus_csv(p))
    assert len(rows) == 1
    assert rows[0].billing_currency == "EUR"
    # The BilledCost is the raw 92.50 EUR — we never convert at ingest.
    assert rows[0].billed_cost == Decimal("92.50")


def test_billing_currency_lowercase_rejected(tmp_path: Path) -> None:
    """FOCUS 1.0 §2.10 mandates uppercase ISO 4217 codes. Lowercase or
    mixed-case must be rejected at ingest — silently accepting "eur"
    and storing it downstream would let the EUR/USD confusion recur.

    Bad cells are caught by the per-row try/except and skipped (one
    bad row doesn't fail the whole load) — the on_skip callback
    records the BillingCurrencyError for the operator's
    rows_skipped counter."""
    p = _write_csv(
        tmp_path,
        [
            {
                "BillingAccountId": "111",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "92.50",
                "EffectiveCost": "100.00",
                "PricingCategory": "On-Demand",
                "ResourceId": "arn:rds:1",
                "SubAccountId": "111",
                "BillingCurrency": "eur",  # wrong case
            }
        ],
    )
    skipped: list[tuple[int, Exception]] = []

    def _on_skip(line_no: int, exc: Exception) -> None:
        skipped.append((line_no, exc))

    rows = list(load_focus_csv(p, on_skip=_on_skip))
    assert rows == []
    assert len(skipped) == 1
    assert isinstance(skipped[0][1], BillingCurrencyError)
    assert "must be 3 uppercase" in str(skipped[0][1])


def test_billing_currency_empty_rejected(tmp_path: Path) -> None:
    """Empty BillingCurrency = the FOCUS NULL sentinel. The audit
    committee's deal-breaker: a missing currency on an EUR export
    was being labeled USD. We fail loud at ingest instead."""
    p = _write_csv(
        tmp_path,
        [
            {
                "BillingAccountId": "111",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "92.50",
                "EffectiveCost": "100.00",
                "PricingCategory": "On-Demand",
                "ResourceId": "arn:rds:1",
                "SubAccountId": "111",
                "BillingCurrency": " ",  # FOCUS null sentinel
            }
        ],
    )
    skipped: list[tuple[int, Exception]] = []

    def _on_skip(line_no: int, exc: Exception) -> None:
        skipped.append((line_no, exc))

    rows = list(load_focus_csv(p, on_skip=_on_skip))
    assert rows == []
    assert len(skipped) == 1
    assert isinstance(skipped[0][1], BillingCurrencyError)
    assert "empty BillingCurrency" in str(skipped[0][1])


def test_billing_currency_unknown_code_rejected(tmp_path: Path) -> None:
    """4-letter code, digit codes, and unknown ISO 4217 codes are all
    rejected. Better to fail one row than silently mis-classify."""
    p = _write_csv(
        tmp_path,
        [
            {
                "BillingAccountId": "111",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "92.50",
                "EffectiveCost": "100.00",
                "PricingCategory": "On-Demand",
                "ResourceId": "arn:rds:1",
                "SubAccountId": "111",
                "BillingCurrency": "USDD",  # 4 letters
            }
        ],
    )
    skipped: list[tuple[int, Exception]] = []

    def _on_skip(line_no: int, exc: Exception) -> None:
        skipped.append((line_no, exc))

    rows = list(load_focus_csv(p, on_skip=_on_skip))
    assert rows == []
    assert len(skipped) == 1
    assert isinstance(skipped[0][1], BillingCurrencyError)
    assert "invalid BillingCurrency" in str(skipped[0][1])


def test_billing_currency_missing_column_fails_load(tmp_path: Path) -> None:
    """A column MISSING from the header (vs a cell empty) fails the
    whole load via _validate_columns. Non-conformant export."""
    p = tmp_path / "bad.csv"
    p.write_text(
        "BillingAccountId,ServiceName,BilledCost,EffectiveCost\n111,AmazonRDS,10,10\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing required columns"):
        list(load_focus_csv(p))


def test_billing_currency_gbp_preserved(tmp_path: Path) -> None:
    """GBP is in the accepted set. A London prospect's export round-trips."""
    p = _write_csv(
        tmp_path,
        [
            {
                "BillingAccountId": "111",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "80.00",
                "EffectiveCost": "80.00",
                "PricingCategory": "On-Demand",
                "ResourceId": "arn:rds:1",
                "SubAccountId": "111",
                "BillingCurrency": "GBP",
            }
        ],
    )
    rows = list(load_focus_csv(p))
    assert rows[0].billing_currency == "GBP"


def test_billing_currency_one_bad_row_skipped_others_kept(tmp_path: Path) -> None:
    """A row with a bad BillingCurrency is skipped (one bad row doesn't
    fail the whole load), and the on_skip callback records it for
    the operator's rows_skipped counter."""
    p = _write_csv(
        tmp_path,
        [
            {
                "BillingAccountId": "111",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "10.00",
                "EffectiveCost": "10.00",
                "PricingCategory": "On-Demand",
                "ResourceId": "arn:rds:1",
                "SubAccountId": "111",
                "BillingCurrency": "USD",
            },
            {
                "BillingAccountId": "111",
                "ServiceName": "AmazonEC2",
                "ChargePeriodStart": "2026-07-01",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "5.00",
                "EffectiveCost": "5.00",
                "PricingCategory": "On-Demand",
                "ResourceId": "arn:ec2:1",
                "SubAccountId": "111",
                "BillingCurrency": "eur",  # bad: lowercase
            },
        ],
    )
    skipped: list[tuple[int, Exception]] = []

    def _on_skip(line_no: int, exc: Exception) -> None:
        skipped.append((line_no, exc))

    rows = list(load_focus_csv(p, on_skip=_on_skip))
    assert len(rows) == 1
    assert rows[0].service == "AmazonRDS"
    assert len(skipped) == 1
    assert isinstance(skipped[0][1], BillingCurrencyError)
