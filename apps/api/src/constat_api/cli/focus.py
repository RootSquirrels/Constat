"""FOCUS CSV ingestion CLI.

Usage:
    python -m constat_api.cli.focus --account 111111111111 --csv path/to/focus.csv
    python -m constat_api.cli.focus --account 111111111111 --csv focus.csv --account-name prod

The function is split from the entry point so it's easily testable with the
shared session fixture.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from constat_focus.aggregator import aggregate_for_storage
from constat_focus.loader import load_focus_csv
from sqlalchemy.orm import Session

from constat_api.db import SessionLocal
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import focus_charges as focus_charges_repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestResult:
    csv_path: str
    account_external_id: str
    account_id: str
    rows_read: int
    rows_written: int
    inserted: int
    updated: int
    duration_seconds: float


def ingest_focus_csv(
    *,
    session: Session,
    csv_path: Path,
    account_external_id: str,
    account_name: str | None = None,
) -> IngestResult:
    """Load + aggregate + upsert one FOCUS CSV.

    Caller owns the session transaction. This function does NOT commit.
    """
    start = time.monotonic()

    if not csv_path.exists():
        raise FileNotFoundError(f"FOCUS CSV not found: {csv_path}")

    raw_charges = list(load_focus_csv(csv_path))
    logger.info("Loaded %d FOCUS rows from %s", len(raw_charges), csv_path)

    aggregated = aggregate_for_storage(raw_charges)
    logger.info("Aggregated into %d (account, service, period) rows", len(aggregated))

    account = accounts_repo.get_or_create(session, account_external_id, account_name)
    inserted, updated = focus_charges_repo.upsert_aggregated(session, account.id, aggregated)
    session.commit()

    duration = time.monotonic() - start
    return IngestResult(
        csv_path=str(csv_path),
        account_external_id=account_external_id,
        account_id=str(account.id),
        rows_read=len(raw_charges),
        rows_written=inserted + updated,
        inserted=inserted,
        updated=updated,
        duration_seconds=round(duration, 3),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ingest a FOCUS CSV export into Constat.")
    parser.add_argument("--account", required=True, help="AWS account ID (12-digit)")
    parser.add_argument("--csv", required=True, type=Path, help="Path to FOCUS CSV file")
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
            result = ingest_focus_csv(
                session=session,
                csv_path=args.csv,
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
