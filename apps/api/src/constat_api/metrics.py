"""Prometheus metrics for the Constat API.

Powers the `/metrics` endpoint. The SLO targets in
`docs/architecture.md` (availability, p95 latency, freshness,
silently-incomplete runs, cross-tenant leaks) become measurable
counters and histograms here. Without this, the SLO targets in the
doc are a polite lie.

Design:

- All metrics are module-level `prometheus_client` objects. They
  share a single `REGISTRY` (the default one). The `/metrics`
  endpoint renders the registry in Prometheus exposition format.
- Helper functions wrap the `.labels(...).inc()` / `.observe()`
  pattern so the call sites in the runner / collector / CLI stay
  readable.
- Labels are bounded (no user-controlled paths, no unbounded
  cardinality). A `path` label uses the FastAPI route template
  (`/insights/{insight_id}`), not the resolved URL.
- V2 migration to OpenTelemetry: replace the `prometheus_client`
  objects with `opentelemetry.metrics` instruments, keep the same
  metric names and labels. The OpenTelemetry Prometheus exporter
  reads the same format; the contract is the metrics, not the lib.

Cardinality budget per metric (rough):
- `constat_insights_emitted_total{rule, severity}` — rule in {rds_eol,
  chargeback} cross severity in {info, warning, critical} -> at most 6 series
- `constat_inconclusive_total{rule, reason}` — rule in {rds_eol} cross
  reason in {scope_not_proven, missing_facts, no_facts, malformed}
  -> at most 4 series
- `constat_insights_run_duration_seconds{rule}` — 2 series
- `constat_focus_rows_total{outcome}` — outcome in {ingested, skipped}
  -> 2 series
- `constat_source_run_duration_seconds{region, status}` — bounded
  by region count cross status count; status in {success, failed,
  partial}
- `constat_http_requests_total{method, path, status}` — bounded by
  route count cross method count cross status count
- `constat_http_request_duration_seconds{method, path}` — bounded
  by route count cross method count
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

# A dedicated registry so test runs don't pollute the global one
# (and we can reset between tests).
REGISTRY = CollectorRegistry(auto_describe=True)


# ----------------------------------------------------------------------------
# Insight metrics
# ----------------------------------------------------------------------------

INSIGHTS_EMITTED = Counter(
    "constat_insights_emitted_total",
    "Insights emitted by rule execution, partitioned by rule and severity.",
    labelnames=("rule", "severity"),
    registry=REGISTRY,
)

INCONCLUSIVE_TOTAL = Counter(
    "constat_inconclusive_total",
    "Inconclusive ('we don't know') records emitted by rule execution, "
    "partitioned by rule and the reason it could not conclude.",
    labelnames=("rule", "reason"),
    registry=REGISTRY,
)

INSIGHTS_RUN_DURATION = Histogram(
    "constat_insights_run_duration_seconds",
    "Duration of a single insight rule execution (one CLI call or "
    "POST /insights/run). The histogram buckets cover 10s..5min which "
    "is the realistic range for a 50-account pilot.",
    labelnames=("rule",),
    registry=REGISTRY,
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)


# ----------------------------------------------------------------------------
# Source-run metrics
# ----------------------------------------------------------------------------

SOURCE_RUN_DURATION = Histogram(
    "constat_source_run_duration_seconds",
    "Duration of a single AWS source run (per region). The status label "
    "captures the outcome so we can split p95 between 'success' and 'failed'.",
    labelnames=("region", "status"),
    registry=REGISTRY,
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
)

SOURCE_RUN_TOTAL = Counter(
    "constat_source_run_total",
    "AWS source runs, partitioned by region and status.",
    labelnames=("region", "status"),
    registry=REGISTRY,
)


# ----------------------------------------------------------------------------
# FOCUS ingestion metrics
# ----------------------------------------------------------------------------

FOCUS_ROWS = Counter(
    "constat_focus_rows_total",
    "FOCUS rows processed, partitioned by outcome (ingested vs skipped).",
    labelnames=("outcome",),
    registry=REGISTRY,
)


# ----------------------------------------------------------------------------
# HTTP metrics
# ----------------------------------------------------------------------------

HTTP_REQUESTS_TOTAL = Counter(
    "constat_http_requests_total",
    "HTTP requests served, partitioned by method, route template, status.",
    labelnames=("method", "path", "status"),
    registry=REGISTRY,
)

HTTP_REQUEST_DURATION = Histogram(
    "constat_http_request_duration_seconds",
    "HTTP request duration. The 'path' label is the FastAPI route "
    "template (e.g. '/insights/{insight_id}') to keep cardinality bounded.",
    labelnames=("method", "path"),
    registry=REGISTRY,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


# ----------------------------------------------------------------------------
# Helper functions (call sites use these, not the metric objects directly)
# ----------------------------------------------------------------------------


def record_insight_emitted(*, rule: str, severity: str) -> None:
    INSIGHTS_EMITTED.labels(rule=rule, severity=severity).inc()


def record_inconclusive(*, rule: str, reason: str) -> None:
    INCONCLUSIVE_TOTAL.labels(rule=rule, reason=reason).inc()


def record_insight_run_duration(*, rule: str, duration_seconds: float) -> None:
    INSIGHTS_RUN_DURATION.labels(rule=rule).observe(duration_seconds)


def record_source_run(*, region: str, status: str, duration_seconds: float) -> None:
    SOURCE_RUN_DURATION.labels(region=region, status=status).observe(duration_seconds)
    SOURCE_RUN_TOTAL.labels(region=region, status=status).inc()


def record_focus_rows(*, ingested: int, skipped: int) -> None:
    if ingested:
        FOCUS_ROWS.labels(outcome="ingested").inc(ingested)
    if skipped:
        FOCUS_ROWS.labels(outcome="skipped").inc(skipped)


def record_http_request(
    *, method: str, path: str, status_code: int, duration_seconds: float
) -> None:
    HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status=str(status_code)).inc()
    HTTP_REQUEST_DURATION.labels(method=method, path=path).observe(duration_seconds)


# ----------------------------------------------------------------------------
# Exposition
# ----------------------------------------------------------------------------


def render_metrics() -> tuple[bytes, str]:
    """Render the registry in Prometheus exposition format.

    Returns `(body, content_type)`. The FastAPI endpoint uses this
    verbatim.
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
