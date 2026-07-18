# Logging (structured JSON + request_id)

> UX/ops P2 item 9. The V1 rule: every log line is JSON, every
> request has a `request_id` for correlation. Local dev gets a
> colored plain-text renderer so the human eye can scan; prod gets
> JSON for ingestion by the log pipeline.

## What's in every log line (JSON mode)

```json
{
  "event": "Loaded 1000 FOCUS rows from /srv/cur/focus-2026-07.csv",
  "level": "info",
  "logger": "constat_api.cli.focus",
  "timestamp": "2026-07-18T15:30:00.123456Z",
  "request_id": "5b1f0a8e-3a4d-4f5b-8a2e-1c2d3e4f5a6b"
}
```

- `event`: the message (the first arg to `logger.info(...)`)
- `level`: lowercase (`info` / `warning` / `error`)
- `logger`: the stdlib logger name (typically `constat_api.<module>`)
- `timestamp`: ISO 8601, UTC, microsecond precision
- `request_id`: the `X-Request-ID` for the current request, or `null`
  in non-request contexts (CLI, background jobs)

The middleware adds an `access` line per request:

```json
{
  "event": "request.complete",
  "method": "GET",
  "path": "/insights",
  "status": 200,
  "duration_ms": 12.34,
  "request_id": "5b1f0a8e-..."
}
```

Levels for the access line: `5xx → error`, `4xx → warning`,
`2xx/3xx → info`. Exceptions in handlers log `event=request.failed`
at `error` level with the traceback, then re-raise so FastAPI's
default handler returns 500.

## How to enable

```bash
# JSON (prod, CI, anything you want to log-ship)
CONSTAT_LOG_JSON=1 python -m constat_api

# Colored plain text (default; local dev)
python -m constat_api
```

The flag is read at import time in
`apps/api/src/constat_api/logging.py::configure_logging`. The
function is idempotent (safe to call once at startup; subsequent
calls replace handlers).

## What the existing `logger.info(...)` calls get

Every existing `logger.info("Region %s failed: %s", region, e)` in
the codebase gets the new format *for free*. The `logger.<level>(...)`
calls don't need to change. The structlog `ProcessorFormatter`
intercepts stdlib `LogRecord` objects, runs them through the shared
processor chain, and renders them as JSON (or colored text).

Two minor caveats:

- **Extras via `logger.info(..., extra={...})` are NOT extracted into
  top-level JSON fields by default.** The data ends up in `event`
  if you used `%`-formatting, or is dropped otherwise. To get a
  custom field in JSON, switch to `structlog.get_logger().info("event",
  count=42, ...)` at the call site. We don't migrate the codebase
  in this commit — too much churn.
- **Per-callsite `structlog.get_logger()` is the recommended path for
  new code.** The stdlib-style `logger = logging.getLogger(__name__)`
  still works; structlog and stdlib share the same root handler.

## The `X-Request-ID` contract

The middleware (`apps/api/src/constat_api/middleware.py::RequestIDMiddleware`)
behaves as follows:

- **Request with `X-Request-ID`**: the middleware uses the supplied
  value verbatim. This preserves tracing when Constat is called by
  another service (e.g. a frontend SPA, a k8s Job, a Lambda).
- **Request without `X-Request-ID`**: the middleware generates a
  fresh UUID4. The caller can read the generated id from the
  response header.
- **Response header**: every response carries `X-Request-ID` with
  the value used for the request, so the caller can correlate with
  their own logs / error reports.

```bash
# Caller supplies an id
$ curl -H 'X-Request-ID: my-trace-123' http://localhost:8000/insights
HTTP/1.1 200 OK
X-Request-ID: my-trace-123
{"insights": [...]}

# Caller does not
$ curl http://localhost:8000/insights
HTTP/1.1 200 OK
X-Request-ID: 5b1f0a8e-3a4d-4f5b-8a2e-1c2d3e4f5a6b
{"insights": [...]}
```

## How to read the `request_id` from a handler

```python
from fastapi import Request

@router.get("/example")
def example(request: Request) -> dict:
    rid = request.state.request_id  # set by the middleware
    logger.info("handler ran", extra={"rid": rid})
    return {"rid": rid}
```

## Sinks and shipping

The root logger has a single `StreamHandler` writing to `sys.stderr`.
For ECS / k8s / Docker, the platform's log collector (Fluent Bit,
Vector, CloudWatch agent) reads the container's stderr. We don't
write to a file in V1.

If the platform needs JSON on stdout (some collectors prefer
stdout), set `LOG_STREAM=stdout` in the env (TODO: not implemented
in V1; the default is stderr). For now, the deployment's
container-runtime config can redirect `2>&1` to merge streams.

## What is NOT in V1

- **Per-handler metrics.** Not in scope for V1; the existing SLO
  surface (the `/status` endpoint + the `request.complete` log
  line) is enough for the pilot.
- **OpenTelemetry export.** V2. The `request_id` and access log
  are enough for log-based tracing today; spans come when we have
  ≥ 2 services that need to be correlated.
- **Sampling.** V1 logs every request. Add sampling in V2 if
  log volume becomes a cost.

## See also

- [`../api/endpoints.md`](../api/endpoints.md) — the request_id
  contract on the API surface
- [`../development/known-issues.md`](../development/known-issues.md) —
  drift between ORM and migrations
