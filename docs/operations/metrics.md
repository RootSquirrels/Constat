# Metrics (Prometheus exposition)

> UX/ops P2 item 11. Every SLO target in
> [`../architecture.md`](../architecture.md) becomes a real
> counter or histogram here. Without this, the SLO targets in
> the doc are a polite lie.

The `/metrics` endpoint serves the standard Prometheus exposition
format. The scraper is on the trusted network (a k8s sidecar, a
dedicated VPC, an internal LB). Same trust model as `/health`:
open in V1, gate behind a `CONSTAT_METRICS_KEY` in V2 if we
expose beyond the trusted boundary.

## Quick start

```bash
# Local: hit the endpoint directly.
curl -s http://localhost:8000/metrics | head -20

# Prometheus scrape config.
scrape_configs:
  - job_name: constat
    scrape_interval: 30s
    static_configs:
      - targets: ['constat-api:8000']
    metrics_path: /metrics
```

The endpoint is **not** behind `verify_api_key`. Prometheus
scrapers don't carry a JWT; the network is the perimeter.

## What is exposed

### Insights (`rule_name` ∈ `{rds_eol, chargeback}`)

| Metric | Type | Labels | When it moves |
|---|---|---|---|
| `constat_insights_emitted_total` | counter | `rule`, `severity` | An insight is inserted (MATCH, not NO_MATCH). |
| `constat_inconclusive_total` | counter | `rule`, `reason` | An INCONCLUSIVE is emitted (`scope_not_proven`, `missing_facts`, `<no facts>`, etc.). |
| `constat_insights_run_duration_seconds` | histogram | `rule` | One full rule execution (CLI or POST /insights/run). Buckets: 0.5s..5min. |

### AWS source runs

| Metric | Type | Labels | When it moves |
|---|---|---|---|
| `constat_source_run_duration_seconds` | histogram | `region`, `status` | One per-region scan completes (success or failed). Buckets: 0.5s..10min. |
| `constat_source_run_total` | counter | `region`, `status` | Same trigger. |

`status` is `success`, `failed`, or `partial`. Regions skipped by
the circuit breaker do **not** fire this counter (no `source_run`
row was created).

### FOCUS ingestion

| Metric | Type | Labels | When it moves |
|---|---|---|---|
| `constat_focus_rows_total` | counter | `outcome` (`ingested` or `skipped`) | A row is successfully ingested or dropped by the loader. |

Use `constat_focus_rows_total{outcome="skipped"} /
constat_focus_rows_total{outcome="ingested"}` as a skip-rate
ratio. Alert when it exceeds 5% over a 24h window.

### HTTP

| Metric | Type | Labels | When it moves |
|---|---|---|---|
| `constat_http_requests_total` | counter | `method`, `path`, `status` | One per HTTP request, excluding `/metrics` and `/health`. |
| `constat_http_request_duration_seconds` | histogram | `method`, `path` | Same trigger. Buckets: 5ms..10s. |

`path` is the **route template** (`/insights/{insight_id}`), not
the resolved URL. This keeps cardinality bounded to the number
of registered routes, not the number of resource IDs. 404s are
labeled `path="unmatched"`.

## Sample PromQL queries

The 5 most useful ones for the V1 pilot:

```promql
# 1. SLO: 99.9% availability
sum(rate(constat_http_requests_total{status!~"5.."}[5m]))
/
sum(rate(constat_http_requests_total[5m]))

# 2. SLO: GET /insights p95 < 500ms
histogram_quantile(0.95,
  rate(constat_http_request_duration_seconds_bucket{
    method="GET", path="/insights"
  }[5m])
)

# 3. SLO: AWS scan freshness < 6h
6 * 3600 -
  time() - max(...)
# (or: alert if no successful source_run in 6h; the alert rule is
# in your Prometheus alertmanager config, not in the API)

# 4. SLO: zero silentry-incomplete runs
# (The runner emits source_run_total{status="failed"} per region;
# alert if status="failed" rate > 0 for > 1h with no compensating
# success.)

# 5. Skip-rate FOCUS
sum(rate(constat_focus_rows_total{outcome="skipped"}[24h]))
/
sum(rate(constat_focus_rows_total[24h]))
```

