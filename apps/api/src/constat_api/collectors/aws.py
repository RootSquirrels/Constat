"""AWS collector: cross-account RDS scan via AssumeRole.

Design:
- TargetAccount: one prospect AWS account (role_arn, external_id, regions).
- Collector: iterates targets, assumes role per target, scans regions,
  writes resources + observations + facts. Errors per region are
  collected, not fatal.
- Dependency injection for testability: assume_role_fn and scan_fn are
  injectable. Production uses defaults that hit real boto3.
- Dry-run: skip writes, still call AWS (validate IAM + region coverage).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
from constat_aws_rds.collector import (
    DEFAULT_REGIONS,
    collect_db_instances,
    db_to_facts,
    db_to_observation,
)
from sqlalchemy.orm import Session

from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import observations as observations_repo
from constat_api.repositories import resources as resources_repo
from constat_api.repositories import source_runs as source_runs_repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TargetAccount:
    """One prospect AWS account to scan.

    role_arn = None -> use the base session as-is (single-account mode).
    external_id = the shared secret configured in the prospect's trust policy.
    regions = None -> use the default set.
    """

    aws_account_id: str
    role_arn: str | None = None
    external_id: str | None = None
    name: str | None = None
    regions: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CollectionResult:
    aws_account_id: str
    regions_scanned: list[str]
    resources_written: int
    observations_written: int
    facts_written: int
    errors: list[str] = field(default_factory=list)


# Type aliases for injected callables.
AssumeRoleFn = Callable[[boto3.Session, TargetAccount], boto3.Session]
ScanFn = Callable[[boto3.Session, list[str]], Iterator[dict[str, Any]]]


def _assume_role(base_session: boto3.Session, target: TargetAccount) -> boto3.Session:
    """Default assume_role: STS AssumeRole with optional ExternalId."""
    if target.role_arn is None:
        return base_session

    sts = base_session.client("sts")
    kwargs: dict[str, Any] = {
        "RoleArn": target.role_arn,
        "RoleSessionName": f"constat-{uuid4()}",
        "DurationSeconds": 3600,
    }
    if target.external_id:
        kwargs["ExternalId"] = target.external_id

    response = sts.assume_role(**kwargs)
    creds = response["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def collect_target(
    session: Session,
    target: TargetAccount,
    *,
    base_session: boto3.Session,
    assume_role_fn: AssumeRoleFn | None = None,
    scan_fn: ScanFn | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> CollectionResult:
    """Scan one target: assume role, iterate regions, write resources/facts/observations.

    Per-region failures are recorded in result.errors; the scan continues.
    The caller owns the session transaction; this function flushes per region
    so partial progress survives.

    `assume_role_fn` and `scan_fn` use late-bound defaults (None -> look up the
    module-level default) so tests can patch them via `unittest.mock.patch`.

    `force=True` aborts any 'running' source_run in the same scope before
    starting a new one. Use this to recover from stuck runs after
    `cleanup_stuck_runs` failed to free the scope, or when you know the
    previous worker is dead.
    """
    if assume_role_fn is None:
        # Late binding: allows `patch("constat_api.collectors.aws._assume_role")`.
        assume_role_fn = _assume_role
    if scan_fn is None:
        # Late binding: same reason.
        scan_fn = collect_db_instances

    regions = list(target.regions) if target.regions else list(DEFAULT_REGIONS)
    aws_session = assume_role_fn(base_session, target)

    resources_written = 0
    observations_written = 0
    facts_written = 0
    errors: list[str] = []

    account = accounts_repo.get_or_create(session, target.aws_account_id, target.name)

    for region in regions:
        run = source_runs_repo.start_run(
            session,
            account_id=account.id,
            region=region,
            resource_type="AWS::RDS::DBInstance",
            source="aws_rds",
            force=force,
        )
        region_resources = 0
        region_error: str | None = None
        try:
            if run is None:
                # Another scan is already active for this scope. Skip to avoid
                # double-counting. We still log it as an error in the result.
                errors.append(f"{region}: scan already in progress")
                continue

            for db in scan_fn(aws_session, [region]):
                resource = resources_repo.upsert_resource(
                    session,
                    account.id,
                    region=region,
                    resource_type="AWS::RDS::DBInstance",
                    native_id=db["DBInstanceArn"],
                )

                observed_at = datetime.now(tz=UTC)

                if not dry_run:
                    obs = db_to_observation(resource.id, db, observed_at)
                    observations_repo.insert_observation(session, obs, source_run_id=run.id)

                    # `account_id` here is the INTERNAL account UUID stringified,
                    # because `facts.account_id` is a FK to accounts.id (UUID type).
                    facts = db_to_facts(resource.id, str(account.id), db, observed_at)
                    inserted, updated = facts_repo.upsert_facts(
                        session, facts, source_run_id=run.id
                    )
                    facts_written += inserted + updated
                    observations_written += 1

                resources_written += 1
                region_resources += 1

            if not dry_run:
                session.flush()

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            region_error = f"{error_code}: {e}"
            errors.append(f"{region}: {region_error}")
            logger.warning("Region %s failed: %s", region, e)
        finally:
            if run is not None:
                status = "success" if region_error is None else "failed"
                source_runs_repo.finish_run(
                    session,
                    run,
                    status=status,
                    resources_found=region_resources,
                    error=region_error,
                )
                # On successful scans, retire resources in this scope that
                # weren't seen in this run. This is the GTM promise:
                # "we never claim a resource is alive without proof".
                if status == "success" and not dry_run:
                    try:
                        retired = resources_repo.retire_stale_resources(
                            session,
                            account_id=account.id,
                            region=region,
                            resource_type="AWS::RDS::DBInstance",
                            source="aws_rds",
                        )
                        if retired:
                            logger.info("Region %s: retired %d stale resources", region, retired)
                    except Exception:
                        # Retirement is best-effort: a failure here must
                        # not turn a successful scan into a failed one.
                        logger.exception("Region %s: retirement sweep raised", region)

    if not dry_run:
        session.commit()

    return CollectionResult(
        aws_account_id=target.aws_account_id,
        regions_scanned=regions,
        resources_written=resources_written,
        observations_written=observations_written,
        facts_written=facts_written,
        errors=errors,
    )


def collect_targets(
    session: Session,
    targets: list[TargetAccount],
    *,
    base_session: boto3.Session,
    assume_role_fn: AssumeRoleFn | None = None,
    scan_fn: ScanFn | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> list[CollectionResult]:
    """Collect across multiple targets. One target's failure does not stop the others."""
    if assume_role_fn is None:
        assume_role_fn = _assume_role
    if scan_fn is None:
        scan_fn = collect_db_instances
    results: list[CollectionResult] = []
    for target in targets:
        try:
            result = collect_target(
                session,
                target,
                base_session=base_session,
                assume_role_fn=assume_role_fn,
                scan_fn=scan_fn,
                dry_run=dry_run,
                force=force,
            )
            results.append(result)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error("Target %s failed at AssumeRole: %s", target.aws_account_id, e)
            results.append(
                CollectionResult(
                    aws_account_id=target.aws_account_id,
                    regions_scanned=list(target.regions)
                    if target.regions
                    else list(DEFAULT_REGIONS),
                    resources_written=0,
                    observations_written=0,
                    facts_written=0,
                    errors=[f"assume_role: {error_code}: {e}"],
                )
            )
    return results
