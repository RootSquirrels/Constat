"""AWS collection CLI.

Usage:
    python -m constat_api.cli.aws --targets targets.json
    python -m constat_api.cli.aws --dry-run --targets targets.json
    python -m constat_api.cli.aws --enqueue-all

The targets JSON is a list of {aws_account_id, role_arn, external_id, name, regions}.

`--enqueue-all` is the batch path (roadmap 1.3): it creates one collect
job over ALL persisted collect_targets and enqueues one WorkItem per
(target x region) through the exact same code path as POST /collect/aws
(routers/aws.py::enqueue_all_persisted_targets) — including the SRE-4
commit-before-send ordering and enqueue_error recording. The queue
implementation comes from settings (CONSTAT_COLLECT_MODE=inline|sqs), so
this is what the ECS scheduled task runs in sqs mode; a worker service
drains the queue.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import boto3
from sqlalchemy.orm import Session

from constat_api.collectors.aws import TargetAccount, collect_targets
from constat_api.db import SessionLocal
from constat_api.settings import get_base_aws_session, settings
from constat_api.tenant import bind_tenant

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
) -> list[dict[str, Any]]:
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
    parser = argparse.ArgumentParser(description="Run an AWS collection scan.")
    parser.add_argument(
        "--targets",
        type=Path,
        help="Path to JSON file with [{aws_account_id, role_arn, external_id, name, regions}]",
    )
    parser.add_argument(
        "--enqueue-all",
        action="store_true",
        help=(
            "Create one collect job over ALL persisted collect_targets and "
            "enqueue the work items (same code path as POST /collect/aws). "
            "This is what the ECS scheduled task runs; the queue mode comes "
            "from CONSTAT_COLLECT_MODE."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Skip writes, log only")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return parser


def _enqueue_all() -> int:
    """Batch enqueue over persisted targets. Returns the process exit code."""
    # Late import: the router module pulls in FastAPI; keep the plain
    # --targets path import-light.
    from constat_api.routers.aws import EnqueueError, enqueue_all_persisted_targets

    try:
        with SessionLocal() as session:
            # The GUC must be set for RLS on Postgres (same binding as the
            # API's get_db dependency and the worker).
            bind_tenant(session, settings.default_tenant_id)
            job_id, n_items = enqueue_all_persisted_targets(session, actor="cli:aws:enqueue-all")
    except EnqueueError as e:
        logger.error("Enqueue failed (job kept, marked with enqueue_error): %s", e)
        return 2
    except ValueError as e:
        logger.error("%s", e)
        return 1
    logger.info("Enqueued %d work item(s) under collect job %s", n_items, job_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.enqueue_all:
        if args.targets:
            logger.error("--enqueue-all and --targets are mutually exclusive")
            return 1
        return _enqueue_all()

    if not args.targets:
        logger.error("choose --targets <path> or --enqueue-all")
        return 1

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
