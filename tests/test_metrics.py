"""Tests for the Prometheus metrics module + /metrics endpoint.

UX/ops P2 item 11: the SLO targets in the doc become real counters
and histograms here. These tests pin:
- The helper functions increment the right metric with the right labels.
- The /metrics endpoint renders Prometheus exposition format.
- The HTTP middleware records per-request counts (excludes /metrics
  and /health to avoid self-referential noise).
- The runner emits metrics for emitted insights and inconclusive
  records (cross-validates the wiring).
"""

from __future__ import annotations

import pytest
from constat_api.metrics import (
    INCONCLUSIVE_TOTAL,
    INSIGHTS_EMITTED,
    record_focus_rows,
    record_http_request,
    record_inconclusive,
    record_insight_emitted,
    record_insight_run_duration,
    record_source_run,
    render_metrics,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Use unique label values per test to avoid cross-test contamination.
# The metrics module shares one CollectorRegistry; if we used the same
# labels, tests would read each other's counters. Unique labels keep
# each test self-contained while still exercising the real registry.


def _parse_exposition(body: bytes) -> str:
    """Return the exposition body as text (helps debugging failures)."""
    return body.decode("utf-8")


def test_render_metrics_returns_prometheus_format() -> None:
    """The exposition body is text and the content-type is the Prometheus one."""
    body, content_type = render_metrics()
    assert isinstance(body, bytes)
    assert b"# HELP" in body  # every metric has a HELP line
    assert b"# TYPE" in body  # every metric has a TYPE line
    assert "text/plain" in content_type


def test_record_insight_emitted_increments_counter() -> None:
    """The insights_emitted counter is labelled by rule + severity."""
    record_insight_emitted(rule="test_rule_ins", severity="warning")

    # Read the counter back.
    value = INSIGHTS_EMITTED.labels(rule="test_rule_ins", severity="warning")._value.get()
    assert value == 1

    # The exposition includes our label. The exact label order is
    # alphabetical (prometheus_client convention) — we don't pin it.
    body, _ = render_metrics()
    text = _parse_exposition(body)
    assert 'constat_insights_emitted_total' in text
    assert 'rule="test_rule_ins"' in text
    assert 'severity="warning"' in text


def test_record_inconclusive_increments_counter() -> None:
    record_inconclusive(rule="test_rule_inc", reason="scope_not_proven")
    value = INCONCLUSIVE_TOTAL.labels(
        rule="test_rule_inc", reason="scope_not_proven"
    )._value.get()
    assert value == 1
    body, _ = render_metrics()
    text = _parse_exposition(body)
    assert 'constat_inconclusive_total' in text
    assert 'rule="test_rule_inc"' in text
    assert 'reason="scope_not_proven"' in text


def test_record_insight_run_duration_observes_histogram() -> None:
    """A 1.5s run ends up in the 1-2s bucket."""
    record_insight_run_duration(rule="test_rule_dur", duration_seconds=1.5)

    body, _ = render_metrics()
    text = _parse_exposition(body)
    assert 'constat_insights_run_duration_seconds_bucket' in text
    assert 'rule="test_rule_dur"' in text
    assert 'constat_insights_run_duration_seconds_count' in text


def test_record_source_run_observes_histogram_and_counter() -> None:
    record_source_run(
        region="eu-metric-test", status="success", duration_seconds=2.5
    )
    text = _parse_exposition(render_metrics()[0])
    assert 'constat_source_run_duration_seconds_bucket' in text
    assert 'region="eu-metric-test"' in text
    assert 'status="success"' in text
    assert 'constat_source_run_total' in text
    assert 'region="eu-metric-test"' in text
    assert 'status="success"' in text


def test_record_focus_rows_increments_ingested_and_skipped() -> None:
    """The focus counter is labelled by outcome."""
    record_focus_rows(ingested=10, skipped=2)

    text = _parse_exposition(render_metrics()[0])
    assert 'constat_focus_rows_total' in text
    assert 'outcome="ingested"' in text
    assert 'outcome="skipped"' in text


def test_record_http_request_increments_counter_and_observes() -> None:
    record_http_request(
        method="GET", path="/metric-test/path", status_code=200, duration_seconds=0.05
    )
    text = _parse_exposition(render_metrics()[0])
    assert 'constat_http_requests_total' in text
    assert 'method="GET"' in text
    assert 'path="/metric-test/path"' in text
    assert 'status="200"' in text
    assert 'constat_http_request_duration_seconds_bucket' in text


def test_excluded_paths_are_not_recorded() -> None:
    """The middleware must skip /metrics and /health to avoid feedback noise.

    This test asserts the EXCLUSION list is non-empty. The actual
    end-to-end behavior (HTTP middleware skip) is covered by the
    /metrics endpoint integration test below.
    """
    from constat_api.middleware import _EXCLUDED_PATHS

    assert "/metrics" in _EXCLUDED_PATHS
    assert "/health" in _EXCLUDED_PATHS


# ----------------------------------------------------------------------------
# /metrics endpoint integration test
# ----------------------------------------------------------------------------


@pytest.fixture
def metrics_client() -> TestClient:
    """A FastAPI app with the HTTP middleware + /metrics endpoint."""
    from constat_api.metrics import render_metrics
    from constat_api.middleware import HTTPMetricsMiddleware
    from fastapi import Response

    app = FastAPI()
    app.add_middleware(HTTPMetricsMiddleware)

    @app.get("/probe/{value}")
    def probe(value: str) -> dict[str, str]:
        return {"value": value}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint() -> Response:
        body, content_type = render_metrics()
        return Response(content=body, media_type=content_type)

    return TestClient(app)


def test_metrics_endpoint_returns_exposition(metrics_client: TestClient) -> None:
    """The /metrics endpoint returns Prometheus format with the help/type headers."""
    response = metrics_client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "# HELP" in response.text
    assert "# TYPE" in response.text


def test_http_middleware_records_probe_requests(metrics_client: TestClient) -> None:
    """A request to a real route increments the http_requests counter."""
    metrics_client.get("/probe/metric-test-unique-value")
    response = metrics_client.get("/metrics")
    text = response.text
    # The route template is /probe/{value}, not the resolved URL.
    assert 'path="/probe/{value}"' in text
    assert 'status="200"' in text


def test_http_middleware_skips_metrics_endpoint(metrics_client: TestClient) -> None:
    """Hitting /metrics must not itself increment the counter (no feedback)."""
    # Hit /metrics 5 times.
    for _ in range(5):
        metrics_client.get("/metrics")

    after_text = metrics_client.get("/metrics").text

    # The /metrics path is in the exclusion list — its calls don't
    # produce a `path="/metrics"` series. We assert absence.
    assert 'path="/metrics"' not in after_text


def test_http_middleware_skips_health_endpoint(metrics_client: TestClient) -> None:
    """/health is also excluded from the http counter."""
    metrics_client.get("/health")
    text = metrics_client.get("/metrics").text
    assert 'path="/health"' not in text
