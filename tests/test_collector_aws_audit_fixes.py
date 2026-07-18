"""Proof tests for the audit findings fixed in the AWS collector.

- F-01: a non-ClientError (e.g. ReadTimeoutError) mid-scan must mark the
  run 'failed' and must NOT trigger the retirement sweep.
- F-08: a resource is retired only after TWO consecutive successful
  scans both missed it.
- F-06: role_arn without external_id is rejected before calling STS
  (confused-deputy defense, collector side).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ReadTimeoutError
from constat_api.collectors.aws import TargetAccount, _assume_role, collect_target
from constat_api.orm import ResourceORM, SourceRunORM
from sqlalchemy.orm import Session

from tests.conftest import make_rds_db_dict

_REGION = "eu-west-1"


def _no_assume_role(base_session: Any, target: TargetAccount) -> Any:
    return base_session


def _scan_with(arns: list[str]):
    """Build a scan_fn that yields one DB instance per (region, arn) pair."""

    def _scan(s: Any, regions: list[str]):
        for region in regions:
            for arn in arns:
                yield {"_region": region, **make_rds_db_dict(arn=arn)}

    return _scan


def _target() -> TargetAccount:
    return TargetAccount(aws_account_id="111111111111", regions=(_REGION,))


def _collect(session: Session, target: TargetAccount, scan_fn: Any, **kwargs: Any) -> Any:
    return collect_target(
        session,
        target,
        base_session=MagicMock(),
        assume_role_fn=_no_assume_role,
        scan_fn=scan_fn,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# F-01: an exception escaping the ClientError handler must fail the run.
# ---------------------------------------------------------------------------


def test_f01_mid_scan_timeout_fails_run_without_retirement(session: Session) -> None:
    """scan_fn yields N resources then raises ReadTimeoutError -> run
    'failed', 0 retirements, the N upserted resources bumped."""
    target = _target()

    # Two successful runs, so the retirement sweep WOULD fire if it ran.
    # Run A sees arn:stale + arn:pre; run B sees only arn:pre.
    _collect(session, target, _scan_with(["arn:stale", "arn:pre"]))
    _collect(session, target, _scan_with(["arn:pre"]))

    stale = session.query(ResourceORM).filter_by(native_id="arn:stale").one()
    pre = session.query(ResourceORM).filter_by(native_id="arn:pre").one()
    assert stale.retired_at is None
    # Backdate both: a wrongful retirement sweep on the next run would
    # retire arn:stale, and a last_seen bump on arn:pre stays visible
    # despite sqlite's second-precision timestamps.
    stale.last_seen_at = datetime.now(tz=UTC) - timedelta(hours=1)
    pre.last_seen_at = datetime.now(tz=UTC) - timedelta(hours=1)
    session.commit()
    pre_last_seen_before = pre.last_seen_at

    def _timeout_scan(s: Any, regions: list[str]):
        yield {"_region": regions[0], **make_rds_db_dict(arn="arn:pre")}
        raise ReadTimeoutError(endpoint_url="https://rds.eu-west-1.amazonaws.com")

    result = _collect(session, target, _timeout_scan)

    # The resource yielded before the raise was written...
    assert result.resources_written == 1
    assert any("Timeout" in e for e in result.errors)

    # ...but the run is FAILED, not success (pre-fix it was 'success').
    # (Filter by status rather than ordering by started_at: sqlite stores
    # these timestamps with second precision, so "latest" is ambiguous.)
    runs = session.query(SourceRunORM).all()
    failed = [r for r in runs if r.status == "failed"]
    assert len(failed) == 1
    assert "Timeout" in (failed[0].error or "")

    session.refresh(stale)
    session.refresh(pre)
    # No retirement on a failed run: arn:stale is still active.
    assert stale.retired_at is None
    # The N upserted resources had their last_seen_at bumped before the raise.
    assert pre.last_seen_at > pre_last_seen_before


def test_f01_unexpected_exception_still_fails_the_run(session: Session) -> None:
    """An exception type caught by NO except block escapes collect_target,
    but the finally block still marks the run 'failed' (scan_completed
    stays False) and never runs the retirement sweep."""
    target = _target()

    def _buggy_scan(s: Any, regions: list[str]):
        raise RuntimeError("unexpected bug in the connector")
        yield  # pragma: no cover - makes this a generator

    with pytest.raises(RuntimeError, match="unexpected bug"):
        _collect(session, target, _buggy_scan)

    run = session.query(SourceRunORM).one()
    assert run.status == "failed"


def test_f01_botocore_error_counts_toward_circuit_breaker(session: Session) -> None:
    """BotoCoreError subtypes (timeouts, connection errors) feed the
    circuit breaker exactly like ClientError."""
    target = TargetAccount(aws_account_id="111111111111", regions=("r1", "r2", "r3"))

    def _scan(s: Any, regions: list[str]):
        for region in regions:
            if region in ("r1", "r2"):
                raise ReadTimeoutError(endpoint_url=f"https://rds.{region}.amazonaws.com")
            yield {"_region": region, **make_rds_db_dict()}

    result = _collect(session, target, _scan, max_consecutive_region_errors=2)

    assert result.resources_written == 0
    assert result.regions_skipped_by_breaker == ["r3"]
    real_errors = [e for e in result.errors if "circuit breaker" not in e]
    assert len(real_errors) == 2
    assert all("Timeout" in e for e in real_errors)


def test_f01_error_classification_buckets(session: Session) -> None:
    """ClientError codes are bucketed into AccessDenied / Throttling /
    Unknown in the recorded error string."""
    target = TargetAccount(aws_account_id="111111111111", regions=("deny", "slow", "weird"))
    from botocore.exceptions import ClientError

    codes = {"deny": "AccessDeniedException", "slow": "ThrottlingException", "weird": "Weird"}

    def _scan(s: Any, regions: list[str]):
        for region in regions:
            raise ClientError(
                {"Error": {"Code": codes[region], "Message": "x"}},
                "DescribeDBInstances",
            )
            yield  # pragma: no cover - unreachable, keeps this a generator

    result = _collect(session, target, _scan, max_consecutive_region_errors=100)

    assert any("AccessDenied (AccessDeniedException)" in e for e in result.errors)
    assert any("Throttling (ThrottlingException)" in e for e in result.errors)
    assert any("Unknown (Weird)" in e for e in result.errors)


# ---------------------------------------------------------------------------
# F-08: retirement requires two consecutive misses.
# ---------------------------------------------------------------------------


def test_f08_resource_retired_only_after_two_consecutive_misses(session: Session) -> None:
    """Missing from scan 1 -> still active; missing again in scan 2 ->
    retired."""
    target = _target()

    _collect(session, target, _scan_with(["arn:1", "arn:2"]))  # run A
    _collect(session, target, _scan_with(["arn:2"]))  # run B: misses arn:1 once

    r1 = session.query(ResourceORM).filter_by(native_id="arn:1").one()
    assert r1.retired_at is None, "missed by only one of the two latest successful runs"

    # Backdate so the comparison against run B's started_at is
    # deterministic (sqlite stores started_at with second precision).
    r1.last_seen_at = datetime.now(tz=UTC) - timedelta(hours=1)
    session.commit()

    _collect(session, target, _scan_with(["arn:2"]))  # run C: misses arn:1 again

    session.refresh(r1)
    assert r1.retired_at is not None, "missed by the two latest successful runs"
    r2 = session.query(ResourceORM).filter_by(native_id="arn:2").one()
    assert r2.retired_at is None


# ---------------------------------------------------------------------------
# F-06: role_arn without external_id is refused before STS.
# ---------------------------------------------------------------------------


def test_f06_role_arn_without_external_id_raises(session: Session) -> None:
    """collect_target with the real _assume_role refuses a role_arn
    without external_id before any STS call."""
    target = TargetAccount(
        aws_account_id="111111111111",
        role_arn="arn:aws:iam::111111111111:role/ConstatReadOnly",
        regions=(_REGION,),
    )
    with pytest.raises(ValueError, match="external_id"):
        collect_target(
            session,
            target,
            base_session=MagicMock(),
            scan_fn=_scan_with([]),
        )


def test_f06_empty_external_id_also_rejected() -> None:
    target = TargetAccount(
        aws_account_id="111111111111",
        role_arn="arn:aws:iam::111111111111:role/ConstatReadOnly",
        external_id="",
    )
    with pytest.raises(ValueError, match="external_id"):
        _assume_role(MagicMock(), target)


def test_f06_assume_role_passes_external_id_to_sts() -> None:
    """Happy path: a target with role_arn + external_id assumes the role
    and forwards the ExternalId to STS."""
    base = MagicMock()
    base.client.return_value.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "AK",
            "SecretAccessKey": "SK",
            "SessionToken": "ST",
        }
    }
    target = TargetAccount(
        aws_account_id="111111111111",
        role_arn="arn:aws:iam::111111111111:role/ConstatReadOnly",
        external_id="shared-secret",
    )

    _assume_role(base, target)

    base.client.assert_called_once_with("sts")
    sts_kwargs = base.client.return_value.assume_role.call_args.kwargs
    assert sts_kwargs["RoleArn"] == target.role_arn
    assert sts_kwargs["ExternalId"] == "shared-secret"
