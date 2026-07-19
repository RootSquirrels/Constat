"""Work queue for asynchronous AWS collection (roadmap 1.1 / 1.2).

POST /collect/aws enqueues one `WorkItem` per (target x region); a worker
(`constat_api.worker`) drains items and runs the collector for that single
region. One item = one account x one region, so the natural unit of AWS
throttling (per account) and of failure (per region) are both one message.

Two implementations behind the `WorkQueue` protocol:

- `InProcessQueue` — thread-safe, bounded, in-memory. Default (`inline`
  mode): the API process enqueues and drains it from a lifespan worker
  pool. Not durable — a restart loses pending items. That is acceptable
  for the single-replica pilot: collection is idempotent (the source_runs
  partial unique index dedupes), so re-POSTing rebuilds the backlog.
- `SQSQueue` — production (`sqs` mode). Message body is the JSON WorkItem;
  ack = DeleteMessage, nack = ChangeMessageVisibility. DLQ / redrive
  policy is infrastructure's job (Terraform), not this module's.

The router and the in-process worker share one queue instance per process
via `get_queue()` (module-level singleton). Tests call `reset_queue()` for
isolation and drive `worker.drain_once` directly — no sleeps.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

import boto3

from constat_api.settings import settings

logger = logging.getLogger(__name__)


class QueueFullError(Exception):
    """Raised by send() when the queue is at capacity (backpressure, 1.2).

    The API maps this to 503 + Retry-After: the caller slows down instead
    of the process growing an unbounded in-memory backlog.
    """


@dataclass(frozen=True)
class WorkItem:
    """One unit of collection work: one AWS account, one region.

    `resource_types` travels inside the item (None = collector default,
    i.e. RDS only). `force` and `dry_run` are propagated verbatim from the
    POST body. `job_id` links every source_run the worker writes back to
    the collect_jobs row the API returned.
    """

    job_id: UUID
    aws_account_id: str
    region: str
    role_arn: str | None = None
    external_id: str | None = None
    name: str | None = None
    resource_types: tuple[str, ...] | None = None
    force: bool = False
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form (SQS message body)."""
        return {
            "job_id": str(self.job_id),
            "aws_account_id": self.aws_account_id,
            "region": self.region,
            "role_arn": self.role_arn,
            "external_id": self.external_id,
            "name": self.name,
            "resource_types": list(self.resource_types) if self.resource_types else None,
            "force": self.force,
            "dry_run": self.dry_run,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> WorkItem:
        """Inverse of to_dict. Raises KeyError/ValueError on malformed input."""
        return WorkItem(
            job_id=UUID(data["job_id"]),
            aws_account_id=data["aws_account_id"],
            region=data["region"],
            role_arn=data.get("role_arn"),
            external_id=data.get("external_id"),
            name=data.get("name"),
            resource_types=tuple(data["resource_types"]) if data.get("resource_types") else None,
            force=bool(data.get("force", False)),
            dry_run=bool(data.get("dry_run", False)),
        )


@dataclass(frozen=True)
class ReceivedItem:
    """A dequeued WorkItem plus the receipt the ack/nack calls need.

    `attempts` counts deliveries (1 = first). The worker uses it for
    nack backoff; on SQS it maps to ApproximateReceiveCount.
    """

    receipt: str
    item: WorkItem
    attempts: int


class WorkQueue(Protocol):
    """The queue contract the worker drains and the API enqueues into."""

    def send(self, items: Sequence[WorkItem]) -> None:
        """Enqueue items atomically-ish. Raises QueueFullError on backpressure."""
        ...

    def receive(self, max_items: int, wait_seconds: int) -> list[ReceivedItem]:
        """Dequeue up to max_items, waiting at most wait_seconds for one."""
        ...

    def ack(self, receipt: str) -> None:
        """Mark a received item as processed (delete it)."""
        ...

    def nack(self, receipt: str, delay_seconds: float) -> None:
        """Return a received item to the queue, visible again after delay."""
        ...


class InProcessQueue:
    """Thread-safe bounded in-memory queue with delayed nack requeue.

    Layout: a deque of ready items, a list of delayed (not-before) items,
    and an in-flight map keyed by receipt. Receipts are random uuids, so a
    double-ack or an ack after nack is a logged no-op, never a crash.
    Capacity counts pending AND in-flight items — in-flight work is still
    memory the process holds, and backpressure must cover it.
    """

    def __init__(self, maxsize: int = 1000) -> None:
        self._maxsize = maxsize
        self._cond = threading.Condition()
        self._ready: deque[tuple[str, WorkItem, int]] = deque()
        self._delayed: list[tuple[float, str, WorkItem, int]] = []
        self._in_flight: dict[str, tuple[WorkItem, int]] = {}

    def _pending_count(self) -> int:
        """Items the queue still owes a worker: ready + delayed + in-flight."""
        return len(self._ready) + len(self._delayed) + len(self._in_flight)

    def send(self, items: Sequence[WorkItem]) -> None:
        if not items:
            return
        with self._cond:
            if self._pending_count() + len(items) > self._maxsize:
                raise QueueFullError(
                    f"collect queue is full ({self._pending_count()}/{self._maxsize} items); "
                    f"refusing {len(items)} more"
                )
            for item in items:
                self._ready.append((uuid4().hex, item, 0))
            self._cond.notify_all()
        self._record_depth()

    def receive(self, max_items: int, wait_seconds: int) -> list[ReceivedItem]:
        deadline = time.monotonic() + max(wait_seconds, 0)
        with self._cond:
            while True:
                self._promote_due()
                out: list[ReceivedItem] = []
                while self._ready and len(out) < max_items:
                    receipt, item, attempts = self._ready.popleft()
                    attempts += 1
                    self._in_flight[receipt] = (item, attempts)
                    out.append(ReceivedItem(receipt=receipt, item=item, attempts=attempts))
                if out or time.monotonic() >= deadline:
                    if out:
                        self._record_depth()
                    return out
                # Nothing ready: wait until the next delayed item matures,
                # a send arrives, or the caller's deadline passes.
                wait_for = deadline - time.monotonic()
                if self._delayed:
                    wait_for = min(wait_for, max(self._delayed[0][0] - time.monotonic(), 0.0))
                self._cond.wait(timeout=max(wait_for, 0.0))

    def ack(self, receipt: str) -> None:
        with self._cond:
            if self._in_flight.pop(receipt, None) is None:
                logger.warning("ack for unknown receipt %s (already acked/nacked?)", receipt)
        self._record_depth()

    def nack(self, receipt: str, delay_seconds: float) -> None:
        with self._cond:
            entry = self._in_flight.pop(receipt, None)
            if entry is None:
                logger.warning("nack for unknown receipt %s (already acked/nacked?)", receipt)
                return
            item, attempts = entry
            not_before = time.monotonic() + max(delay_seconds, 0.0)
            self._delayed.append((not_before, receipt, item, attempts))
            self._delayed.sort(key=lambda e: e[0])
            self._cond.notify_all()
        self._record_depth()

    def _promote_due(self) -> None:
        """Move delayed items whose not-before has passed back to ready.

        Caller must hold the condition lock.
        """
        now = time.monotonic()
        while self._delayed and self._delayed[0][0] <= now:
            _, receipt, item, attempts = self._delayed.pop(0)
            self._ready.append((receipt, item, attempts))

    def _record_depth(self) -> None:
        """Best-effort queue-depth gauge. In-process only: on SQS, depth
        is a CloudWatch metric and a local gauge would be one replica's
        partial view."""
        from constat_api.metrics import set_collect_queue_depth

        with self._cond:
            set_collect_queue_depth(len(self._ready) + len(self._delayed))


class SQSQueue:
    """SQS-backed queue for `sqs` mode (production).

    One SQS message per WorkItem, body = JSON of `WorkItem.to_dict()`.
    Long-polling (20 s by default) keeps the worker loop cheap. The
    visibility timeout must exceed the slowest single-region scan —
    CONSTAT_SQS_VISIBILITY_TIMEOUT, default 15 min (see settings).

    Redrive policy / DLQ are configured on the queue itself by Terraform;
    a poison item exhausts its receives and lands there without any code
    here.
    """

    def __init__(
        self,
        queue_url: str,
        *,
        visibility_timeout_seconds: int = 900,
        client: Any | None = None,
    ) -> None:
        self._queue_url = queue_url
        self._visibility_timeout = visibility_timeout_seconds
        self._client = client if client is not None else boto3.client("sqs")

    def send(self, items: Sequence[WorkItem]) -> None:
        # One SendMessage per item. At ICP scale (~560 items per sweep)
        # this is a few hundred calls per POST — fine, and it keeps a
        # partial failure surface obvious (SendMessageBatch silently
        # splits failures per entry). Revisit if a sweep grows 10x.
        for item in items:
            self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=json.dumps(item.to_dict()),
            )

    def receive(self, max_items: int, wait_seconds: int) -> list[ReceivedItem]:
        response = self._client.receive_message(
            QueueUrl=self._queue_url,
            # SQS caps MaxNumberOfMessages at 10.
            MaxNumberOfMessages=max(1, min(max_items, 10)),
            WaitTimeSeconds=max(0, min(wait_seconds, 20)),
            VisibilityTimeout=self._visibility_timeout,
            AttributeNames=["ApproximateReceiveCount"],
        )
        out: list[ReceivedItem] = []
        for msg in response.get("Messages", []):
            try:
                item = WorkItem.from_dict(json.loads(msg["Body"]))
            except (KeyError, ValueError, json.JSONDecodeError):
                # A malformed message can never be processed; ack it away
                # so it doesn't burn its redrive budget as a poison pill.
                logger.exception("dropping malformed SQS message %s", msg.get("MessageId"))
                self.ack(msg["ReceiptHandle"])
                continue
            out.append(
                ReceivedItem(
                    receipt=msg["ReceiptHandle"],
                    item=item,
                    attempts=int(msg.get("Attributes", {}).get("ApproximateReceiveCount", "1")),
                )
            )
        return out

    def ack(self, receipt: str) -> None:
        self._client.delete_message(QueueUrl=self._queue_url, ReceiptHandle=receipt)

    def nack(self, receipt: str, delay_seconds: float) -> None:
        self._client.change_message_visibility(
            QueueUrl=self._queue_url,
            ReceiptHandle=receipt,
            # SQS visibility timeout is an int in [0, 43200].
            VisibilityTimeout=max(0, min(int(delay_seconds), 43200)),
        )


