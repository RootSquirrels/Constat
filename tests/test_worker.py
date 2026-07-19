"""Unit tests for the collection worker and the in-process queue.

Covers: drain semantics (ack on success, nack with backoff on failure),
per-account bounded concurrency (1.2), the source_runs partial-unique-index
dedup under the worker, and job_id threading into source_runs.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import EndpointConnectionError
from constat_api.collect_queue import InProcessQueue, QueueFullError, WorkItem
from constat_api.orm import InsightRunORM, SourceRunORM
from constat_api.repositories import accounts as accounts_repo
from constat_api.repositories import collect_jobs as collect_jobs_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.worker import (
    NACK_BACKOFF_BASE_SECONDS,
    NACK_BACKOFF_MAX_SECONDS,
    PerAccountLimiter,
    _backoff_seconds,
    drain_once,
)
from sqlalchemy.orm import Session

from tests.conftest import make_rds_db_dict


def _item(
    account: str = "111111111111", region: str = "eu-west-1", job_id: UUID | None = None
) -> WorkItem:
    # Explicit rds-only scope (SRE-2b changed the collector default to ALL
    # registered jobs): these unit tests mock only the RDS scan. The
    # all-jobs default is covered end-to-end in test_collect_async.py.
    return WorkItem(
        job_id=job_id or uuid4(),
        aws_account_id=account,
        region=region,
        resource_types=("rds",),
    )


def _make_job(session: Session, total_items: int = 1) -> UUID:
    """Persist a collect_jobs row and return its id.

    The worker's orphan reconciliation (SRE-4) ack-drops any item whose
    job row does not exist, so drain tests against a real session must
    create the job first — exactly like POST /collect/aws does.
    """
    job = collect_jobs_repo.create_job(session, actor="test", total_items=total_items, summary={})
    session.commit()
    return job.job_id


def _collector_patches(scan_return=None):
    if scan_return is None:
        scan_return = iter([{"_region": "eu-west-1", **make_rds_db_dict()}])
    return (
        patch(
            "constat_api.collectors.aws._assume_role",
            side_effect=lambda base, target: base,
        ),
        patch(
            "constat_api.collectors.aws.collect_db_instances",
            return_value=scan_return,
        ),
    )


# ---------------------------------------------------------------------------
# InProcessQueue mechanics
# ---------------------------------------------------------------------------


def test_inprocess_queue_send_receive_ack() -> None:
    q = InProcessQueue(maxsize=10)
    item = _item()
    q.send([item])
    received = q.receive(max_items=1, wait_seconds=0)
    assert len(received) == 1
    assert received[0].item == item
    assert received[0].attempts == 1
    q.ack(received[0].receipt)
    # Acked items are gone.
    assert q.receive(max_items=1, wait_seconds=0) == []


def test_inprocess_queue_full_raises() -> None:
    q = InProcessQueue(maxsize=1)
    q.send([_item()])
    with pytest.raises(QueueFullError):
        q.send([_item(region="eu-central-1")])


def test_inprocess_queue_nack_requeues_with_delay() -> None:
    q = InProcessQueue(maxsize=10)
    q.send([_item()])
    received = q.receive(max_items=1, wait_seconds=0)
    q.nack(received[0].receipt, delay_seconds=60)
    # Not visible again before the delay expires (no real sleep: the
    # not-before timestamp is 60s out, so an immediate poll is empty).
    assert q.receive(max_items=1, wait_seconds=0) == []


def test_inprocess_queue_double_ack_is_noop() -> None:
    q = InProcessQueue(maxsize=10)
    q.send([_item()])
    received = q.receive(max_items=1, wait_seconds=0)
    q.ack(received[0].receipt)
    q.ack(received[0].receipt)  # logged, not raised


def test_work_item_json_round_trip() -> None:
    item = WorkItem(
        job_id=uuid4(),
        aws_account_id="111111111111",
        role_arn="arn:aws:iam::111111111111:role/ConstatReadOnly",
        external_id="secret",
        name="prod",
        region="eu-west-1",
        resource_types=("rds", "ec2_volume"),
        force=True,
        dry_run=True,
    )
    assert WorkItem.from_dict(item.to_dict()) == item


# ---------------------------------------------------------------------------
# drain_once semantics
# ---------------------------------------------------------------------------


def test_drain_success_acks_and_writes_job_id(session: Session) -> None:
    """A successful item is acked; its source_run carries the job_id."""
    q = InProcessQueue(maxsize=10)
    item = _item(job_id=_make_job(session))
    q.send([item])
    p_assume, p_scan = _collector_patches()
    with p_assume, p_scan:
        outcomes = drain_once(lambda: session, q, base_session=MagicMock())

    assert [o.status for o in outcomes] == ["success"]
    assert q.receive(max_items=1, wait_seconds=0) == []  # acked, not requeued
    run = session.query(SourceRunORM).one()
    assert run.job_id == item.job_id
    assert run.status == "success"


def test_drain_region_error_nacks_with_backoff(session: Session) -> None:
    """A region error (BotoCoreError family) -> failed outcome + nack; the
    failed source_run is still recorded with the job_id."""
    q = InProcessQueue(maxsize=10)
    item = _item(job_id=_make_job(session))
    q.send([item])

    def _boom(_session, _regions):
        raise EndpointConnectionError(endpoint_url="https://rds.eu-west-1.amazonaws.com")

    with (
        patch(
            "constat_api.collectors.aws._assume_role",
            side_effect=lambda base, target: base,
        ),
        patch("constat_api.collectors.aws.collect_db_instances", side_effect=_boom),
    ):
        outcomes = drain_once(lambda: session, q, base_session=MagicMock())

    assert [o.status for o in outcomes] == ["failed"]
    assert outcomes[0].errors
    # Nacked with a future not-before: an immediate re-poll is empty.
    assert q.receive(max_items=1, wait_seconds=0) == []
    run = session.query(SourceRunORM).one()
    assert run.status == "failed"
    assert run.job_id == item.job_id


def test_drain_collector_exception_isolated_per_item(session: Session) -> None:
    """1.1 AC: an exception in one item never affects the next item."""
    q = InProcessQueue(maxsize=10)
    job_id = _make_job(session, total_items=2)
    q.send(
        [_item(account="111111111111", job_id=job_id), _item(account="222222222222", job_id=job_id)]
    )

    def _explode(*_args, **_kwargs):
        raise RuntimeError("worker bug")

    # First item explodes inside collect_target; second uses the real one.
    real_collect = __import__(
        "constat_api.collectors.aws", fromlist=["collect_target"]
    ).collect_target
    calls = {"n": 0}

    def _flaky_collect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _explode()
        return real_collect(*args, **kwargs)

    p_assume, p_scan = _collector_patches()
    with (
        p_assume,
        p_scan,
        patch(
            "constat_api.collectors.aws.collect_target",
            side_effect=_flaky_collect,
        ),
    ):
        outcomes = drain_once(lambda: session, q, max_items=2, base_session=MagicMock())

    by_account = {o.item.aws_account_id: o for o in outcomes}
    assert by_account["111111111111"].status == "failed"
    assert "worker bug" in by_account["111111111111"].errors[0]
    assert by_account["222222222222"].status == "success"


def test_backoff_grows_and_caps() -> None:
    assert _backoff_seconds(1) == NACK_BACKOFF_BASE_SECONDS
    assert _backoff_seconds(2) == 2 * NACK_BACKOFF_BASE_SECONDS
    assert _backoff_seconds(1000) == NACK_BACKOFF_MAX_SECONDS


# ---------------------------------------------------------------------------
# Dedup: the source_runs partial unique index is still the arbiter
# ---------------------------------------------------------------------------


def test_drain_duplicate_scope_reports_scan_already_in_progress(session: Session) -> None:
    """Two items for the same scope: the loser gets 'scan already in
    progress' from the partial unique index (roadmap dedup), as a failed
    outcome to retry later."""
    account = accounts_repo.get_or_create(session, "111111111111")
    active = source_runs_repo.start_run(
        session,
        account_id=account.id,
        region="eu-west-1",
        resource_type="AWS::RDS::DBInstance",
        source="aws_rds",
    )
    assert active is not None
    session.commit()

    q = InProcessQueue(maxsize=10)
    q.send([_item(job_id=_make_job(session))])
    p_assume, p_scan = _collector_patches()
    with p_assume, p_scan:
        outcomes = drain_once(lambda: session, q, base_session=MagicMock())

    assert [o.status for o in outcomes] == ["failed"]
    assert any("scan already in progress" in e for e in outcomes[0].errors)


# ---------------------------------------------------------------------------
# Per-account bounded concurrency (1.2)
# ---------------------------------------------------------------------------


def test_per_account_limiter_basic() -> None:
    limiter = PerAccountLimiter(1)
    assert limiter.try_acquire("111")
    assert not limiter.try_acquire("111")  # at cap
    assert limiter.try_acquire("222")  # different account: unaffected
    limiter.release("111")
    assert limiter.try_acquire("111")


def test_drain_defers_when_account_busy() -> None:
    """An item for an account at its cap is deferred (nacked, short delay),
    and the collector is never called for it."""
    q = InProcessQueue(maxsize=10)
    q.send([_item()])
    limiter = PerAccountLimiter(1)
    assert limiter.try_acquire("111111111111")  # simulate an in-flight sibling

    with patch("constat_api.collectors.aws.collect_target") as mock_collect:
        outcomes = drain_once(MagicMock, q, base_session=MagicMock(), limiter=limiter)

    assert [o.status for o in outcomes] == ["deferred"]
    mock_collect.assert_not_called()
    # Deferred item is requeued with a delay, not lost.
    assert q.receive(max_items=1, wait_seconds=0) == []


def test_per_account_cap_across_concurrent_drains() -> None:
    """Two threads draining two items for the SAME account with cap=1:
    the second item defers while the first is mid-scan. Synchronized with
    events, no real sleeps."""
    q = InProcessQueue(maxsize=10)
    q.send([_item(region="eu-west-1"), _item(region="eu-central-1")])
    limiter = PerAccountLimiter(1)
    scan_started = threading.Event()
    scan_release = threading.Event()
    thread_outcomes: list = []

    def _blocking_collect(*_args, **_kwargs):
        scan_started.set()
        assert scan_release.wait(timeout=10)
        return SimpleNamespace(errors=[])

    def _drain_in_thread() -> None:
        thread_outcomes.extend(
            drain_once(MagicMock, q, max_items=1, base_session=MagicMock(), limiter=limiter)
        )

    with patch(
        "constat_api.collectors.aws.collect_target",
        side_effect=_blocking_collect,
    ) as mock_collect:
        t = threading.Thread(target=_drain_in_thread)
        t.start()
        try:
            assert scan_started.wait(timeout=10)
            # Thread holds the account's only slot mid-scan: our drain of
            # the second item must defer, not run.
            outcomes = drain_once(
                MagicMock, q, max_items=1, base_session=MagicMock(), limiter=limiter
            )
            assert [o.status for o in outcomes] == ["deferred"]
        finally:
            scan_release.set()
            t.join(timeout=10)

    assert [o.status for o in thread_outcomes] == ["success"]
    # The collector ran exactly once: the deferred item never scanned.
    assert mock_collect.call_count == 1
    # The slot was released after the scan finished.
    assert limiter.in_flight("111111111111") == 0


# ---------------------------------------------------------------------------
# Orphan reconciliation (SRE-4)
# ---------------------------------------------------------------------------


def test_drain_drops_item_whose_job_row_is_gone(session: Session) -> None:
    """An item whose collect_jobs row does not exist can never be
    reconciled: the worker ack-drops it (no scan, no nack loop), logs,
    and counts constat_collect_orphan_items_total."""
    from constat_api.metrics import COLLECT_ORPHAN_ITEMS_TOTAL

    q = InProcessQueue(maxsize=10)
    q.send([_item()])  # random job_id, no job row
    orphans_before = COLLECT_ORPHAN_ITEMS_TOTAL._value.get()

    with patch("constat_api.collectors.aws.collect_target") as mock_collect:
        outcomes = drain_once(lambda: session, q, base_session=MagicMock())

    assert [o.status for o in outcomes] == ["dropped"]
    mock_collect.assert_not_called()  # never scanned
    assert q.receive(max_items=1, wait_seconds=0) == []  # acked, gone
    assert COLLECT_ORPHAN_ITEMS_TOTAL._value.get() == orphans_before + 1


# ---------------------------------------------------------------------------
# Collect -> evaluate chain (SRE-1b)
# ---------------------------------------------------------------------------


def _eol_scan(session, regions):
    """One PG12 instance per region: PG12 is past EOL but inside Extended
    Support (catalog: EOL 2025-02-28, force-upgrade 2028-02-29), so the
    rds_eol rule deterministically emits an insight."""
    for r in regions:
        yield {"_region": r, **make_rds_db_dict(engine_version="12.13")}


def test_completed_job_triggers_rule_evaluation(session: Session) -> None:
    """Draining the last (only) item of a job runs every registered rule:
    insights land in the DB and evaluation_status flips to 'success'."""
    from constat_api.orm import InsightORM

    q = InProcessQueue(maxsize=10)
    job_id = _make_job(session)
    q.send([_item(job_id=job_id)])
    with (
        patch(
            "constat_api.collectors.aws._assume_role",
            side_effect=lambda base, target: base,
        ),
        patch("constat_api.collectors.aws.collect_db_instances", side_effect=_eol_scan),
    ):
        outcomes = drain_once(lambda: session, q, base_session=MagicMock())

    assert [o.status for o in outcomes] == ["success"]
    # Every registered rule produced an insight_run row.
    run_rules = {r.rule_name for r in session.query(InsightRunORM).all()}
    from constat_api.insights.runner import RUNNERS

    assert run_rules == set(RUNNERS)
    # The seeded PG12 instance produced an rds_eol insight.
    insight_rules = {i.rule_name for i in session.query(InsightORM).all()}
    assert "rds_eol" in insight_rules
    # The job row records the chain outcome.
    ops = collect_jobs_repo.get_job_ops(session, job_id)
    assert ops is not None
    assert ops.evaluation_status == "success"


def test_evaluation_claim_is_atomic_no_rerun(session: Session) -> None:
    """A second drain / worker does NOT re-run evaluation for a completed
    job: the conditional UPDATE ... WHERE evaluation_status IS NULL
    admits exactly one winner."""
    from constat_api.worker import _maybe_run_evaluation

    q = InProcessQueue(maxsize=10)
    job_id = _make_job(session)
    q.send([_item(job_id=job_id)])
    p_assume, p_scan = _collector_patches()
    with p_assume, p_scan:
        drain_once(lambda: session, q, base_session=MagicMock())

    runs_after_first = session.query(InsightRunORM).count()
    assert runs_after_first > 0

    # A second worker reaching the same completed job loses the claim.
    assert collect_jobs_repo.try_claim_evaluation(session, job_id) is False
    session.rollback()
    _maybe_run_evaluation(lambda: session, job_id)
    assert session.query(InsightRunORM).count() == runs_after_first


def test_failed_rule_marks_evaluation_failed_and_drain_continues(
    session: Session,
) -> None:
    """A rule raising inside the chain flips evaluation_status to 'failed'
    but never crashes the drain loop: the item stays acked 'success'."""
    q = InProcessQueue(maxsize=10)
    job_id = _make_job(session)
    q.send([_item(job_id=job_id)])

    def _boom(*_args, **_kwargs):
        raise RuntimeError("rule exploded")

    p_assume, p_scan = _collector_patches()
    with (
        p_assume,
        p_scan,
        patch("constat_api.worker.run_rule", side_effect=_boom),
    ):
        outcomes = drain_once(lambda: session, q, base_session=MagicMock())

    assert [o.status for o in outcomes] == ["success"]  # item acked regardless
    ops = collect_jobs_repo.get_job_ops(session, job_id)
    assert ops is not None
    assert ops.evaluation_status == "failed"


def test_incomplete_job_does_not_trigger_evaluation(session: Session) -> None:
    """A job with 2 items where only 1 has been acked is not complete:
    no rule runs yet (the second item is still queued)."""
    q = InProcessQueue(maxsize=10)
    job_id = _make_job(session, total_items=2)
    # Only one of the two items is in the queue right now.
    q.send([_item(job_id=job_id, region="eu-west-1")])
    p_assume, p_scan = _collector_patches()
    with p_assume, p_scan:
        outcomes = drain_once(lambda: session, q, base_session=MagicMock())

    assert [o.status for o in outcomes] == ["success"]
    assert session.query(InsightRunORM).count() == 0
    ops = collect_jobs_repo.get_job_ops(session, job_id)
    assert ops is not None
    assert ops.evaluation_status is None