## Cardinality budget

The labels are bounded by intent:

- `rule` ∈ 2 values (rds_eol, chargeback)
- `severity` ∈ 3 values (info, warning, critical)
- `reason` ∈ ~4 values (scope_not_proven, missing_facts, etc.)
- `status` (HTTP) ∈ ~10 values (200, 201, 204, 400, 401, 404, 409, 422, 500, ...)
- `path` ≈ 10-20 registered routes (templates, not resolved)
- `region` ≈ 20 AWS regions
- `method` ∈ 2-3 values (GET, POST, DELETE)

Worst case: 2 * 3 * 4 * 10 * 20 * 3 = 14,400 series. Well within
Prometheus's recommended ceiling (10k series per metric family,
100k per job).

## Adding a new metric

1. Add the metric to `apps/api/src/constat_api/metrics.py`. Use a
   bounded label set; document the cardinality budget in the
   module docstring.
2. Add a helper function (`record_X(...)`) so call sites stay
   readable. The helper does the label-pinning and `.inc()` /
   `.observe()`.
3. Hook the helper at the call site (runner, collector, CLI).
4. Add a test in `tests/test_metrics.py` covering the helper
   and the exposition.
5. Update this doc with the new metric, the labels, the PromQL
   example, and the alert rule (if any).

## OpenTelemetry migration path (V2)

The strategic brief's ADR-09 calls for OpenTelemetry as the
future observability layer. The V1 metrics in this module are
already the contract — the metric names and labels are the
spec. The migration is:

1. Add `opentelemetry-sdk` and `opentelemetry-exporter-prometheus`
   to `apps/api/pyproject.toml`.
2. Replace the `prometheus_client.Counter` / `Histogram`
   declarations with `opentelemetry.metrics.Counter` /
   `Histogram` instruments, keeping the same `name=` and
   `description=`.
3. Replace `record_X()` helpers with OTel's recommended
   pattern (use a `meter`, instrument at the call site).
4. Wire `opentelemetry-exporter-prometheus` into the
   `/metrics` endpoint — it generates the same exposition
   format, so the Prometheus scrape config does not change.
5. Add an OTel collector sidecar (or use a managed backend) for
   OTLP export.

The dashboards, alert rules, and PromQL queries do not change
because the **metric names and labels are the contract**, not
the library. That's the whole point of the V1 choice.

## What is NOT in V1

- **Histograms for source_run count per region over time.** A
  counter is enough; the rate() function gives you the rate.
  Add a separate gauge if you need the current backlog.
- **RDS Extended Support cost ($/month) per account.** This is
  a domain metric, not an operational one. The pilot's
  customers see this in the UI. We don't expose it via
  /metrics until a customer asks for the alert.
- **Traces.** ADR-09 says V2. The `request_id` (already in
  every log line) is the seed for trace correlation.
- **Authentication for the scraper.** V1 trusts the network.
  V2: `CONSTAT_METRICS_KEY` header check on `/metrics`.
- **Per-tenant labels.** V1 is single-tenant. When we go
  multi-tenant in V2, the metric labels gain `tenant_id`. The
  cardinality cost is real (one series per tenant per existing
  label combo); watch it.

## See also

- [`../architecture.md`](../architecture.md) — the SLO targets
  this module measures
- [`./alerting.md`](./alerting.md) — the 3 Prometheus alerts built on
  these metrics (`deploy/prometheus/alerts.yml`) and their runbooks
- [`../api/endpoints.md`](../api/endpoints.md) — the `/metrics`
  endpoint contract
- [`./logging.md`](./logging.md) — the request_id that
  correlates the metric with the log line
