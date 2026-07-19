"""End-to-end tests for the async collect flow (roadmap 1.1 / 1.2).

POST /collect/aws -> 202 + job row + enqueued work items ->
`drain_inline_queue` (deterministic worker drain, no sleeps) ->
GET /collect/aws/jobs/{job_id} reflects the outcome.

SRE-4: the job row is committed BEFORE the queue send, so an enqueue
failure keeps the job and records `enqueue_error` on it (503 + job_id).
SRE-2b: a target without `resource_types` scans ALL registered jobs;
tests that drain with only the RDS scan mocked pass ["rds"] explicitly.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from botocore.exceptions import EndpointConnectionError
from constat_api.collect_queue import InProcessQueue
from constat_api.collectors import aws as aws_collector
from constat_api.orm import CollectJobORM, ResourceORM
from constat_api.repositories import collect_jobs as collect_jobs_repo
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import drain_inline_queue, make_rds_db_dict

_ALL_RESOURCE_TYPES = sorted(aws_collector.JOB_REGISTRY)


def _scan(session, regions):
    for r in regions:
        yield {"_region": r, **make_rds_db_dict()}


def _collector_patches(scan_fn=_scan):
    return (
        patch(
            "constat_api.collectors.aws._assume_role",
            side_effect=lambda base, target: base,
        ),
        patch("constat_api.collectors.aws.collect_db_instances", side_effect=scan_fn),
    )


def _patch_ec2_jobs_empty() -> Any:
    """Replace the EC2 jobs' scan_fn with an empty iterator.

    The RDS scan has a test seam (the worker passes collect_db_instances
    as a late-bound override); the EC2 jobs read their scan_fn straight
    from JOB_REGISTRY, so tests that exercise the all-jobs default patch
    the registry entries instead.
    """

    def _empty(_session: Any, _regions: Any) -> Iterator[dict]:
        return iter([])

    patched = {
        key: (job if key == "rds" else dataclasses.replace(job, scan_fn=_empty))
        for key, job in aws_collector.JOB_REGISTRY.items()
    }
    return patch.dict(aws_collector.JOB_REGISTRY, patched)


def test_post_returns_202_creates_job_and_enqueues(client: TestClient, session: Session) -> None:
    body = {
        "targets": [
            {
                "aws_account_id": "111111111111",
                "name": "prod",
                "regions": ["eu-west-1", "eu-central-1"],
            }
        ],
    }
    response = client.post("/collect/aws", json=body)
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["items_enqueued"] == 2

    # The job row exists with counts-only summary (no account ids).
    job = session.query(CollectJobORM).one()
    assert str(job.job_id) == payload["job_id"]
    assert job.total_items == 2
    assert job.actor == "anonymous"  # auth-open dev mode
    # SRE-2b: no resource_types in the request -> the default scope is
    # ALL registered jobs, and the summary says so.
    assert job.summary == {
        "accounts": 1,
        "regions": 2,
        "resource_types": _ALL_RESOURCE_TYPES,
    }


def test_default_scope_scans_all_registered_jobs(client: TestClient, session: Session) -> None:
    """SRE-2b: a target without resource_types produces one source_run
    per registered job per region (rds + ec2_volume + ec2_snapshot +
    ec2_instance), not just the RDS one."""
    body = {
        "targets": [
            {"aws_account_id": "111111111111", "regions": ["eu-west-1"]},
        ],
    }
    p_assume, p_scan = _collector_patches()
    with p_assume, p_scan, _patch_ec2_jobs_empty():
        response = client.post("/collect/aws", json=body)
        assert response.status_code == 202
        outcomes = drain_inline_queue(session)

    assert [o.status for o in outcomes] == ["success"]
    status = client.get(f"/collect/aws/jobs/{response.json()['job_id']}").json()
    assert status["runs_by_status"] == {"success": len(_ALL_RESOURCE_TYPES)}
    assert {r["resource_type"] for r in status["runs"]} == {
        aws_collector.JOB_REGISTRY[k].resource_type for k in _ALL_RESOURCE_TYPES
    }


def test_drain_then_job_status_shows_success(client: TestClient, session: Session) -> None:
    body = {
        "targets": [
            {
                "aws_account_id": "111111111111",
                "regions": ["eu-west-1", "eu-central-1"],
                # Explicit rds-only scan: only the RDS scan_fn is mocked.
                "resource_types": ["rds"],
            },
        ],
    }
    p_assume, p_scan = _collector_patches()
    with p_assume, p_scan:
        response = client.post("/collect/aws", json=body)
        assert response.status_code == 202
        outcomes = drain_inline_queue(session)

    assert sorted(o.status for o in outcomes) == ["success", "success"]
    # One resource per region (same native id, distinct regions).
    assert session.query(ResourceORM).count() == 2

    job = client.get(f"/collect/aws/jobs/{response.json()['job_id']}")
    assert job.status_code == 200
    status = job.json()
    assert status["total_items"] == 2
    assert status["scopes_started"] == 2
    assert status["pending"] == 0
    assert status["runs_by_status"] == {"success": 2}
    assert {r["region"] for r in status["runs"]} == {"eu-west-1", "eu-central-1"}
    assert all(r["resources_found"] == 1 for r in status["runs"])
    # SRE-1b: the completed job's evaluation chain ran to the end.
    assert status["evaluation_status"] == "success"
    assert status["enqueue_error"] is None


def test_failure_isolation_one_region_fails_others_succeed(
    client: TestClient, session: Session
) -> None:
    """1.1 AC: one region raising (BotoCoreError family) fails only its own
    item; the other region's scan succeeds and the job shows both."""

    def _flaky(session, regions):
        if regions == ["eu-west-1"]:
            raise EndpointConnectionError(endpoint_url="https://rds.eu-west-1.amazonaws.com")
        yield from _scan(session, regions)

    body = {
        "targets": [
            {
                "aws_account_id": "111111111111",
                "regions": ["eu-west-1", "eu-central-1"],
                "resource_types": ["rds"],
            },
        ],
    }
    p_assume, p_scan = _collector_patches(_flaky)
    with p_assume, p_scan:
        response = client.post("/collect/aws", json=body)
        assert response.status_code == 202
        outcomes = drain_inline_queue(session)

    by_region = {o.item.region: o for o in outcomes}
    assert by_region["eu-central-1"].status == "success"
    assert by_region["eu-west-1"].status == "failed"
    assert by_region["eu-west-1"].errors  # the region error travelled with the outcome

    status = client.get(f"/collect/aws/jobs/{response.json()['job_id']}").json()
    assert status["runs_by_status"] == {"failed": 1, "success": 1}
    failed_run = next(r for r in status["runs"] if r["status"] == "failed")
    assert failed_run["region"] == "eu-west-1"
    assert failed_run["error"] is not None


