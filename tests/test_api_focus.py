"""Test the /collect/focus HTTP endpoint."""

from __future__ import annotations

import csv
from pathlib import Path

from fastapi.testclient import TestClient


def _write_csv(path: Path, rows: list[dict]) -> Path:
    fields = [
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
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    return path


def test_focus_ingest_endpoint(client: TestClient, tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path / "focus.csv",
        [
            {
                "BillingAccountId": "111111111111",
                "BillingAccountName": "prod",
                "ServiceName": "AmazonRDS",
                "ChargePeriodStart": "2026-07-01T00:00:00Z",
                "ChargePeriodEnd": "2026-07-31T23:59:59Z",
                "BilledCost": "100.00",
                "AmortizedCost": "120.00",
                "EffectiveCost": "95.00",
                "PricingCategory": "On-Demand",
                "Region": "eu-west-1",
            }
        ],
    )

    response = client.post(
        "/collect/focus",
        json={"account_external_id": "111111111111", "csv_path": str(csv_path)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows_read"] == 1
    assert body["inserted"] == 1
    assert body["updated"] == 0
    assert "account_id" in body


def test_focus_ingest_endpoint_404_on_missing_file(client: TestClient, tmp_path: Path) -> None:
    response = client.post(
        "/collect/focus",
        json={
            "account_external_id": "111111111111",
            "csv_path": str(tmp_path / "nonexistent.csv"),
        },
    )
    assert response.status_code == 404
