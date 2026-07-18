"""Tests for the FOCUS CSV loader."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from constat_focus.loader import load_focus_csv


def _write_csv(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    fieldnames = [
        "BillingAccountId",
        "BillingAccountName",
        "ServiceName",
        "ChargePeriodStart",
        "ChargePeriodEnd",
        "BilledCost",
        "AmortizedCost",
        "EffectiveCost",
        "PricingCategory",
        "Region",
    ]
    p = tmp_path / "focus.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    return p


def test_loads_valid_row(tmp_path):
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
                "AmortizedCost": "120.00",
                "EffectiveCost": "95.00",
                "PricingCategory": "On-Demand",
                "Region": "eu-west-1",
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


def test_missing_required_column_raises(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("BillingAccountId,ServiceName\n111,AmazonRDS\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing required columns"):
        list(load_focus_csv(p))


def test_malformed_row_is_skipped(tmp_path):
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
                "AmortizedCost": "100",
                "EffectiveCost": "100",
                "PricingCategory": "On-Demand",
                "Region": "eu-west-1",
            },
            {
                "BillingAccountId": "222",
                "BillingAccountName": "y",
                "ServiceName": "AmazonEC2",
                # Missing ChargePeriodStart -> will raise
                "ChargePeriodStart": "",
                "ChargePeriodEnd": "2026-07-31",
                "BilledCost": "50",
                "AmortizedCost": "50",
                "EffectiveCost": "50",
                "PricingCategory": "On-Demand",
                "Region": "eu-west-1",
            },
        ],
    )

    rows = list(load_focus_csv(p))
    assert len(rows) == 1
    assert rows[0].account_id == "111"
