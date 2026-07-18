"""Tests for the V1 API key auth middleware.

Auth is a single shared API key passed via `X-API-Key`. When the
configured `CONSTAT_API_KEY` is None, auth is open (dev mode). When set,
requests without the matching header get 401.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from constat_api.auth import _get_settings, verify_api_key
from constat_api.main import app
from constat_api.settings import Settings
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def auth_settings(client: TestClient):
    """Enable auth on the app for the duration of one test.

    Overrides the settings dep on the app to return a Settings with
    `api_key` set, so verify_api_key actually requires the header.
    The `client` fixture provides a TestClient wired to the in-memory DB.
    """
    test_key = "test-secret-key-12345"

    def _override_settings():
        return Settings(api_key=test_key)

    app.dependency_overrides[_get_settings] = _override_settings
    yield test_key
    # Reset the dep override; the client fixture clears at session teardown
    app.dependency_overrides.pop(_get_settings, None)


# ---------------------------------------------------------------------------
# Unit tests of verify_api_key (no HTTP)
# ---------------------------------------------------------------------------


def test_verify_api_key_open_in_dev_mode() -> None:
    """When api_key is None, no header required, no exception."""
    cfg = Settings(api_key=None)
    verify_api_key(x_api_key=None, cfg=cfg)
    verify_api_key(x_api_key="any-value", cfg=cfg)


def test_verify_api_key_requires_header_when_key_set() -> None:
    """When api_key is configured, missing header -> 401."""
    cfg = Settings(api_key="some-key")
    with pytest.raises(HTTPException) as exc_info:
        verify_api_key(x_api_key=None, cfg=cfg)
    assert exc_info.value.status_code == 401
    assert "required" in exc_info.value.detail.lower()


def test_verify_api_key_rejects_wrong_key() -> None:
    """Configured key set, wrong header value -> 401."""
    cfg = Settings(api_key="the-real-key")
    with pytest.raises(HTTPException) as exc_info:
        verify_api_key(x_api_key="wrong", cfg=cfg)
    assert exc_info.value.status_code == 401
    assert "invalid" in exc_info.value.detail.lower()


def test_verify_api_key_accepts_correct_key() -> None:
    """Configured key set, correct header -> no exception."""
    cfg = Settings(api_key="the-real-key")
    verify_api_key(x_api_key="the-real-key", cfg=cfg)


# ---------------------------------------------------------------------------
# HTTP-level: each protected endpoint returns 401 without the right header
# ---------------------------------------------------------------------------


def test_health_endpoint_stays_open_under_auth(client: TestClient) -> None:
    """/health does NOT require auth (LB health checks must not be auth'd)."""
    # The default settings have api_key=None, so /health is open.
    response = client.get("/health")
    assert response.status_code == 200


def test_collect_aws_returns_401_without_header(auth_settings: str, client: TestClient) -> None:
    """POST /collect/aws without the right header -> 401."""
    body = {
        "targets": [{"aws_account_id": "111111111111", "regions": ["eu-west-1"]}],
    }
    response = client.post("/collect/aws", json=body)
    assert response.status_code == 401

    response = client.post("/collect/aws", json=body, headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


def test_collect_aws_returns_200_with_correct_header(
    auth_settings: str, client: TestClient
) -> None:
    """POST /collect/aws with the right header -> 200."""
    from datetime import UTC, datetime

    body = {
        "targets": [{"aws_account_id": "111111111111", "regions": ["eu-west-1"]}],
    }
    with (
        patch("constat_api.routers.aws.get_base_aws_session") as mock_session,
        patch(
            "constat_api.collectors.aws._assume_role",
            side_effect=lambda base, target: base,
        ),
        patch(
            "constat_api.collectors.aws.collect_db_instances",
            return_value=iter(
                [
                    {
                        "_region": "eu-west-1",
                        "DBInstanceArn": "arn:aws:rds:eu-west-1:111111111111:db:t",
                        "DBInstanceIdentifier": "t",
                        "Engine": "postgres",
                        "EngineVersion": "14.7",
                        "DBInstanceClass": "db.m5.large",
                        "DBInstanceStatus": "available",
                        "AllocatedStorage": 100,
                        "InstanceCreateTime": datetime(2024, 1, 1, tzinfo=UTC),
                        "MultiAZ": True,
                        "StorageEncrypted": True,
                        "DBSubnetGroup": {"DBSubnetGroupName": "default"},
                        "Endpoint": {"Address": "t.x.rds.amazonaws.com"},
                    }
                ]
            ),
        ),
    ):
        mock_session.return_value = MagicMock()
        response = client.post("/collect/aws", json=body, headers={"X-API-Key": auth_settings})
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["resources_written"] == 1


def test_insights_run_returns_401_without_header(auth_settings: str, client: TestClient) -> None:
    """POST /insights/run without the right header -> 401."""
    response = client.post("/insights/run", json={"rule": "rds_eol"})
    assert response.status_code == 401

    response = client.post(
        "/insights/run",
        json={"rule": "rds_eol"},
        headers={"X-API-Key": "wrong"},
    )
    assert response.status_code == 401


def test_insights_run_returns_200_with_correct_header(
    auth_settings: str, client: TestClient
) -> None:
    """POST /insights/run with the right header -> 200 (no resources, no rejection)."""
    response = client.post(
        "/insights/run",
        json={"rule": "rds_eol"},
        headers={"X-API-Key": auth_settings},
    )
    assert response.status_code == 200
    assert response.json()["rule_name"] == "rds_eol"


def test_focus_collect_returns_401_without_header(
    auth_settings: str, client: TestClient, tmp_path
) -> None:
    """POST /collect/focus without the right header -> 401."""
    response = client.post(
        "/collect/focus",
        json={"account_external_id": "111", "file_path": str(tmp_path / "x.csv")},
    )
    assert response.status_code == 401


def test_insights_list_returns_401_without_header(auth_settings: str, client: TestClient) -> None:
    """GET /insights without the right header -> 401."""
    response = client.get("/insights")
    assert response.status_code == 401
