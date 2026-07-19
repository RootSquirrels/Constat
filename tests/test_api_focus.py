"""Test the /collect/focus HTTP endpoint."""

from __future__ import annotations

import csv
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.testclient import TestClient


def _csv_fieldnames() -> list[str]:
    return [
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
        "BillingCurrency",
    ]


def _write_csv(path: Path, rows: list[dict]) -> Path:
    fields = _csv_fieldnames()
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    return path


def _write_parquet(path: Path, rows: list[dict]) -> Path:
    fields = _csv_fieldnames()
    table = pa.table({k: [r.get(k, "") for r in rows] for k in fields})
    pq.write_table(table, path)
    return path


def _rds_row() -> dict:
    return {
        "BillingAccountId": "111111111111",
        "BillingAccountName": "prod",
        "ServiceName": "AmazonRDS",
        "ChargePeriodStart": "2026-07-01T00:00:00Z",
        "ChargePeriodEnd": "2026-07-31T23:59:59Z",
        "BilledCost": "100.00",
        "EffectiveCost": "120.00",
        "PricingCategory": "On-Demand",
        "Region": "eu-west-1",
        "ResourceId": "arn:aws:rds:eu-west-1:111111111111:db:myapp",
        "SubAccountId": "222222222222",
        "BillingCurrency": "USD",
    }


def test_focus_ingest_endpoint(client: TestClient, tmp_path: Path) -> None:
    file_path = _write_csv(tmp_path / "focus.csv", [_rds_row()])

    response = client.post(
        "/collect/focus",
        json={"account_external_id": "111111111111", "file_path": str(file_path)},
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
            "file_path": str(tmp_path / "nonexistent.csv"),
        },
    )
    assert response.status_code == 404


def test_focus_ingest_endpoint_accepts_parquet(client: TestClient, tmp_path: Path) -> None:
    """V1: prospect FOCUS data arrives in Parquet. The HTTP endpoint must
    accept the same shape as CSV, format detected by file extension."""
    file_path = _write_parquet(tmp_path / "focus.parquet", [_rds_row()])

    response = client.post(
        "/collect/focus",
        json={"account_external_id": "111111111111", "file_path": str(file_path)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows_read"] == 1
    assert body["inserted"] == 1
