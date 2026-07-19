"""End-to-end tests for the FOCUS ingestion pipeline.

Covers: CLI function with a session fixture, upsert dedup, account auto-create.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from constat_api.cli.focus import ingest_focus_file
from constat_api.orm import FocusChargeORM
from constat_api.repositories import accounts as accounts_repo
from sqlalchemy.orm import Session


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
        "BillingCurrency": "USD",
    }


def test_ingest_creates_account_and_charges(session: Session, tmp_path: Path) -> None:
    file_path = _write_csv(tmp_path / "focus.csv", [_rds_row(billed="100.00")])

    result = ingest_focus_file(
        session=session,
        path=file_path,
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
    file_path = _write_csv(
        tmp_path / "focus.csv",
        [_rds_row(billed="100"), _rds_row(billed="50")],
    )

    result = ingest_focus_file(
        session=session,
        path=file_path,
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
    file_path = _write_csv(tmp_path / "focus.csv", [_rds_row(billed="100")])

    # First run: insert
    result1 = ingest_focus_file(session=session, path=file_path, account_external_id="111111111111")
    assert result1.inserted == 1
    assert result1.updated == 0

    # Second run: update (same natural key)
    result2 = ingest_focus_file(session=session, path=file_path, account_external_id="111111111111")
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
    file_path = _write_csv(tmp_path / "focus.csv", rows)

    result = ingest_focus_file(session=session, path=file_path, account_external_id="111111111111")

    assert result.inserted == 2  # two different periods
    assert result.rows_written == 2


def test_ingest_uses_friendly_account_name_when_provided(session: Session, tmp_path: Path) -> None:
    file_path = _write_csv(tmp_path / "focus.csv", [_rds_row()])
    ingest_focus_file(
        session=session,
        path=file_path,
        account_external_id="222222222222",
        account_name="prod-eu",
    )
    acc = accounts_repo.get_by_external_id(session, "222222222222")
    assert acc is not None
    assert acc.name == "prod-eu"


def test_ingest_raises_on_missing_file(session: Session, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ingest_focus_file(
            session=session,
            path=tmp_path / "nonexistent.csv",
            account_external_id="111111111111",
        )


def test_ingest_supports_parquet(session: Session, tmp_path: Path) -> None:
    """V1 spec: prospect FOCUS data arrives in Parquet. The same ingest
    pipeline must accept it without code changes."""
    file_path = _write_parquet(tmp_path / "focus.parquet", [_rds_row(billed="250.00")])

    result = ingest_focus_file(
        session=session,
        path=file_path,
        account_external_id="111111111111",
    )

    assert result.inserted == 1
    assert result.updated == 0
    assert result.rows_read == 1

    charges = session.query(FocusChargeORM).all()
    assert len(charges) == 1
    assert charges[0].service == "AmazonRDS"
    assert charges[0].billed_cost == 250.0


def test_ingest_reports_rows_total_and_rows_skipped(session: Session, tmp_path: Path) -> None:
    """UX/ops P2 item 7: the DAF wants to know how many rows we ingested
    and how many were dropped, without grepping logs.

    Here: 3 valid rows, 1 with an unparseable ChargePeriodStart. The
    loader's _row_to_charge raises; the CLI's on_skip callback records
    the line, the loader continues. The IngestResult reports
    rows_total=4, rows_read=3, rows_skipped=1.

    (Note: a bad BilledCost doesn't trigger this path — _parse_decimal
    warns and defaults to 0, the row is still considered valid.)
    """
    file_path = tmp_path / "focus.csv"
    valid = _rds_row(billed="100.00")
    bad = _rds_row(billed="100.00")
    bad["ChargePeriodStart"] = "not-a-date"  # forces _parse_date to raise

    fields = _csv_fieldnames()
    with file_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in [valid, valid, bad, valid]:  # 3 good, 1 bad
            w.writerow({k: r.get(k, "") for k in fields})

    result = ingest_focus_file(
        session=session,
        path=file_path,
        account_external_id="111111111111",
    )

    assert result.rows_total == 4
    assert result.rows_read == 3
    assert result.rows_skipped == 1
    # 3 good rows aggregate to 1 unique (account, service, period) bucket.
    assert result.inserted == 1
    assert result.rows_written == 1
