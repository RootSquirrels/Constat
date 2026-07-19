"""CollectJob repository: one row per accepted async POST /collect/aws.

The job row is write-once for everything the ORM maps: progress is
derived from the source_runs that carry the job_id, never updated on
the job row itself. That keeps the worker write path unchanged (it only
writes source_runs) and means a crashed worker cannot leave the job row
in a lying state.

Two columns are the exception, and both are accessed via raw text() SQL
because the ORM model (orm.py) does not map them yet — a parallel
workstream owns that file. Migration 0021 adds them:

- `enqueue_error` (SRE-4): the router commits the job row BEFORE
  enqueueing; if the queue send fails, the job is kept and the failure
  is recorded here (a partially-sent queue batch cannot be rolled back).
- `evaluation_status` (SRE-1b): when a job's last work item is acked,
  one worker atomically claims rule evaluation
  (UPDATE ... WHERE evaluation_status IS NULL) and records the outcome.

When the ORM catches up, the text() statements below become plain ORM
attribute access; the function contracts stay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from constat_api.orm import GUID, CollectJobORM, SourceRunORM
from constat_api.tenant import current_tenant


def create_job(
    session: Session,
    *,
    actor: str,
    total_items: int,
    summary: dict[str, Any],
) -> CollectJobORM:
    """Insert the job row for an accepted collect request.

    `summary` must be counts only (accounts / regions / resource_types) —
    the same non-PII discipline as audit_events metadata.
    """
    job = CollectJobORM(actor=actor, total_items=total_items, summary=summary)
    tenant_id = current_tenant(session)
    if tenant_id is not None:
        job.tenant_id = tenant_id
    session.add(job)
    session.flush()
    return job


def get_job(session: Session, job_id: UUID) -> CollectJobORM | None:
    """Fetch a job by id, or None (the API maps None to 404)."""
    return session.execute(
        select(CollectJobORM).where(CollectJobORM.job_id == job_id)
    ).scalar_one_or_none()


def list_runs_for_job(session: Session, job_id: UUID) -> list[SourceRunORM]:
    """All source_runs written for this job, oldest first.

    One work item (account x region) produces one run per scanned
    resource type, so runs outnumber items when resource_types is set.
    """
    stmt = (
        select(SourceRunORM)
        .where(SourceRunORM.job_id == job_id)
        .order_by(SourceRunORM.started_at.asc())
    )
    return list(session.execute(stmt).scalars())


def is_job_complete(session: Session, job_id: UUID, total_items: int) -> bool:
    """True when every enqueued work item has produced its source_run(s).

    Completion = at least one source_run per expected (account, region)
    scope (total_items scopes were enqueued) AND no run still 'running'.
    A failed run counts as done: the item was processed, the rule layer
    degrades to INCONCLUSIVE for unproven scopes — that's the product
    contract, not a reason to block evaluation forever.

    An item that hit "scan already in progress" wrote no run for its
    scope under this job_id, so the scope count stays short and the job
    is correctly NOT complete (the nacked item will be retried).
    """
    runs = list_runs_for_job(session, job_id)
    if any(r.status == "running" for r in runs):
        return False
    scopes = {(r.account_id, r.region) for r in runs}
    return len(scopes) >= total_items


# ---------------------------------------------------------------------------
# Migration-0021 columns via raw SQL (orm.py is owned by a parallel
# workstream — see the module docstring). job_id params are bound with the
# ORM's GUID type so the sqlite CHAR(36) / Postgres native-UUID conversion
# matches the mapped columns exactly.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobOps:
    """The migration-0021 ops columns of one collect_jobs row."""

    enqueue_error: str | None
    evaluation_status: str | None


def get_job_ops(session: Session, job_id: UUID) -> JobOps | None:
    """Read enqueue_error + evaluation_status for the status endpoint.

    Returns None when the job does not exist (caller already 404s via
    get_job, so this is a race-guard only).
    """
    stmt = text(
        "SELECT enqueue_error, evaluation_status FROM collect_jobs WHERE job_id = :job_id"
    ).bindparams(bindparam("job_id", type_=GUID()))
    row = session.execute(stmt, {"job_id": job_id}).one_or_none()
    if row is None:
        return None
    return JobOps(enqueue_error=row.enqueue_error, evaluation_status=row.evaluation_status)


def mark_enqueue_failed(session: Session, job_id: UUID, error: str) -> None:
    """Record a queue-send failure on the (already committed) job row.

    SRE-4: the job is NEVER rolled back on enqueue failure — a queue
    send is not transactional with the DB, so "undoing" the job row
    would strand already-sent items. The caller commits after this.
    """
    stmt = text("UPDATE collect_jobs SET enqueue_error = :err WHERE job_id = :job_id").bindparams(
        bindparam("job_id", type_=GUID())
    )
    session.execute(stmt, {"err": error, "job_id": job_id})


def try_claim_evaluation(session: Session, job_id: UUID) -> bool:
    """Atomically claim post-collect evaluation for this job. One winner.

    The conditional UPDATE is the claim: even with a pool of workers
    acking the last items concurrently, exactly one row matches
    `evaluation_status IS NULL`. The winner must commit before running
    rules so a crash mid-evaluation leaves 'running' (visible, not
    silently re-claimable) rather than a phantom NULL.
    """
    stmt = text(
        "UPDATE collect_jobs SET evaluation_status = 'running' "
        "WHERE job_id = :job_id AND evaluation_status IS NULL"
    ).bindparams(bindparam("job_id", type_=GUID()))
    result = session.execute(stmt, {"job_id": job_id})
    return result.rowcount == 1  # type: ignore[union-attr]


def set_evaluation_status(session: Session, job_id: UUID, status: str) -> None:
    """Write the terminal evaluation outcome ('success' or 'failed')."""
    if status not in ("success", "failed"):
        raise ValueError(f"invalid terminal evaluation_status: {status!r}")
    stmt = text(
        "UPDATE collect_jobs SET evaluation_status = :status WHERE job_id = :job_id"
    ).bindparams(bindparam("job_id", type_=GUID()))
    session.execute(stmt, {"status": status, "job_id": job_id})
