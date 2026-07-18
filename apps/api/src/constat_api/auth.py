"""API key authentication (V1).

V1 is a single-tenant pilot. Auth is a single shared API key passed via
the `X-API-Key` header. The expected key is read from the env var
`CONSTAT_API_KEY` and compared in constant time.

When `CONSTAT_API_KEY` is unset (dev mode), auth is open and a warning
is logged. This is convenient for the local demo and tests, but NEVER
deploy with auth open. The /health endpoint stays open regardless (LB
health checks must not require auth).

V2: replace with proper auth — JWT, OAuth, or mTLS. The interface
(Depends(verify_api_key)) stays the same so swap is one-line.
"""

from __future__ import annotations

import hmac
import logging
import os
import warnings

from fastapi import Depends, Header, HTTPException, status

from constat_api.settings import Settings, settings

logger = logging.getLogger(__name__)

# One-time warning at module import. Cheap, prevents accidental prod
# deployment with auth open.
if not settings.api_key:
    if os.getenv("CONSTAT_ENV", "dev") != "dev":
        warnings.warn(
            "CONSTAT_API_KEY is not set but CONSTAT_ENV != 'dev'. "
            "The API is unauthenticated. Set CONSTAT_API_KEY before deploying.",
            stacklevel=1,
        )
    logger.warning("API auth is OPEN (CONSTAT_API_KEY unset). Do not deploy this way.")


def _get_settings() -> Settings:
    """Override-friendly settings accessor.

    Returns the module-level `settings` by default. Tests can override
    via `app.dependency_overrides[_get_settings] = ...`.
    """
    return settings


def verify_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    cfg: Settings = Depends(_get_settings),
) -> None:
    """Verify the X-API-Key header against the configured key.

    No-op when `cfg.api_key` is None (dev mode). Returns 401 otherwise.
    Uses `hmac.compare_digest` to avoid timing leaks.
    """
    if cfg.api_key is None:
        return  # dev mode
    if x_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-API-Key header is required",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if not hmac.compare_digest(x_api_key, cfg.api_key):
        # Don't reveal whether the key was wrong vs missing. Same body
        # for both, same status. Timing is constant-time.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
