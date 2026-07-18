"""Cleanup stuck source_runs.

A 'running' source_run that's been active for too long is a sign that
the previous worker died. The partial unique index in migration 0005
then blocks all subsequent scans for that scope until the row is freed.

Usage:
    python -m constat_api.cli.cleanup_stuck_runs               # default 2h threshold
    python -m constat_api.cli.cleanup_stuck_runs --hours 1     # custom threshold

Wire this into a periodic cron / Fargate task to recover from worker
crashes. The default 2h threshold is generous: a healthy RDS scan across
all default regions takes < 5 min. Lower it to e.g. 30 minutes for
tighter recovery SLAs.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta

from constat_api.db import SessionLocal
from constat_api.repositories import source_runs as source_runs_repo

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mark stuck source_runs as 'failed'.")
    parser.add_argument(
        "--hours",
        type=float,
        default=2.0,
        help="Threshold in hours: runs in 'running' state older than this are cleaned up. Default: 2.0",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    threshold = timedelta(hours=args.hours)
    try:
        with SessionLocal() as session:
            cleaned = source_runs_repo.cleanup_stuck_runs(session, threshold=threshold)
    except Exception:
        logger.exception("Cleanup failed")
        return 2

    logger.info("Cleaned up %d stuck run(s) (threshold=%s)", cleaned, threshold)
    return 0


if __name__ == "__main__":
    sys.exit(main())
