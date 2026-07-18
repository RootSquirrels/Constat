"""Replay facts from observations.

Rebuilds the `facts` table from the `observations` table. Use cases:
- A resolver (rds_eol, ...) changes its facts derivation logic. Replay
  to update the facts for past observations without re-scanning AWS.
- A bug in db_to_facts produced wrong facts. Replay to fix them
  retroactively.
- The facts table was corrupted / lost. Replay from the immutable
  observations log.

The replay is deterministic: each observation's payload is run through
the same translation function the collector used (db_to_facts for RDS).
Result: the facts after replay == the facts that would have been written
at the original scan time, modulo resolver changes.

Usage:
    python -m constat_api.cli.replay_facts --dry-run            # preview
    python -m constat_api.cli.replay_facts --account 111        # one account
    python -m constat_api.cli.replay_facts --since 2026-07-01    # since date
    python -m constat_api.cli.replay_facts                       # everything

This is a V1 admin tool. No HTTP endpoint — replay is destructive and
operator-initiated. We don't want a UI button for "rewrite my facts".
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from constat_aws_rds.collector import db_to_facts
from sqlalchemy import select
from sqlalchemy.orm import Session

from constat_api.db import SessionLocal
from constat_api.orm import ObservationORM, ResourceORM
from constat_api.repositories import facts as facts_repo

logger = logging.getLogger(__name__)

# Sources for which the replay tool knows how to translate observations
# back into facts. New connectors add their own (source, translator) pair.
SUPPORTED_SOURCES: dict[str, Any] = {
    "aws_rds": db_to_facts,
}


def _payload_to_db(payload: dict[str, Any], region: str) -> dict[str, Any]:
    """Reverse of `db_to_observation`: rebuild the boto3 dict from the
    stored payload. Mirrors the fields kept in db_to_observation.
    """
    create_time_str = payload.get("InstanceCreateTime")
    create_time = None
    if create_time_str:
        try:
            # Stored as ISO 8601. boto3 returns a datetime; db_to_facts
            # only reads scalar fields, so this could stay a string in
            # principle, but db_to_observation stored it as a string
            # for portability. We leave it as a string.
            create_time = datetime.fromisoformat(create_time_str)
        except (ValueError, TypeError):
            create_time = None
    return {
        "DBInstanceArn": payload.get("DBInstanceArn"),
        "DBInstanceIdentifier": payload.get("DBInstanceIdentifier"),
        "Engine": payload.get("Engine"),
        "EngineVersion": payload.get("EngineVersion"),
        "DBInstanceClass": payload.get("DBInstanceClass"),
        "DBInstanceStatus": payload.get("DBInstanceStatus"),
        "AllocatedStorage": payload.get("AllocatedStorage"),
        "InstanceCreateTime": create_time,
        "MultiAZ": payload.get("MultiAZ"),
        "StorageEncrypted": payload.get("StorageEncrypted"),
        "DBSubnetGroup": (
            {"DBSubnetGroupName": payload["DBSubnetGroup"]}
            if payload.get("DBSubnetGroup")
            else None
        ),
        "Endpoint": {"Address": payload["Endpoint"]} if payload.get("Endpoint") else None,
        "_region": region,
    }


def replay_observations(
    session: Session,
    *,
    account_external_id: str | None = None,
    since: datetime | None = None,
    sources: Iterable[str] | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Replay observations into facts. Returns a small stats dict.

    Filters:
    - account_external_id: only observations on this AWS account.
    - since: only observations with observed_at >= this.
    - sources: only observations with source in this set. Default: all
      sources we know how to translate.

    Returns: {observations_scanned, facts_upserted, observations_skipped}
    """
    sources_set = set(sources) if sources else set(SUPPORTED_SOURCES)
    unknown = sources_set - set(SUPPORTED_SOURCES)
    if unknown:
        raise ValueError(f"Unknown source(s) for replay: {sorted(unknown)}")

    # Build the query
    stmt = select(ObservationORM).order_by(ObservationORM.observed_at.asc())
    if since is not None:
        stmt = stmt.where(ObservationORM.observed_at >= since)
    if account_external_id is not None:
        # Join via resources -> accounts to filter by external_id
        stmt = stmt.join(ResourceORM, ObservationORM.resource_id == ResourceORM.id)
        from constat_api.orm import AccountORM

        stmt = stmt.join(AccountORM, ResourceORM.account_id == AccountORM.id).where(
            AccountORM.external_id == account_external_id
        )
    observations = session.execute(stmt).scalars().all()

    facts_upserted = 0
    skipped = 0
    seen_observation_ids: set = set()
    for obs in observations:
        if obs.id in seen_observation_ids:
            continue
        seen_observation_ids.add(obs.id)
        if obs.source not in sources_set:
            skipped += 1
            continue
        translator = SUPPORTED_SOURCES[obs.source]
        resource = session.get(ResourceORM, obs.resource_id)
        if resource is None or resource.account_id is None:
            skipped += 1
            continue
        # Reconstruct the boto3-style dict
        if obs.source == "aws_rds":
            db = _payload_to_db(obs.payload, resource.region)
        else:
            skipped += 1
            continue
        facts = translator(resource.id, str(resource.account_id), db, obs.observed_at)
        if not dry_run:
            facts_repo.upsert_facts(session, facts, source_run_id=obs.source_run_id)
        facts_upserted += len(facts)
        # Flush every 200 observations to keep the transaction small.
        if not dry_run and facts_upserted % 200 == 0:
            session.flush()

    if not dry_run:
        session.commit()
    return {
        "observations_scanned": len(observations),
        "facts_upserted": facts_upserted,
        "observations_skipped": skipped,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay observations into the facts table (rebuild without re-scanning AWS)."
    )
    parser.add_argument(
        "--account",
        default=None,
        help="Filter by AWS account external_id (12-digit). Default: all accounts.",
    )
    parser.add_argument(
        "--since",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
        default=None,
        help="Only observations with observed_at >= this ISO date.",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        help="Source name to replay. Repeat for multiple. Default: all supported.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the diff but don't write. Shows the would-be stats.",
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
            stats = replay_observations(
                session,
                account_external_id=args.account,
                since=args.since,
                sources=args.source,
                dry_run=args.dry_run,
            )
    except Exception:
        logger.exception("Replay failed")
        return 2

    mode = "DRY-RUN" if args.dry_run else "REPLAY"
    logger.info(
        "%s: scanned=%d, facts_upserted=%d, skipped=%d",
        mode,
        stats["observations_scanned"],
        stats["facts_upserted"],
        stats["observations_skipped"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
