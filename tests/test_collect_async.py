"""End-to-end tests for the async collect flow (roadmap 1.1 / 1.2).

POST /collect/aws -> 202 + job row + enqueued work items ->
`drain_inline_queue` (deterministic worker drain, no sleeps) ->
GET /collect/aws/jobs/{job_id} reflects the outcome.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

from botocore.exceptions import EndpointConnectionError
from constat_api.collect_queue import InProcessQueue
from constat_api.orm import CollectJobORM, ResourceORM
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import drain_inline_queue, make_rds_db_dict


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
    assert job.summary == {"accounts": 1, "regions": 2, "resource_types": ["rds"]}


def test_drain_then_job_status_shows_success(client: TestClient, session: Session) -> None:
    body = {
        "targets": [
            {"aws_account_id": "111111111111", "regions": ["eu-west-1", "eu-central-1"]},
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
            {"aws_account_id": "111111111111", "regions": ["eu-west-1", "eu-central-1"]},
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


def test_queue_full_returns_503_with_retry_after(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """Backpressure (1.2): a full in-process queue -> 503 + Retry-After,
    and no half-created job row."""
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
    assert session.query(CollectJobORM).count() == 0


def test_get_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get(f"/collect/aws/jobs/{uuid4()}")
    assert response.status_code == 404