def test_queue_full_returns_503_and_keeps_job_with_enqueue_error(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """SRE-4 (outbox ordering): the job row is committed BEFORE the send,
    so a full queue -> 503 + Retry-After, the job row is KEPT (never
    rolled back), and `enqueue_error` records the failure. The 503 detail
    carries the job_id so the caller can reconcile."""
    monkeypatch.setattr(
        "constat_api.routers.aws.get_queue",
        lambda: InProcessQueue(maxsize=1),
    )
    body = {
        "targets": [
            {"aws_account_id": "111111111111", "regions": ["eu-west-1", "eu-central-1"]},
        ],
    }
    response = client.post("/collect/aws", json=body)
    assert response.status_code == 503
    assert "Retry-After" in response.headers

    # The job survived the failed send and is marked for reconciliation.
    job = session.query(CollectJobORM).one()
    assert str(job.job_id) in response.json()["detail"]
    ops = collect_jobs_repo.get_job_ops(session, job.job_id)
    assert ops is not None
    assert ops.enqueue_error is not None
    assert "QueueFullError" in ops.enqueue_error

    # ... and the status endpoint surfaces it.
    status = client.get(f"/collect/aws/jobs/{job.job_id}").json()
    assert status["enqueue_error"] == ops.enqueue_error
    assert status["evaluation_status"] is None


def test_get_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get(f"/collect/aws/jobs/{uuid4()}")
    assert response.status_code == 404
