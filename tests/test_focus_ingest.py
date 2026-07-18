"""End-to-end tests for the FOCUS ingestion pipeline.

Covers: CLI function with a session fixture, upsert dedup, account auto-create.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from constat_api.cli.focus import ingest_focus_csv
from constat_api.orm import FocusChargeORM
from constat_api.repositories import accounts as accounts_repo
from sqlalchemy.orm import Session


def _write_csv(path: Path, rows: list[dict]) -> Path:
    fields = [
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
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    return path


def _rds_row(billed: str = "100") -> dict:
    return {
        "BillingAccountId": "111111111111",
        "BillingAccountName": "prod",
        "ServiceName": "AmazonRDS",
        "ChargePeriodStart": "2026-07-01T00:00:00Z",
        "ChargePeriodEnd": "2026-07-31T23:59:59Z",
        "BilledCost": billed,
        "EffectiveCost": billed,  # FOCUS 1.0: amortized
        "PricingCategory": "On-Demand",
        "Region": "eu-west-1",
        "ResourceId": "arn:aws:rds:eu-west-1:111111111111:db:myapp",
        "SubAccountId": "222222222222",
    }


def test_ingest_creates_account_and_charges(session: Session, tmp_path: Path) -> None:
    csv_path = _write_csv(tmp_path / "focus.csv", [_rds_row(billed="100.00")])

    result = ingest_focus_csv(
        session=session,
        csv_path=csv_path,
        account_external_id="111111111111",
    )

    assert result.inserted == 1
    assert result.updated == 0
    assert result.rows_read == 1
    assert result.rows_written == 1

    acc = accounts_repo.get_by_external_id(session, "111111111111")
    assert acc is not None
    assert acc.name == "account-111111111111"

    charges = session.query(FocusChargeORM).all()
    assert len(charges) == 1
    assert charges[0].service == "AmazonRDS"
    assert charges[0].billed_cost == 100.0


def test_ingest_aggregates_multiple_rows_for_same_key(session: Session, tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path / "focus.csv",
        [_rds_row(billed="100"), _rds_row(billed="50")],
    )

    result = ingest_focus_csv(
        session=session,
        csv_path=csv_path,
        account_external_id="111111111111",
    )

    assert result.rows_read == 2
    assert result.inserted == 1  # aggregated to 1 row
    assert result.rows_written == 1

    charges = session.query(FocusChargeORM).all()
    assert len(charges) == 1
    assert charges[0].billed_cost == 150.0
    assert charges[0].charge_count == 2


def test_ingest_dedupes_on_rerun(session: Session, tmp_path: Path) -> None:
    csv_path = _write_csv(tmp_path / "focus.csv", [_rds_row(billed="100")])

    # First run: insert
    result1 = ingest_focus_csv(
        session=session, csv_path=csv_path, account_external_id="111111111111"
    )
    assert result1.inserted == 1
    assert result1.updated == 0

    # Second run: update (same natural key)
    result2 = ingest_focus_csv(
        session=session, csv_path=csv_path, account_external_id="111111111111"
    )
    assert result2.inserted == 0
    assert result2.updated == 1

    # Still only one row
    assert session.query(FocusChargeORM).count() == 1


def test_ingest_separates_periods(session: Session, tmp_path: Path) -> None:
    rows = [
        _rds_row(billed="100")
        | {"ChargePeriodStart": "2026-06-01T00:00:00Z", "ChargePeriodEnd": "2026-06-30T23:59:59Z"},
        _rds_row(billed="200")
        | {"ChargePeriodStart": "2026-07-01T00:00:00Z", "ChargePeriodEnd": "2026-07-31T23:59:59Z"},
    ]
    csv_path = _write_csv(tmp_path / "focus.csv", rows)

    result = ingest_focus_csv(
        session=session, csv_path=csv_path, account_external_id="111111111111"
    )

    assert result.inserted == 2  # two different periods
    assert result.rows_written == 2


def test_ingest_uses_friendly_account_name_when_provided(session: Session, tmp_path: Path) -> None:
    csv_path = _write_csv(tmp_path / "focus.csv", [_rds_row()])
    ingest_focus_csv(
        session=session,
        csv_path=csv_path,
        account_external_id="222222222222",
        account_name="prod-eu",
    )
    acc = accounts_repo.get_by_external_id(session, "222222222222")
    assert acc is not None
    assert acc.name == "prod-eu"


def test_ingest_raises_on_missing_file(session: Session, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ingest_focus_csv(
            session=session,
            csv_path=tmp_path / "nonexistent.csv",
            account_external_id="111111111111",
        )
