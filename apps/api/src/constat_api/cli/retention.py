"""Retention policy runner CLI.

Applies the configured retention policies to delete data older than
the configured window. Wire to a periodic scheduler (cron / Fargate
task / Task Scheduler on Windows) — the same pattern as
cleanup_stuck_runs.

Usage:
    python -m constat_api.cli.retention                      # run all enabled policies
    python -m constat_api.cli.retention --seed              # seed defaults first
    python -m constat_api.cli.retention --table observations  # single table
    python -m constat_api.cli.retention --list              # show current policies
    python -m constat_api.cli.retention --dry-run           # preview, no deletes
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import select

from constat_api.audit import ACTOR_SYSTEM_RETENTION, record_event
from constat_api.db import SessionLocal
from constat_api.orm import RetentionPolicyORM
from constat_api.retention import (
    ALLOWED_TABLES,
    apply_all_enabled,
    apply_retention,
    seed_default_policies,
)

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply retention policies to delete old data. "
        "Wire to a periodic scheduler (cron / Fargate task)."
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Seed the default retention policies before running. Idempotent.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the current retention policies and exit.",
    )
    parser.add_argument(
        "--table",
        choices=sorted(ALLOWED_TABLES),
        default=None,
        help="Run retention for a single table. Default: every enabled policy.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be deleted without actually deleting.",
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
            if args.seed:
                seeded = seed_default_policies(session)
                logger.info("Seeded %d default retention policies", seeded)

            if args.list:
                policies = (
                    session.execute(
                        select(RetentionPolicyORM).order_by(RetentionPolicyORM.table_name)
                    )
                    .scalars()
                    .all()
                )
                print(f"{'TABLE':<25} {'DAYS':>6}  {'ENABLED':<8}  LAST APPLIED")
                for p in policies:
                    last = p.last_applied_at.isoformat() if p.last_applied_at else "(never)"
                    print(f"{p.table_name:<25} {p.retention_days:>6}  {p.enabled!s:<8}  {last}")
                return 0

            if args.dry_run:
                # Just print the policies; don't actually run retention.
                policies = (
                    session.execute(
                        select(RetentionPolicyORM).where(RetentionPolicyORM.enabled.is_(True))
                    )
                    .scalars()
                    .all()
                )
                print("DRY-RUN — would apply these policies:")
                for p in policies:
                    print(f"  {p.table_name}: delete rows older than {p.retention_days} days")
                return 0

            if args.table:
                # Single-table mode.
                policy = session.execute(
                    select(RetentionPolicyORM).where(RetentionPolicyORM.table_name == args.table)
                ).scalar_one_or_none()
                if policy is None:
                    logger.error("No policy found for table %r", args.table)
                    return 1
                if not policy.enabled:
                    logger.warning("Policy for %r is disabled; skipping", args.table)
                    return 0
                deleted = apply_retention(
                    session,
                    table_name=policy.table_name,
                    retention_days=policy.retention_days,
                )
                logger.info(
                    "Retention[%s]: deleted %d row(s) older than %d days",
                    policy.table_name,
                    deleted,
                    policy.retention_days,
                )
                record_event(
                    session,
                    action="retention_applied",
                    actor=ACTOR_SYSTEM_RETENTION,
                    target_type="table",
                    target_id=policy.table_name,
                    metadata={
                        "deleted_count": deleted,
                        "retention_days": policy.retention_days,
                    },
                )
                session.commit()
                return 0

            # Default: apply all enabled policies.
            results = apply_all_enabled(session)
            logger.info("Retention complete:")
            for table_name, deleted in results.items():
                logger.info("  %s: %d row(s) deleted", table_name, deleted)
            record_event(
                session,
                action="retention_applied_all",
                actor=ACTOR_SYSTEM_RETENTION,
                target_type="system",
                target_id="retention",
                metadata={
                    "tables_processed": len(results),
                    "total_deleted": sum(max(0, v) for v in results.values()),
                },
            )
            session.commit()
    except Exception:
        logger.exception("Retention run failed")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
