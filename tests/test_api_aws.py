"""Test the /collect/aws HTTP endpoint.

The collector is fully injectable, so the endpoint test mocks the boto3
session and the scan function via dependency injection at the collector level.
For a true end-to-end test we'd need a real boto3 session — that's V2 with moto.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def _make_db(arn: str = "arn:aws:rds:eu-west-1:111111111111:db:test") -> dict[str, Any]:
    return {
        "DBInstanceArn": arn,
        "DBInstanceIdentifier": "test",
        "Engine": "postgres",
        "EngineVersion": "14.7",
        "DBInstanceClass": "db.m5.xlarge",
        "DBInstanceStatus": "available",
        "AllocatedStorage": 100,
        "InstanceCreateTime": datetime(2024, 1, 1, tzinfo=UTC),
        "MultiAZ": True,
        "StorageEncrypted": True,
        "DBSubnetGroup": {"DBSubnetGroupName": "default"},
        "Endpoint": {"Address": "test.xxxx.eu-west-1.rds.amazonaws.com"},
    }


def _scan(session, regions):
    for r in regions:
        yield {"_region": r, **_make_db()}


def test_aws_collect_endpoint(client: TestClient) -> None:
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

    fake_base = MagicMock()
    with (
        patch("constat_api.routers.aws.get_base_aws_session") as mock_session,
        patch(
            "constat_api.collectors.aws._assume_role",
            side_effect=lambda base, target: base,
        ),
        patch("constat_api.collectors.aws.collect_db_instances", side_effect=_scan),
    ):
        mock_session.return_value = fake_base
        response = client.post("/collect/aws", json=body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["results"]) == 1
    r = payload["results"][0]
    assert r["aws_account_id"] == "111111111111"
    assert r["resources_written"] == 1
    assert r["errors"] == []


def test_aws_collect_endpoint_dry_run(client: TestClient) -> None:
    body = {
        "targets": [
            {
                "aws_account_id": "111111111111",
                "regions": ["eu-west-1"],
            }
        ],
        "dry_run": True,
    }
    with (
        patch("constat_api.routers.aws.get_base_aws_session") as mock_session,
        patch(
            "constat_api.collectors.aws._assume_role",
            side_effect=lambda base, target: base,
        ),
        patch("constat_api.collectors.aws.collect_db_instances", side_effect=_scan),
    ):
        mock_session.return_value = MagicMock()
        response = client.post("/collect/aws", json=body)

    assert response.status_code == 200
    r = response.json()["results"][0]
    assert r["resources_written"] == 1
    assert r["facts_written"] == 0  # dry-run
    assert r["observations_written"] == 0


def test_aws_collect_endpoint_validates_input(client: TestClient) -> None:
    response = client.post("/collect/aws", json={"targets": []})
    assert response.status_code == 422
