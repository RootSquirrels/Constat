"""Insight runner CLI.

Usage:
    python -m constat_api.cli.run_insights --all                    # every registered rule
    python -m constat_api.cli.run_insights --rule rds_eol --today 2026-07-18
    python -m constat_api.cli.run_insights --rule chargeback --period-label 2026-07
    python -m constat_api.cli.run_insights --rule chargeback --tag-key Application

The scheduled task (infra/ecs.tf) uses `--all`, which iterates RUNNERS:
adding a rule to the registry is enough for it to run daily. The previous
hardcoded two-line command in Terraform silently skipped 4 of 6 rules
(client-committee finding) — tests/test_run_insights_cli.py pins ecs.tf
to `--all` so a hardcoded rule list cannot come back.

With `--all`, one failing rule does not stop the others: each rule runs
in its own session/transaction, failures are logged, and the exit code
is 2 if any rule failed (the scheduler's log stream shows which).
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
    parser = argparse.ArgumentParser(description="Run insight rules.")
    parser.add_argument(
        "--all",
        dest="run_all",
        action="store_true",
        help=(
            "Run every rule registered in RUNNERS (sorted). Used by the "
            "scheduled task so the rule list can never drift from the registry."
        ),
    )
    parser.add_argument(
        "--rule",
        default=None,
        choices=sorted(RUNNERS),
        help=f"Run a single rule. One of: {', '.join(sorted(RUNNERS))}.",
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


def _run_one(rule_name: str, args: argparse.Namespace) -> bool:
    """Run a single rule in its own session. Returns True on success."""
    try:
        with SessionLocal() as session:
            result = run_rule(
                session,
                rule_name,
                today=args.today,
                period_label=args.period_label,
                tag_key=args.tag_key,
            )
    except Exception:
        logger.exception("Rule %s failed", rule_name)
        return False

    logger.info(
        "Rule %s: scanned %d, emitted %d insights, %d inconclusive, %d errors (period=%s)",
        result.rule_name,
        result.resources_scanned,
        result.insights_emitted,
        result.inconclusive_emitted,
        len(result.errors),
        result.period_label or "n/a",
    )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.run_all and args.rule:
        parser.error("--all and --rule are mutually exclusive")
    if not args.run_all and not args.rule:
        parser.error("choose --all or --rule <name>")

    rules = sorted(RUNNERS) if args.run_all else [args.rule]

    # One failing rule must not stop the others (each has its own
    # session/transaction). The exit code reports the worst outcome.
    failed = [rule for rule in rules if not _run_one(rule, args)]
    if failed:
        logger.error("%d/%d rules failed: %s", len(failed), len(rules), ", ".join(failed))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
