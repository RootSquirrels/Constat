"""Collector resilience tests (roadmap scoreboard "Collecte & résilience").

Covers:
- adaptive retry mode (jittered backoff + client-side rate limiting on
  throttling) actually reaches the boto3 client creations, for both the
  RDS scan clients and the STS AssumeRole client;
- targeted single-region re-scan: after a multi-region scan where one
  region failed, re-running `collect_target` with
  `regions=(failed_region,)` touches only that region's source_runs.

Concurrency ("two scans on the same scope -> only one proceeds") is NOT
duplicated here: it is already covered end-to-end at the collect_target
level by `test_collector_returns_none_run_when_scan_in_progress` in
tests/test_source_runs.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError
from constat_api.collectors.aws import TargetAccount, _assume_role, collect_target
from constat_api.orm import SourceRunORM
from constat_aws_rds.collector import collect_db_instances
from constat_core.collectors.aws import ADAPTIVE_RETRY_CONFIG
from sqlalchemy.orm import Session

from tests.conftest import make_rds_db_dict


def _no_assume_role(base_session, target):
    return base_session


# ---------------------------------------------------------------------------
# Adaptive retry mode reaches client creation
# ---------------------------------------------------------------------------


def test_adaptive_retry_config_reaches_rds_client() -> None:
    """collect_db_instances passes the adaptive retry config to boto3."""
    boto_session = MagicMock()
    client = boto_session.client.return_value
    client.get_paginator.return_value.paginate.return_value = iter([])

    list(collect_db_instances(boto_session, regions=["eu-west-1"]))

    boto_session.client.assert_called_once_with(
        "rds", region_name="eu-west-1", config=ADAPTIVE_RETRY_CONFIG
    )
    config = boto_session.client.call_args.kwargs["config"]
    assert config.retries == {"mode": "adaptive", "max_attempts": 10}


def test_adaptive_retry_config_reaches_sts_client() -> None:
    """_assume_role applies the same adaptive retry config to the STS client."""
    base_session = MagicMock()
    base_session.client.return_value.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "AKIAFAKE",
            "SecretAccessKey": "fake",
            "SessionToken": "fake",
        }
    }
    target = TargetAccount(
        aws_account_id="111111111111",
        role_arn="arn:aws:iam::111111111111:role/constat-read",
        external_id="ext-123",
    )

    _assume_role(base_session, target)

    base_session.client.assert_called_once_with("sts", config=ADAPTIVE_RETRY_CONFIG)
    config = base_session.client.call_args.kwargs["config"]
    assert config.retries == {"mode": "adaptive", "max_attempts": 10}


# ---------------------------------------------------------------------------
# Targeted single-region re-scan
# ---------------------------------------------------------------------------


def test_targeted_rescan_touches_only_the_failed_region(session: Session) -> None:
    """Scan 2 regions (one fails), then re-scan only the failed region.

    The second run must create a source_run for the failed region only;
    the successful region's source_runs are left untouched.
    """
    target = TargetAccount(
        aws_account_id="111111111111",
        regions=("eu-west-1", "us-east-1"),
        # Explicit rds-only scope (SRE-2b changed the default to ALL jobs).
        resource_types=("rds",),
    )

    def _flaky_scan(aws_session, regions):
        for region in regions:
            if region == "eu-west-1":
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                    "DescribeDBInstances",
                )
            yield {"_region": region, **make_rds_db_dict()}

    result1 = collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_flaky_scan,
    )
    assert any("eu-west-1" in e for e in result1.errors)

    def _ok_scan(aws_session, regions):
        for region in regions:
            yield {"_region": region, **make_rds_db_dict()}

    # Re-scan ONLY the failed region.
    retry = TargetAccount(
        aws_account_id="111111111111", regions=("eu-west-1",), resource_types=("rds",)
    )
    result2 = collect_target(
        session,
        retry,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=_ok_scan,
    )

    assert result2.errors == []
    assert result2.regions_scanned == ["eu-west-1"]
    assert result2.resources_written == 1

    runs = session.query(SourceRunORM).all()
    assert len(runs) == 3  # failed eu-west-1, ok us-east-1, retried eu-west-1
    us_east_runs = [r for r in runs if r.region == "us-east-1"]
    eu_west_runs = [r for r in runs if r.region == "eu-west-1"]
    assert len(us_east_runs) == 1  # untouched by the re-scan
    assert us_east_runs[0].status == "success"
    assert sorted(r.status for r in eu_west_runs) == ["failed", "success"]
