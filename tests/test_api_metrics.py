"""Tests for the /metrics gate (F-15).

/metrics is open when CONSTAT_METRICS_KEY is unset (trusted-network V1,
a warning is logged at startup). When the key is set, the scraper must
send it via the X-Metrics-Key header; missing and wrong keys both get
the same 401.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from constat_api.auth import _get_settings
from constat_api.main import app
from constat_api.settings import Settings
from fastapi.testclient import TestClient


@pytest.fixture
def metrics_key() -> Iterator[str]:
    """Set a metrics key for one test via a settings dep override."""
    key = "test-metrics-key-67890"

    def _override_settings() -> Settings:
        return Settings(metrics_key=key)

    app.dependency_overrides[_get_settings] = _override_settings
    yield key
    app.dependency_overrides.pop(_get_settings, None)


def test_metrics_open_when_key_unset(client: TestClient) -> None:
    """Default settings (no metrics key): /metrics is open."""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert b"constat_" in response.content or response.content.startswith(b"# HELP")


def test_metrics_401_without_header(metrics_key: str, client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 401


def test_metrics_401_with_wrong_key(metrics_key: str, client: TestClient) -> None:
    response = client.get("/metrics", headers={"X-Metrics-Key": "wrong"})
    assert response.status_code == 401


def test_metrics_200_with_correct_key(metrics_key: str, client: TestClient) -> None:
    response = client.get("/metrics", headers={"X-Metrics-Key": metrics_key})
    assert response.status_code == 200
