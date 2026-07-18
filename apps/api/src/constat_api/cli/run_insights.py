"""Insight runner CLI.

Usage:
    python -m constat_api.cli.run_insights                    # run rds_eol with today
    python -m constat_api.cli.run_insights --today 2026-07-18 # run with a specific date
    python -m constat_api.cli.run_insights --rule rds_eol -v   # verbose
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date as date_type

from constat_api.db import SessionLocal
from constat_api.insights.runner import run_rds_eol

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an insight rule across all resources.")
    parser.add_argument(
        "--rule",
        default="rds_eol",
        choices=["rds_eol"],
        help="Rule to run (V1: only rds_eol).",
    )
    parser.add_argument(
        "--today",
        type=lambda s: date_type.fromisoformat(s),
        default=None,
        help="Override 'today' for deterministic EOL/pricing calc (ISO date).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        with SessionLocal() as session:
            if args.rule == "rds_eol":
                result = run_rds_eol(session, today=args.today)
            else:
                logger.error("Unknown rule: %s", args.rule)
                return 1
    except Exception:
        logger.exception("Run failed")
        return 2

    logger.info(
        "Rule %s: scanned %d, emitted %d insights, %d inconclusive, %d errors",
        result.rule_name,
        result.resources_scanned,
        result.insights_emitted,
        result.inconclusive_emitted,
        len(result.errors),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
