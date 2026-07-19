"""Collection worker: drains the collect queue one region at a time.

One WorkItem = one AWS account x one region. The worker resolves it to a
single-region `collect_target` call, so all the audit-hardened invariants
(source_run per scope, retirement after two proofs, circuit breaker) are
unchanged — the only new wiring is `job_id` on the source_runs.

Failure model (1.1):
- Item processed without region errors -> ack.
- Region errors (throttling, AccessDenied, ...) -> nack with exponential
  backoff (30s x attempt, capped). The failed source_run is already
  recorded; the retry rebuilds it. On SQS the redrive policy caps total
  attempts and lands poison items on the DLQ (Terraform's side).
- Exception escaping the collector (bug, DB down) -> same nack path.
  One item's failure NEVER affects the others: each item gets its own
  session and its own try/except.
- "scan already in progress" (the source_runs partial unique index, the
  roadmap's dedup) is a region error like any other: nacked, retried
  later, and by then the in-flight scan has finished.

Per-account bounded concurrency (1.2): `PerAccountLimiter` caps in-flight
items per aws_account_id at CONSTAT_WORKER_PER_ACCOUNT — AWS API quotas
are per-account, so parallelizing ACROSS accounts is safe but hammering
one account from several threads is not. An item for a busy account is
nacked with a short delay (not a failure): it will be picked up when the
account frees a slot.

Entry points:
- API lifespan (inline mode): `start_worker_pool` on the in-process queue.
- `python -m constat_api.worker` (sqs mode): standalone drain loop with
  graceful SIGTERM — this is what the external ECS worker service runs.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from constat_api.collect_queue import ReceivedItem, WorkItem, WorkQueue, get_queue
from constat_api.collectors import aws as aws_collector
from constat_api.collectors.aws import TargetAccount
from constat_api.metrics import (
    record_collect_item,
    set_collect_items_in_flight,
)
from constat_api.settings import settings
from constat_api.tenant import bind_tenant

logger = logging.getLogger(__name__)

# Nack backoff: 30s x attempt, capped at 5 min. Region scans fail mostly
# on transient throttling; 30s is one SQS long-poll generation, and the
# cap keeps a persistently-failing item cheap without ever dropping it.
NACK_BACKOFF_BASE_SECONDS = 30
NACK_BACKOFF_MAX_SECONDS = 300

# Requeue delay when the account already has its per-account cap of items
# in flight. Short: the slot frees as soon as a sibling region finishes.
BUSY_ACCOUNT_DELAY_SECONDS = 5

SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class ItemOutcome:
    """What drain_once did with one received item.

    status: "success" (acked), "failed" (nacked after errors/exception),
    "deferred" (nacked, per-account cap reached — not a failure).
    """

    item: WorkItem
    status: str
    errors: tuple[str, ...] = field(default_factory=tuple)


class PerAccountLimiter:
    """In-flight registry keyed by aws_account_id (1.2).

    A dict + a lock, shared by every worker thread in the process. No
    external state: in sqs mode each ECS worker task self-limits, which
    is correct while per_account <= tasks... is NOT guaranteed; the cap
    is enforced per worker PROCESS. Cross-task coordination would need
    DynamoDB/Redis — deliberately out of scope for V1 (the task count is
    Terraform's knob; keep CONSTAT_WORKER_PER_ACCOUNT conservative).
    """

    def __init__(self, max_per_account: int) -> None:
        if max_per_account < 1:
            raise ValueError("max_per_account must be >= 1")
        self._max = max_per_account
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def try_acquire(self, aws_account_id: str) -> bool:
        """Take a slot for the account, or return False when at the cap."""
        with self._lock:
            current = self._counts.get(aws_account_id, 0)
            if current >= self._max:
                return False
            self._counts[aws_account_id] = current + 1
            return True

    def release(self, aws_account_id: str) -> None:
        """Free a slot. Must be called exactly once per successful acquire."""
        with self._lock:
            current = self._counts.get(aws_account_id, 0)
            if current <= 1:
                self._counts.pop(aws_account_id, None)
            else:
                self._counts[aws_account_id] = current - 1

    def in_flight(self, aws_account_id: str) -> int:
        """Current in-flight count for an account. Observability + tests."""
        with self._lock:
            return self._counts.get(aws_account_id, 0)


def _backoff_seconds(attempts: int) -> int:
    """30s x attempt, capped. attempts is the delivery count (>= 1)."""
    return min(NACK_BACKOFF_BASE_SECONDS * max(attempts, 1), NACK_BACKOFF_MAX_SECONDS)


def _process_item(
    session_factory: SessionFactory,
    received: ReceivedItem,
    *,
    base_session: Any,
) -> ItemOutcome:
    """Run the collector for one item's single region.

    Returns the outcome; does NOT ack/nack — the caller owns the queue
    side so the ack/nack decision stays in one place.
    """
    item = received.item
    target = TargetAccount(
        aws_account_id=item.aws_account_id,
        role_arn=item.role_arn,
        external_id=item.external_id,
        name=item.name,
        regions=(item.region,),
        resource_types=item.resource_types,
    )
    session = session_factory()
    try:
        # Same tenant binding as the get_db dependency: without it, the
        # RLS GUC is unset on Postgres and every write is denied.
        bind_tenant(session, settings.default_tenant_id)
        result = aws_collector.collect_target(
            session,
            target,
            base_session=base_session,
            # Late-bound module attribute: tests patch
            # constat_api.collectors.aws.collect_db_instances, and the
            # override only applies to the RDS job (legacy test path,
            # same as collect_targets).
            scan_fn=aws_collector.collect_db_instances,
            dry_run=item.dry_run,
            force=item.force,
            job_id=item.job_id,
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return ItemOutcome(
        item=item, status="success" if not result.errors else "failed", errors=tuple(result.errors)
    )


def drain_once(
    session_factory: SessionFactory,
    queue: WorkQueue,
    *,
    max_items: int = 1,
    wait_seconds: int = 0,
    base_session: Any | None = None,
    limiter: PerAccountLimiter | None = None,
) -> list[ItemOutcome]:
    """Receive up to max_items and process each one independently.

    This is the unit tests drive directly (deterministic, no sleeps):
    POST enqueues, `drain_once(session_factory, get_queue())` runs the
    scans synchronously in the test thread.

    A shared `limiter` enforces the per-account cap across threads; when
    None, a fresh limiter is used (single-threaded drain, cap never hit).
    One item's failure never affects the others (1.1 AC): each item is
    acked or nacked on its own outcome.
    """
    if base_session is None:
        # Late import + call: tests inject a MagicMock instead.
        from constat_api.settings import get_base_aws_session

        base_session = get_base_aws_session()
    if limiter is None:
        limiter = PerAccountLimiter(settings.worker_per_account)

    received = queue.receive(max_items=max_items, wait_seconds=wait_seconds)
    outcomes: list[ItemOutcome] = []
    for r in received:
        item = r.item
        if not limiter.try_acquire(item.aws_account_id):
            # Per-account cap reached: not a failure, just "not now".
            queue.nack(r.receipt, delay_seconds=BUSY_ACCOUNT_DELAY_SECONDS)
            record_collect_item(outcome="deferred")
            outcomes.append(ItemOutcome(item=item, status="deferred"))
            continue
        set_collect_items_in_flight(1)
        try:
            outcome = _process_item(session_factory, r, base_session=base_session)
            if outcome.status == "success":
                queue.ack(r.receipt)
            else:
                # Region errors recorded in source_runs; retry with backoff.
                queue.nack(r.receipt, delay_seconds=_backoff_seconds(r.attempts))
            record_collect_item(outcome=outcome.status)
        except Exception as e:
            queue.nack(r.receipt, delay_seconds=_backoff_seconds(r.attempts))
            record_collect_item(outcome="failed")
            outcome = ItemOutcome(item=item, status="failed", errors=(f"{type(e).__name__}: {e}",))
            logger.exception(
                "collect item failed: account=%s region=%s job=%s",
                item.aws_account_id,
                item.region,
                item.job_id,
            )
        finally:
            set_collect_items_in_flight(-1)
            limiter.release(item.aws_account_id)
        logger.info(
            "collect item %s: account=%s region=%s job=%s errors=%d",
            outcome.status,
            item.aws_account_id,
            item.region,
            item.job_id,
            len(outcome.errors),
        )
        outcomes.append(outcome)
    return outcomes


def start_worker_pool(
    session_factory: SessionFactory,
    queue: WorkQueue,
    *,
    concurrency: int,
    per_account: int,
    stop_event: threading.Event,
    wait_seconds: int = 1,
) -> list[threading.Thread]:
    """Start `concurrency` daemon threads draining `queue` until stop_event.

    All threads share one PerAccountLimiter, so the per-account cap holds
    across the whole pool. wait_seconds is the receive poll: short (1s)
    in-process so shutdown is prompt; the standalone SQS worker passes 20
    (long-polling). Threads are daemons AND joined on shutdown: daemon so
    a wedged scan can't hang process exit forever, joined so a healthy
    shutdown finishes the in-flight item first.
    """
    limiter = PerAccountLimiter(per_account)
    threads: list[threading.Thread] = []
    for i in range(concurrency):
        t = threading.Thread(
            target=_pool_loop,
            args=(session_factory, queue, limiter, stop_event, wait_seconds),
            name=f"collect-worker-{i}",
            daemon=True,
        )
        t.start()
        threads.append(t)
    return threads


def _pool_loop(
    session_factory: SessionFactory,
    queue: WorkQueue,
    limiter: PerAccountLimiter,
    stop_event: threading.Event,
    wait_seconds: int,
) -> None:
    while not stop_event.is_set():
        try:
            drain_once(
                session_factory,
                queue,
                max_items=1,
                wait_seconds=wait_seconds,
                limiter=limiter,
            )
        except Exception:
            # A queue-level failure (SQS down, DB unreachable) must not
            # kill the thread: back off and keep the pool alive.
            logger.exception("collect worker: drain raised; backing off 5s")
            stop_event.wait(5)


def main(argv: list[str] | None = None) -> int:
    """Standalone worker entrypoint: `python -m constat_api.worker`.

    This is what the external worker service (sqs mode) runs. It drains
    until SIGTERM/SIGINT, finishing the in-flight item before exiting
    (ECS sends SIGTERM, then SIGKILL after stopTimeout).
    """
    parser = argparse.ArgumentParser(description="Drain the collect queue.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Drain one batch and exit (debugging / smoke tests).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Imported here so `drain_once` in tests never triggers engine creation.
    from constat_api.db import SessionLocal

    queue = get_queue()
    if args.once:
        outcomes = drain_once(SessionLocal, queue, max_items=10, wait_seconds=5)
        logger.info("drained %d item(s)", len(outcomes))
        return 0

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        logger.info("received signal %d; finishing in-flight items", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    threads = start_worker_pool(
        SessionLocal,
        queue,
        concurrency=settings.worker_concurrency,
        per_account=settings.worker_per_account,
        stop_event=stop_event,
        # SQS long-polling: 20s is the max and the cheap default.
        wait_seconds=20,
    )
    logger.info("collect worker pool started (%d threads)", len(threads))
    while not stop_event.is_set():
        stop_event.wait(0.5)
    for t in threads:
        # One full receive poll + a margin; a thread mid-scan exits with
        # the process anyway (daemon) once ECS's stopTimeout expires.
        t.join(timeout=25)
    logger.info("collect worker pool stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
