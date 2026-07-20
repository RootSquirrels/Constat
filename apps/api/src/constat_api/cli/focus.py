"""FOCUS ingestion CLI.

Supports both CSV and Parquet FOCUS 1.0 exports. The format is detected
by file extension via `load_focus()`.

Usage:
    python -m constat_api.cli.focus --account 111111111111 --file path/to/focus.csv
    python -m constat_api.cli.focus --account 111111111111 --file path/to/focus.parquet
    python -m constat_api.cli.focus --account 111111111111 --file focus.csv --account-name prod

The function is split from the entry point so it's easily testable with the
shared session fixture.

Quality tracking (UX/ops P2 item 7):
- `rows_total`: every data row in the file (header excluded for CSV, parquet
  metadata for Parquet). Computed before parsing.
- `rows_read`: the rows that parsed successfully (== `len(raw_charges)`).
- `rows_skipped`: `rows_total - rows_read` (malformed rows that were
  logged and dropped by the loader).
- The /collect/focus endpoint surfaces all three so the DAF can see
  "we ingested 1000 lines, 5 were broken" without grepping logs.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from constat_focus.aggregator import aggregate_for_storage
from constat_focus.loader import load_focus
from sqlalchemy.orm import Session

from constat_api.db import SessionLocal
from constat_api.metrics import record_focus_rows
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import focus_charges as focus_charges_repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestResult:
    file_path: str
    account_external_id: str
    account_id: str
    rows_total: int
    rows_read: int
    rows_skipped: int
    rows_written: int
    inserted: int
    updated: int
    duration_seconds: float


def _count_rows_total(path: Path) -> int:
    """Best-effort total row count for a FOCUS file.

    For CSV: count lines minus 1 (the header). Approximate for files
    with embedded newlines in quoted fields; close enough for the
    skip-rate metric.

    For Parquet: read the file's metadata footer (no full scan).
    """
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("rb") as f:
            # Empty-file guard: 0 rows even before subtracting the header.
            n = sum(1 for _ in f)
        return max(0, n - 1)
    if suffix == ".parquet":
        try:
            import pyarrow.parquet as pq  # local import: heavy dep

            md = pq.read_metadata(path)
            return int(md.num_rows)
        except Exception as exc:
            logger.warning("Could not read Parquet metadata for %s: %s", path, exc)
            return -1  # sentinel: unknown
    raise ValueError(f"Unsupported FOCUS file extension: {suffix!r}")


def ingest_focus_file(
    *,
    session: Session,
    path: Path,
    account_external_id: str,
    account_name: str | None = None,
) -> IngestResult:
    """Load + aggregate + upsert one FOCUS file (CSV or Parquet).

    Caller owns the session transaction. This function does NOT commit.

    Returns an IngestResult with rows_total / rows_read / rows_skipped so
    the caller (CLI or /collect/focus router) can report quality stats.
    """
    start = time.monotonic()

    if not path.exists():
        raise FileNotFoundError(f"FOCUS file not found: {path}")

    rows_total = _count_rows_total(path)

    skipped: list[tuple[int, str]] = []

    def on_skip(line_or_idx: int, exc: Exception) -> None:
        skipped.append((line_or_idx, str(exc)))

    raw_charges = list(load_focus(path, on_skip=on_skip))
    rows_read = len(raw_charges)
    # rows_total is best-effort (-1 for unreadable parquet metadata).
    # In that case, rows_skipped is "at least len(skipped)" but we report
    # the exact count the loader tracked.
    rows_skipped = len(skipped) if rows_total < 0 else max(0, rows_total - rows_read)
    logger.info(
        "Loaded %d/%d FOCUS rows from %s (%d skipped)",
        rows_read,
        rows_total if rows_total >= 0 else "?",
        path,
        rows_skipped,
    )

    aggregated = aggregate_for_storage(raw_charges)
    logger.info("Aggregated into %d (account, service, period) rows", len(aggregated))

    account = accounts_repo.get_or_create(session, account_external_id, account_name)
    inserted, updated = focus_charges_repo.upsert_aggregated(session, account.id, aggregated)
    session.commit()

    # P2 item 11: feed the SLO counters. The ingestion observability
    # is a V1 SLO target (rows_skipped > 5% triggers an alert).
    record_focus_rows(ingested=rows_read, skipped=rows_skipped)

    duration = time.monotonic() - start
    return IngestResult(
        file_path=str(path),
        account_external_id=account_external_id,
        account_id=str(account.id),
        rows_total=rows_total,
        rows_read=rows_read,
        rows_skipped=rows_skipped,
        rows_written=inserted + updated,
        inserted=inserted,
        updated=updated,
        duration_seconds=round(duration, 3),
    )


# Back-compat alias for older callers/tests. New code should use ingest_focus_file.
ingest_focus_csv = ingest_focus_file


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ingest a FOCUS 1.0 file into Constat.")
    parser.add_argument("--account", required=True, help="AWS account ID (12-digit)")
    parser.add_argument(
        "--file",
        required=True,
        type=Path,
        help="Path to FOCUS file (.csv or .parquet)",
    )
    parser.add_argument(
        "--account-name", default=None, help="Optional friendly name for the account"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        with SessionLocal() as session:
            result = ingest_focus_file(
                session=session,
                path=args.file,
                account_external_id=args.account,
                account_name=args.account_name,
            )
        logger.info("Ingestion complete: %s", asdict(result))
    except FileNotFoundError as e:
        logger.error("%s", e)
        return 1
    except Exception:
        logger.exception("Ingestion failed")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
