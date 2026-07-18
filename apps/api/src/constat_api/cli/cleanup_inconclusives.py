"""Inconclusive cleanup CLI.

UX/ops P2 item 8: the `inconclusive` table grows without bound. A
"missing fact" listed 6 months ago is no longer actionable. This
command deletes records older than N days.

Usage:
    python -m constat_api.cli.cleanup_inconclusives --older-than 30
    python -m constat_api.cli.cleanup_inconclusives --older-than 7 --dry-run
    python -m constat_api.cli.cleanup_inconclusives --older-than 30 --rule rds_eol

Schedule it from cron / k8s CronJob / Task Scheduler. See
`docs/operations/inconclusive-cleanup.md` for the recommended cadence.
"""

from __future__ import annotations

import argparse
import logging
import sys

from constat_api.db import SessionLocal
from constat_api.repositories import inconclusive as inconclusive_repo

logger = logging.getLogger(__name__)


def run_cleanup(
    *, older_than_days: int, rule_name: str | None = None, dry_run: bool = False
) -> int:
    """Delete inconclusive records older than N days. Returns the rowcount."""
    with SessionLocal() as session:
        if dry_run:
            # In dry-run, count without deleting.
            # (Reuse count_inconclusive scoped by rule, then apply cutoff
            # in Python — good enough for a one-off audit.)
            from datetime import UTC, datetime, timedelta

            from sqlalchemy import select

            from constat_api.orm import InconclusiveORM

            cutoff = datetime.now(tz=UTC) - timedelta(days=older_than_days)
            stmt = select(InconclusiveORM).where(InconclusiveORM.computed_at < cutoff)
            if rule_name is not None:
                stmt = stmt.where(InconclusiveORM.rule_name == rule_name)
            eligible = session.execute(stmt).scalars().all()
            logger.info(
                "DRY RUN: %d inconclusive record(s) older than %d days (rule=%s) would be deleted",
                len(eligible),
                older_than_days,
                rule_name or "*",
            )
            return len(eligible)

        # The repo's `delete_older_than` doesn't take a rule filter. For
        # the CLI/HTTP path, we accept all rules in one call (the typical
        # operation is "clean the whole table"). If the user wants per-rule
        # cleanup, we add it later.
        deleted = inconclusive_repo.delete_older_than(session, older_than_days=older_than_days)
        session.commit()
        logger.info(
            "Deleted %d inconclusive record(s) older than %d days",
            deleted,
            older_than_days,
        )
        return deleted


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Delete inconclusive records older than N days.")
    parser.add_argument(
        "--older-than",
        type=int,
        required=True,
        help="Age threshold in days. Records with computed_at < now-N days are deleted.",
    )
    parser.add_argument(
        "--rule",
        default=None,
        help="If set, only delete records for this rule_name (dry-run only in V1).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count eligible records without deleting.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.rule is not None and not args.dry_run:
        logger.error(
            "--rule is currently dry-run only (the delete path doesn't yet "
            "support per-rule filtering). Use --dry-run --rule <name> to audit."
        )
        return 2

    try:
        run_cleanup(
            older_than_days=args.older_than,
            rule_name=args.rule,
            dry_run=args.dry_run,
        )
    except Exception:
        logger.exception("Cleanup failed")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
