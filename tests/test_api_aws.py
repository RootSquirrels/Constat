"""Test the /collect/aws HTTP endpoint (async flow, roadmap 1.1).

POST /collect/aws returns 202 + a job id and enqueues one work item per
(target x region); the actual scan runs in the worker. Tests drain the
in-process queue deterministically via `drain_inline_queue` (no sleeps),
then assert on outcomes and the job status endpoint.
"""

from __future__ import annotations

from unittest.mock import patch

from constat_api.orm import FactORM, ResourceORM
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import drain_inline_queue, make_rds_db_dict


def _scan(session, regions):
    for r in regions:
        yield {"_region": r, **make_rds_db_dict()}


def _collector_patches():
    """Patch AssumeRole (returns the base session) and the RDS scan."""
    return (
        patch(
            "constat_api.collectors.aws._assume_role",
            side_effect=lambda base, target: base,
        ),
        patch("constat_api.collectors.aws.collect_db_instances", side_effect=_scan),
    )


def test_aws_collect_endpoint(client: TestClient, session: Session) -> None:
    body = {
        "targets": [
            {
                "aws_account_id": "111111111111",
                "role_arn": "arn:aws:iam::111111111111:role/ConstatReadOnly",
                "external_id": "secret-uuid",
                "name": "prod",
                "regions": ["eu-west-1"],
            }
        ],
        "dry_run": False,
    }

    p_assume, p_scan = _collector_patches()
    with p_assume, p_scan:
        response = client.post("/collect/aws", json=body)
        assert response.status_code == 202, response.text
        payload = response.json()
        assert payload["items_enqueued"] == 1

        outcomes = drain_inline_queue(session)

    assert [o.status for o in outcomes] == ["success"]
    # The scan actually wrote the resource.
    assert session.query(ResourceORM).count() == 1

    job = client.get(f"/collect/aws/jobs/{payload['job_id']}")
    assert job.status_code == 200
    status = job.json()
    assert status["runs_by_status"] == {"success": 1}
    assert status["pending"] == 0
    assert status["runs"][0]["region"] == "eu-west-1"
    assert status["runs"][0]["resources_found"] == 1


def test_aws_collect_endpoint_dry_run(client: TestClient, session: Session) -> None:
    body = {
        "targets": [
            {
                "aws_account_id": "111111111111",
                "regions": ["eu-west-1"],
            }
        ],
        "dry_run": True,
    }
    p_assume, p_scan = _collector_patches()
    with p_assume, p_scan:
        response = client.post("/collect/aws", json=body)
        assert response.status_code == 202
        outcomes = drain_inline_queue(session)

    assert [o.status for o in outcomes] == ["success"]
    # Dry-run: AWS is called but nothing is committed.
    assert session.query(ResourceORM).count() == 0
    assert session.query(FactORM).count() == 0


def test_aws_collect_endpoint_validates_input(client: TestClient) -> None:
    response = client.post("/collect/aws", json={"targets": []})
    assert response.status_code == 422


def test_aws_collect_role_arn_without_external_id_rejected(client: TestClient) -> None:
    """F-06: role_arn without external_id is a confused-deputy risk -> 422."""
    body = {
        "targets": [
            {
                "aws_account_id": "111111111111",
                "role_arn": "arn:aws:iam::111111111111:role/ConstatReadOnly",
                # no external_id
            }
        ],
    }
    response = client.post("/collect/aws", json=body)
    assert response.status_code == 422
    assert "external_id" in response.text


def test_aws_collect_unknown_resource_type_rejected(client: TestClient) -> None:
    """Unknown resource_types fail at enqueue time (422), not in the worker."""
    body = {
        "targets": [
            {"aws_account_id": "111111111111", "resource_types": ["rds", "s3_bucket"]},
        ],
    }
    response = client.post("/collect/aws", json=body)
    assert response.status_code == 422
    assert "s3_bucket" in response.text


def test_aws_collect_endpoint_with_force(client: TestClient, session: Session) -> None:
    """force=True is accepted and propagated to the collector."""
    body = {
        "targets": [
            {"aws_account_id": "111111111111", "regions": ["eu-west-1"]},
        ],
        "force": True,
    }
    p_assume, p_scan = _collector_patches()
    with p_assume, p_scan:
        response = client.post("/collect/aws", json=body)
        assert response.status_code == 202
        outcomes = drain_inline_queue(session)
    assert [o.status for o in outcomes] == ["success"]
    assert session.query(ResourceORM).count() == 1


def test_aws_cleanup_stuck_runs_endpoint(client: TestClient) -> None:
    """The cleanup endpoint returns the number of runs freed."""
    response = client.post(
        "/collect/aws/cleanup-stuck-runs",
        params={"threshold_hours": 2.0},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cleaned"] == 0
    assert body["threshold_hours"] == 2.0
