"""AWS collection CLI.

Usage:
    python -m constat_api.cli.aws --targets targets.json
    python -m constat_api.cli.aws --dry-run --targets targets.json

The targets JSON is a list of {aws_account_id, role_arn, external_id, name, regions}.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import boto3
from sqlalchemy.orm import Session

from constat_api.collectors.aws import TargetAccount, collect_targets
from constat_api.db import SessionLocal
from constat_api.settings import get_base_aws_session

logger = logging.getLogger(__name__)


def _load_targets(path: Path) -> list[TargetAccount]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        TargetAccount(
            aws_account_id=t["aws_account_id"],
            role_arn=t.get("role_arn"),
            external_id=t.get("external_id"),
            name=t.get("name"),
            regions=tuple(t["regions"]) if t.get("regions") else None,
        )
        for t in raw
    ]


def run_aws_collect(
    *,
    session: Session,
    targets: list[TargetAccount],
    base_session: boto3.Session,
    dry_run: bool = False,
) -> list[dict]:
    results = collect_targets(session, targets, base_session=base_session, dry_run=dry_run)
    return [
        {
            "aws_account_id": r.aws_account_id,
            "regions_scanned": r.regions_scanned,
            "resources_written": r.resources_written,
            "observations_written": r.observations_written,
            "facts_written": r.facts_written,
            "errors": r.errors,
        }
        for r in results
    ]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an AWS RDS collection scan.")
    parser.add_argument(
        "--targets",
        required=True,
        type=Path,
        help="Path to JSON file with [{aws_account_id, role_arn, external_id, name, regions}]",
    )
    parser.add_argument("--dry-run", action="store_true", help="Skip writes, log only")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        targets = _load_targets(args.targets)
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        logger.error("Failed to load targets: %s", e)
        return 1

    base_session = get_base_aws_session()
    try:
        with SessionLocal() as session:
            results = run_aws_collect(
                session=session,
                targets=targets,
                base_session=base_session,
                dry_run=args.dry_run,
            )
        logger.info("Collection complete: %s", json.dumps(results, indent=2))
    except Exception:
        logger.exception("Collection failed")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
