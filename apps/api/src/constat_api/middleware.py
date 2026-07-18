"""FastAPI middleware: request_id + structlog context propagation
+ HTTP metrics recording.

UX/ops P2 item 9 (request_id, JSON logs) and item 11 (OpenTelemetry
metrics) both live here:

- `RequestIDMiddleware`: every request gets a `request_id`, bound
  to a structlog contextvar, echoed in the response header.
- `HTTPMetricsMiddleware`: records `constat_http_requests_total`
  and `constat_http_request_duration_seconds` per request. The
  `path` label is the FastAPI route template (e.g.
  `/insights/{insight_id}`), not the resolved URL, to keep
  cardinality bounded.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from constat_api.metrics import record_http_request

logger = structlog.get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

# Paths we don't track in HTTP metrics (avoid self-referential noise
# and unbounded-cardinality risk from path-templated URLs that aren't
# yet a registered route).
_EXCLUDED_PATHS: frozenset[str] = frozenset({"/metrics", "/health"})


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Bind a request_id to every log line for the duration of the request.

    Order matters: this is the OUTERMOST middleware so it sees the request
    before any auth / business logic. The `X-Request-ID` is taken from
    the request header (caller-supplied) or generated fresh.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Use the caller-supplied request_id if present (preserves tracing
        # across services). Otherwise generate a UUID4.
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        # Stash on request.state so handlers / dependencies can read it.
        request.state.request_id = request_id

        start = time.monotonic()
        status_code = 500  # default if call_next raises
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            # Log the exception, re-raise so the global handler formats it.
            logger.exception(
                "request.failed",
                method=request.method,
                path=request.url.path,
            )
            raise
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            # Always log the access line, even on exception. Levels:
            # 5xx -> error, 4xx -> warning, 2xx/3xx -> info.
            log = logger.bind(
                method=request.method,
                path=request.url.path,
                status=status_code,
                duration_ms=round(duration_ms, 2),
            )
            if status_code >= 500:
                log.error("request.complete")
            elif status_code >= 400:
                log.warning("request.complete")
            else:
                log.info("request.complete")
            structlog.contextvars.clear_contextvars()

        # Echo the request_id back so the caller can correlate.
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class HTTPMetricsMiddleware(BaseHTTPMiddleware):
    """Record HTTP request count and latency in Prometheus metrics.

    The `path` label uses the FastAPI route template
    (`/insights/{insight_id}`) so unbounded-path URLs don't blow up
    cardinality. The resolved URL `/insights/abc-123` collapses to
    the template. For 404s (no matched route), the path label is
    `"unmatched"` — also bounded.

    `/metrics` and `/health` are excluded: they are scraped by
    infrastructure, not driven by users, and would dominate the
    `http_requests_total` count without telling us anything about
    the product.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path in _EXCLUDED_PATHS:
            return await call_next(request)

        start = time.monotonic()
        status_code = 500
        path_label = "unmatched"
        try:
            response = await call_next(request)
            status_code = response.status_code
            # FastAPI populates `request.scope["route"]` AFTER routing
            # has matched. For 404s (no match), it's absent. We use
            # the template path for cardinality bounding, fall back
            # to "unmatched" for the 404 case.
            route = request.scope.get("route")
            if route is not None:
                path_label = getattr(route, "path", "unmatched")
            return response
        finally:
            duration = time.monotonic() - start
            record_http_request(
                method=request.method,
                path=path_label,
                status_code=status_code,
                duration_seconds=duration,
            )
