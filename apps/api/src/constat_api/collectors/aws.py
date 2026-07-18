"""AWS collector: cross-account scan via AssumeRole.

Generalized to support multiple AWS resource types (RDS, EC2/EBS, ...)
through a per-target job list. Each job is one
(resource_type, source, scan_fn, factory) tuple; one source_run is
created per (region, job) per scan.

Design:
- TargetAccount: one prospect AWS account (role_arn, external_id,
  regions, resource_types).
- Collector: iterates targets, assumes role per target, scans regions,
  writes resources + observations + facts. Errors per region/job are
  collected, not fatal.
- Dependency injection for testability: assume_role_fn and scan_fn are
  injectable. Production uses defaults that hit real boto3.
- Dry-run: skip writes, still call AWS (validate IAM + region coverage).
- Targeted re-scan: `TargetAccount.regions` scopes a run to any subset,
  so one failed region can be re-scanned without re-scanning the rest.
- Circuit breaker: after N consecutive region errors, skip the rest
  of the regions. The intuition: if 2 regions in a row hit AccessDenied
  (or worse, network errors), the rest of the regions are likely
  degraded too — don't burn minutes hammering them.

Adding a new AWS resource type = one entry in JOB_REGISTRY + a route
on the request side. The collector doesn't need to change otherwise.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from constat_aws_ec2.collector import (
    INSTANCE_RESOURCE_TYPE,
    SNAPSHOT_RESOURCE_TYPE,
    VOLUME_RESOURCE_TYPE,
    collect_instances,
    collect_snapshots,
    collect_volumes,
    instance_to_observation,
    instance_to_resource,
    snapshot_to_observation,
    snapshot_to_resource,
    volume_to_facts,
    volume_to_observation,
    volume_to_resource,
)
from constat_aws_ec2.collector import (
    SOURCE_NAME as EC2_SOURCE_NAME,
)
from constat_aws_rds.collector import (
    ADAPTIVE_RETRY_CONFIG,
    DEFAULT_REGIONS,
    collect_db_instances,
    db_to_facts,
    db_to_observation,
    db_to_resource,
)
from constat_core.models import Fact, Observation, Resource
from sqlalchemy.orm import Session

from constat_api.metrics import record_source_run
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import observations as observations_repo
from constat_api.repositories import resources as resources_repo
from constat_api.repositories import source_runs as source_runs_repo

logger = logging.getLogger(__name__)

# V1 default: after 2 consecutive region errors, the rest of the regions
# are likely degraded too. Skip them and let the operator decide whether
# to re-run. Tunable per call.
DEFAULT_MAX_CONSECUTIVE_REGION_ERRORS = 2


@dataclass(frozen=True)
class TargetAccount:
    """One prospect AWS account to scan.

    role_arn = None -> use the base session as-is (single-account mode).
    external_id = the shared secret configured in the prospect's trust policy.
        REQUIRED when role_arn is set (F-06: no ExternalId = confused-deputy
        risk); `_assume_role` refuses to call STS without it.
    regions = None -> use the default set.
    resource_types = None -> scan all registered jobs (V1 backward compat).
        A list like ["rds"] or ["ec2_volume"] scopes the scan to one
        resource type, so a prospect who only wants EBS insights doesn't
        pay the RDS scan cost.
    """

    aws_account_id: str
    role_arn: str | None = None
    external_id: str | None = None
    name: str | None = None
    regions: tuple[str, ...] | None = None
    resource_types: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CollectionResult:
    aws_account_id: str
    regions_scanned: list[str]
    resources_written: int
    observations_written: int
    facts_written: int
    errors: list[str] = field(default_factory=list)
    # Regions skipped because the circuit breaker tripped. They were
    # never scanned, so no source_run was created for them.
    regions_skipped_by_breaker: list[str] = field(default_factory=list)


# Type aliases for injected callables.
AssumeRoleFn = Callable[[boto3.Session, TargetAccount], boto3.Session]
ScanFn = Callable[[boto3.Session, list[str]], Iterator[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Scan job registry
#
# Each entry maps a short key (used in TargetAccount.resource_types and in
# API request bodies) to a job: (resource_type, source, scan_fn, factories).
# The collector iterates jobs per region. Adding a new resource type = one
# new entry here + a route in the API.
#
# Factories:
# - resource_factory(raw_dict, account_id) -> canonical Resource
# - observation_factory(resource_id, raw_dict, observed_at) -> Observation
#   (optional: None for resource types where the rule reads facts only,
#   no observation is needed — e.g. if the raw payload would be huge)
# - facts_factory(resource_id, account_id, raw_dict, observed_at) -> list[Fact]
#   (optional: None for resource types with no facts yet)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanJob:
    """One (resource_type, source) scan unit.

    A region scan iterates the job's scan_fn and writes one source_run
    per (region, job) pair. Two jobs on the same region get two
    source_runs (different resource_types, possibly different sources).
    """

    key: str  # short id, used in API requests and TargetAccount.resource_types
    resource_type: str
    source: str
    scan_fn: ScanFn
    resource_factory: Callable[[dict[str, Any], str], Resource]
    observation_factory: Callable[[UUID, dict[str, Any], datetime], Observation] | None = None
    facts_factory: Callable[[UUID, str, dict[str, Any], datetime], list[Fact]] | None = None


JOB_REGISTRY: dict[str, ScanJob] = {
    "rds": ScanJob(
        key="rds",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
        scan_fn=collect_db_instances,
        resource_factory=db_to_resource,
        observation_factory=db_to_observation,
        facts_factory=db_to_facts,
    ),
    "ec2_volume": ScanJob(
        key="ec2_volume",
        resource_type=VOLUME_RESOURCE_TYPE,
        source=EC2_SOURCE_NAME,
        scan_fn=collect_volumes,
        resource_factory=volume_to_resource,
        observation_factory=volume_to_observation,
        facts_factory=volume_to_facts,
    ),
    "ec2_snapshot": ScanJob(
        key="ec2_snapshot",
        resource_type=SNAPSHOT_RESOURCE_TYPE,
        source=EC2_SOURCE_NAME,
        scan_fn=collect_snapshots,
        resource_factory=snapshot_to_resource,
        observation_factory=snapshot_to_observation,
        facts_factory=None,  # no facts yet for snapshots
    ),
    "ec2_instance": ScanJob(
        key="ec2_instance",
        resource_type=INSTANCE_RESOURCE_TYPE,
        source=EC2_SOURCE_NAME,
        scan_fn=collect_instances,
        resource_factory=instance_to_resource,
        observation_factory=instance_to_observation,
        facts_factory=None,  # no facts yet for instances
    ),
}


def _classify_error(code: str) -> str:
    """Bucket an AWS error code (or exception class name) into a coarse class.

    Operators triage by class, not by the dozens of individual codes:
    AccessDenied -> fix the trust policy; Throttling/Timeout -> transient,
    retry later; Unknown -> investigate. Used in the recorded error string
    so the source_runs row is self-explanatory.
    """
    lowered = code.lower()
    if "accessdenied" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
        return "AccessDenied"
    if "throttl" in lowered or "limitexceeded" in lowered or "toomanyrequests" in lowered:
        return "Throttling"
    if "timeout" in lowered or "timedout" in lowered:
        return "Timeout"
    return "Unknown"


def _assume_role(base_session: boto3.Session, target: TargetAccount) -> boto3.Session:
    """Default assume_role: STS AssumeRole with a mandatory ExternalId."""
    if target.role_arn is None:
        return base_session

    if not target.external_id:
        # Confused-deputy defense (F-06): assuming a cross-account role
        # without an ExternalId means anyone who learns the role ARN can
        # assume it. Refuse BEFORE calling STS — the API router validates
        # this too, but the collector is the last line of defense.
        raise ValueError(
            f"TargetAccount {target.aws_account_id}: role_arn is set but "
            "external_id is empty — refusing to assume a cross-account role "
            "without an ExternalId (confused-deputy risk)."
        )

    # Same adaptive retry policy as the RDS scan clients (shared constant
    # from constat_aws_rds.collector): STS throttles too, and a failed
    # AssumeRole fails the whole target, not just one region.
    sts = base_session.client("sts", config=ADAPTIVE_RETRY_CONFIG)
    kwargs: dict[str, Any] = {
        "RoleArn": target.role_arn,
        "RoleSessionName": f"constat-{uuid4()}",
        "DurationSeconds": 3600,
        "ExternalId": target.external_id,
    }

    response = sts.assume_role(**kwargs)
    creds = response["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _resolve_jobs(target: TargetAccount, scan_fn_override: ScanFn | None) -> list[ScanJob]:
    """Resolve the list of scan jobs for a target.

    Rules:
    1. If `target.resource_types` is set, use those jobs from the registry.
    2. Otherwise, default to the RDS job (V1 backward compat).
    3. If a `scan_fn_override` is passed AND the RDS job is in the resolved
       list, build a one-off RDS job with the override scan_fn. This keeps
       the existing test path alive without requiring a registry patch.
    """
    if target.resource_types is not None:
        jobs: list[ScanJob] = []
        for rt in target.resource_types:
            if rt not in JOB_REGISTRY:
                raise ValueError(f"unknown resource_type '{rt}' (known: {sorted(JOB_REGISTRY)})")
            jobs.append(JOB_REGISTRY[rt])
    else:
        jobs = [JOB_REGISTRY["rds"]]

    if scan_fn_override is not None:
        # Legacy test path: override the RDS job's scan_fn. The other
        # jobs (EC2, etc.) keep their registry-default scan_fn.
        new_jobs: list[ScanJob] = []
        for j in jobs:
            if j.key == "rds":
                new_jobs.append(
                    ScanJob(
                        key=j.key,
                        resource_type=j.resource_type,
                        source=j.source,
                        scan_fn=scan_fn_override,
                        resource_factory=j.resource_factory,
                        observation_factory=j.observation_factory,
                        facts_factory=j.facts_factory,
                    )
                )
            else:
                new_jobs.append(j)
        jobs = new_jobs

    return jobs


def _native_id_for_job(job: ScanJob, raw: dict[str, Any]) -> str:
    """Extract the native_id from a raw item for upsert. Each job knows
    which field carries the cloud-unique id (ARN for RDS, VolumeId for
    EBS, SnapshotId for snapshots, InstanceId for EC2)."""
    if job.key == "rds":
        return raw["DBInstanceArn"]
    if job.key == "ec2_volume":
        return raw["VolumeId"]
    if job.key == "ec2_snapshot":
        return raw["SnapshotId"]
    if job.key == "ec2_instance":
        return raw["InstanceId"]
    raise ValueError(f"unknown job key: {job.key}")


def _run_region_job(
    session: Session,
    *,
    job: ScanJob,
    region: str,
    account_id: UUID,
    aws_session: boto3.Session,
    dry_run: bool,
    force: bool,
) -> tuple[int, int, int, str | None]:
    """Run one (region, job) pair. Returns (resources, observations, facts, error).

    Per-region failures are recorded in the returned error; the caller
    (collect_target) decides whether to skip the rest of the regions
    (circuit breaker).
    """
    run = source_runs_repo.start_run(
        session,
        account_id=account_id,
        region=region,
        resource_type=job.resource_type,
        source=job.source,
        force=force,
    )
    region_resources = 0
    region_observations = 0
    region_facts = 0
    region_error: str | None = None
    region_started = time.monotonic()
    # F-01: a run is 'success' ONLY if the scan loop ran to completion.
    # region_error alone is not enough: an exception type we don't catch
    # below would escape with region_error still None, and the finally
    # block would mislabel the run 'success' — then the retirement
    # sweep would "delete" resources that are actually alive.
    scan_completed = False

    try:
        if run is None:
            # Another scan is already active for this scope. Skip to avoid
            # double-counting. The caller records this as an error.
            region_error = "scan already in progress"
            return region_resources, region_observations, region_facts, region_error

        for raw in job.scan_fn(aws_session, [region]):
            native_id = _native_id_for_job(job, raw)
            resource = resources_repo.upsert_resource(
                session,
                account_id,
                region=region,
                resource_type=job.resource_type,
                native_id=native_id,
            )
            observed_at = datetime.now(tz=UTC)

            if not dry_run:
                if job.observation_factory is not None:
                    obs = job.observation_factory(resource.id, raw, observed_at)
                    observations_repo.insert_observation(session, obs, source_run_id=run.id)
                    region_observations += 1

                if job.facts_factory is not None:
                    # `account_id` here is the INTERNAL account UUID stringified,
                    # because `facts.account_id` is a FK to accounts.id (UUID type).
                    facts = job.facts_factory(resource.id, str(account_id), raw, observed_at)
                    inserted, updated = facts_repo.upsert_facts(
                        session, facts, source_run_id=run.id
                    )
                    region_facts += inserted + updated

            region_resources += 1

        if not dry_run:
            session.flush()

        # Scan completed without exception -> mark success.
        scan_completed = True

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_class = _classify_error(error_code)
        region_error = f"{error_class} ({error_code}): {e}"
        logger.warning("Region %s job %s failed: %s", region, job.key, e)
    except BotoCoreError as e:
        # BotoCoreError is NOT a ClientError: read/connect timeouts,
        # connection resets, endpoint failures. Caught separately
        # (F-01) so it (a) marks the run failed instead of escaping,
        # and (b) counts toward the circuit breaker like ClientError —
        # a network blip in 2 consecutive regions means the rest are
        # likely degraded too.
        error_class = _classify_error(type(e).__name__)
        region_error = f"{error_class} ({type(e).__name__}): {e}"
        logger.warning("Region %s job %s failed: %s", region, job.key, e)
    finally:
        if run is not None:
            # An exception that escapes BOTH except blocks (an
            # unexpected bug) still lands here with scan_completed
            # False -> the run is marked 'failed' and no retirement
            # happens. Only a fully completed, error-free scan is
            # a 'success'.
            status = "success" if (scan_completed and region_error is None) else "failed"
            source_runs_repo.finish_run(
                session,
                run,
                status=status,
                resources_found=region_resources,
                error=region_error,
            )
            record_source_run(
                region=region,
                status=status,
                duration_seconds=time.monotonic() - region_started,
            )
            # On successful scans, retire resources in this scope that
            # the TWO most recent successful runs both missed (F-08:
            # one scan is not proof of deletion — a transient gap
            # could otherwise "delete" live resources). This is the
            # GTM promise: "we never claim a resource is alive
            # without proof". scan_completed is redundant with
            # status == "success" but states the invariant explicitly.
            if status == "success" and scan_completed and not dry_run:
                try:
                    retired = resources_repo.retire_stale_resources(
                        session,
                        account_id=account_id,
                        region=region,
                        resource_type=job.resource_type,
                        source=job.source,
                    )
                    if retired:
                        logger.info(
                            "Region %s job %s: retired %d stale resources",
                            region,
                            job.key,
                            retired,
                        )
                except Exception:
                    # Retirement is best-effort: a failure here must
                    # not turn a successful scan into a failed one.
                    logger.exception(
                        "Region %s job %s: retirement sweep raised",
                        region,
                        job.key,
                    )

    return region_resources, region_observations, region_facts, region_error


def collect_target(
    session: Session,
    target: TargetAccount,
    *,
    base_session: boto3.Session,
    assume_role_fn: AssumeRoleFn | None = None,
    scan_fn: ScanFn | None = None,
    dry_run: bool = False,
    force: bool = False,
    max_consecutive_region_errors: int = DEFAULT_MAX_CONSECUTIVE_REGION_ERRORS,
) -> CollectionResult:
    """Scan one target: assume role, iterate regions/jobs, write resources/facts.

    The inner loop iterates `(region, job)` pairs. `target.resource_types`
    selects which jobs run (default: RDS only). Per-region failures are
    collected, not fatal; the circuit breaker trips after N consecutive
    region errors. The caller owns the session transaction; this function
    flushes per region so partial progress survives.

    `assume_role_fn` and `scan_fn` use late-bound defaults so tests can
    patch them via `unittest.mock.patch`. `scan_fn` (when set) overrides
    the RDS job's scan_fn — the legacy test path. Other jobs keep their
    registry-default scan_fn.

    `force=True` aborts any 'running' source_run in the same scope before
    starting a new one. Use this to recover from stuck runs after
    `cleanup_stuck_runs` failed to free the scope, or when you know the
    previous worker is dead.

    Circuit breaker: after `max_consecutive_region_errors` consecutive
    region failures, the rest of the regions are skipped (recorded in
    result.regions_skipped_by_breaker). A single successful region
    resets the counter. Default: 2.
    """
    if assume_role_fn is None:
        # Late binding: allows `patch("constat_api.collectors.aws._assume_role")`.
        assume_role_fn = _assume_role

    jobs = _resolve_jobs(target, scan_fn)

    regions = list(target.regions) if target.regions else list(DEFAULT_REGIONS)
    aws_session = assume_role_fn(base_session, target)

    resources_written = 0
    observations_written = 0
    facts_written = 0
    errors: list[str] = []
    regions_skipped: list[str] = []
    consecutive_errors = 0

    account = accounts_repo.get_or_create(session, target.aws_account_id, target.name)

    for region in regions:
        # Circuit breaker check (before any job starts: a skipped region
        # has no source_run, no flush, no retirement — it's a no-op).
        if consecutive_errors >= max_consecutive_region_errors:
            skip_msg = (
                f"skipped by circuit breaker "
                f"({consecutive_errors} consecutive errors >= "
                f"{max_consecutive_region_errors})"
            )
            errors.append(f"{region}: {skip_msg}")
            regions_skipped.append(region)
            logger.warning("Region %s %s", region, skip_msg)
            continue

        # Run all jobs in this region sequentially. A job failure counts
        # toward the breaker just like a region failure — same logic.
        # We DO NOT early-exit on first job failure, because one job
        # failing (e.g. EC2 snapshot API throttled) shouldn't block RDS.
        # We track per-job outcomes separately and report them all.
        region_had_error = False
        for job in jobs:
            n_res, n_obs, n_facts, err = _run_region_job(
                session,
                job=job,
                region=region,
                account_id=account.id,
                aws_session=aws_session,
                dry_run=dry_run,
                force=force,
            )
            resources_written += n_res
            observations_written += n_obs
            facts_written += n_facts
            if err is not None:
                errors.append(f"{region} ({job.key}): {err}")
                region_had_error = True

        # Circuit-breaker bookkeeping: any error in the region (from any
        # job) increments; a clean run on ALL jobs resets.
        if region_had_error:
            consecutive_errors += 1
        else:
            consecutive_errors = 0

    if not dry_run:
        # PII classification: label the customer identifiers we just
        # ingested. The hash, not the value, is stored. The classifier
        # is defensive — it returns None for empty values and raises
        # on disallowed sensitivities.
        from constat_api.pii import PIIClassifier

        pii = PIIClassifier(session)
        pii.record(
            resource_type="account",
            resource_id=target.aws_account_id,
            field_name="aws_account_id",
            value=target.aws_account_id,
        )
        if target.role_arn:
            pii.record(
                resource_type="account",
                resource_id=target.aws_account_id,
                field_name="arn",
                value=target.role_arn,
            )
        # Classify the resource native_ids (ARN for RDS).
        for region in regions:
            pii.record(
                resource_type="resource",
                resource_id=f"{target.aws_account_id}:{region}",
                field_name="region",
                value=region,
            )
        # Audit: log the scan. Actor defaults to "system" (the
        # collector runs in the API process; the API key actor is
        # set by the router, not by the collector). Metadata is
        # strictly non-PII: counts, region names, error counts.
        from constat_api.audit import record_event

        record_event(
            session,
            action="aws_scan_completed",
            actor="system:aws_collector",
            target_type="account",
            target_id=target.aws_account_id,
            metadata={
                "regions_scanned": len(regions),
                "regions_skipped_by_breaker": len(regions_skipped),
                "resources_written": resources_written,
                "observations_written": observations_written,
                "facts_written": facts_written,
                "errors_count": len(errors),
                "force": force,
                "dry_run": dry_run,
            },
        )
        session.commit()

    return CollectionResult(
        aws_account_id=target.aws_account_id,
        regions_scanned=regions,
        resources_written=resources_written,
        observations_written=observations_written,
        facts_written=facts_written,
        errors=errors,
        regions_skipped_by_breaker=regions_skipped,
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
    max_consecutive_region_errors: int = DEFAULT_MAX_CONSECUTIVE_REGION_ERRORS,
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
                max_consecutive_region_errors=max_consecutive_region_errors,
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
