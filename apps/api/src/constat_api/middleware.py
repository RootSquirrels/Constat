"""FastAPI middleware: request_id + structlog context propagation.

UX/ops P2 item 9: structured logging with a correlation ID. Every
HTTP request gets a `request_id` (from `X-Request-ID` if the caller
provides one, otherwise a fresh UUID4). The id is:

1. Bound to a `structlog.contextvars` contextvar, so every log line
   emitted during the request — including from stdlib `logger.info(...)`
   — gets `request_id=<id>` in its JSON output.
2. Echoed back to the caller in the `X-Request-ID` response header,
   so the client can correlate with their own logs / error reports.
3. Logged at request start and end (path, method, status, duration).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = structlog.get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


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