def build_queue() -> WorkQueue:
    """Build the queue for the configured collect mode.

    `inline` -> InProcessQueue bounded by CONSTAT_COLLECT_QUEUE_MAXSIZE.
    `sqs` -> SQSQueue on CONSTAT_COLLECT_QUEUE_URL (required).
    Anything else is a startup-time config error, not a runtime surprise.
    """
    if settings.collect_mode == "inline":
        return InProcessQueue(maxsize=settings.collect_queue_maxsize)
    if settings.collect_mode == "sqs":
        if not settings.collect_queue_url:
            raise ValueError("CONSTAT_COLLECT_MODE=sqs requires CONSTAT_COLLECT_QUEUE_URL")
        return SQSQueue(
            settings.collect_queue_url,
            visibility_timeout_seconds=settings.sqs_visibility_timeout_seconds,
        )
    raise ValueError(
        f"unknown CONSTAT_COLLECT_MODE {settings.collect_mode!r} (expected 'inline' or 'sqs')"
    )


_queue: WorkQueue | None = None
_queue_lock = threading.Lock()


def get_queue() -> WorkQueue:
    """Process-wide queue singleton. The API router enqueues into it and
    the inline worker pool drains it, so both must see the same instance."""
    global _queue
    if _queue is None:
        with _queue_lock:
            if _queue is None:
                _queue = build_queue()
    return _queue


def reset_queue() -> None:
    """Drop the singleton. Test helper: each test gets a fresh, empty queue."""
    global _queue
    with _queue_lock:
        _queue = None
