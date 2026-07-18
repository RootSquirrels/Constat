"""Insight runner CLI.

Usage:
    python -m constat_api.cli.run_insights                          # run rds_eol with today
    python -m constat_api.cli.run_insights --rule rds_eol --today 2026-07-18
    python -m constat_api.cli.run_insights --rule chargeback        # all-time, all accounts
    python -m constat_api.cli.run_insights --rule chargeback --period-label 2026-07
    python -m constat_api.cli.run_insights --rule chargeback --tag-key Application
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date as date_type

from constat_api.db import SessionLocal
from constat_api.insights.runner import RUNNERS, run_rule

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an insight rule.")
    parser.add_argument(
        "--rule",
        default="rds_eol",
        choices=sorted(RUNNERS),
        help="Rule to run (V1: rds_eol, chargeback).",
    )
    parser.add_argument(
        "--today",
        type=lambda s: date_type.fromisoformat(s),
        default=None,
        help="Override 'today' for deterministic EOL/pricing calc (ISO date). Used by rds_eol.",
    )
    parser.add_argument(
        "--period-label",
        default="all-time",
        help="Label stored in the insight payload. Used by chargeback.",
    )
    parser.add_argument(
        "--tag-key",
        default=None,
        help=(
            "FOCUS tag key to re-aggregate by (e.g. 'Application', 'CostCenter'). "
            "Only used by the chargeback rule. Charges with no tag for the key are "
            "bucketed as '__untagged__'."
        ),
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
            result = run_rule(
                session,
                args.rule,
                today=args.today,
                period_label=args.period_label,
                tag_key=args.tag_key,
            )
    except Exception:
        logger.exception("Run failed")
        return 2

    logger.info(
        "Rule %s: scanned %d, emitted %d insights, %d inconclusive, %d errors (period=%s)",
        result.rule_name,
        result.resources_scanned,
        result.insights_emitted,
        result.inconclusive_emitted,
        len(result.errors),
        result.period_label or "n/a",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
