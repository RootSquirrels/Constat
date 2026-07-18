"""Structured logging setup (UX/ops P2 item 9).

Wraps stdlib `logging` with a structlog JSON formatter so every
log line is machine-parseable. The existing `logger.info(...)`
calls throughout the codebase automatically get the new format
without any per-callsite changes.

What's in the JSON line:
- `event`: the message
- `level`: the log level (lowercase)
- `timestamp`: ISO 8601 in UTC
- `logger`: the logger name
- `request_id`: the X-Request-ID for the current request (set by
  the middleware in `middleware.py`), or null in non-request contexts
- plus any `extra={...}` kwargs passed to the log call (as keyword
  fields)

Enable / disable JSON:
- JSON is on by default when `CONSTAT_LOG_JSON=1` (set by the
  docker-compose / ECS task definition in prod).
- Local dev defaults to a colored plain-text formatter for
  readability.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog


def configure_logging(*, level: str = "INFO") -> None:
    """Configure stdlib + structlog. Idempotent (safe to call once at startup)."""
    use_json = os.getenv("CONSTAT_LOG_JSON", "").lower() in ("1", "true", "yes")

    # Shared processors run for both JSON and pretty output.
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,  # 'level' from the stdlib level
        structlog.stdlib.add_logger_name,  # 'logger' from the stdlib logger name
        structlog.processors.TimeStamper(fmt="iso", utc=True),  # 'timestamp'
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if use_json:
        # Machine-parseable JSON, one object per line.
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        # Human-friendly console output for local dev.
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    # Formatter used by the stdlib handler.
    formatter = structlog.stdlib.ProcessorFormatter(
        # foreign_pre_chain: processors applied to records coming from
        # stdlib logging (e.g. `logger.info("...")` from the codebase).
        foreign_pre_chain=shared_processors,
        processors=[
            # Strip _record and _from_structlog metadata after rendering.
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    # Replace existing handlers (FastAPI / uvicorn install theirs at import).
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level)

    # uvicorn installs its own loggers; route them through our formatter too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        named_logger = logging.getLogger(name)
        named_logger.handlers = [handler]
        named_logger.propagate = False

    # Configure structlog itself (for any direct `structlog.get_logger().info(...)` calls).
    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
