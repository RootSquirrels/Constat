"""API key authentication + minimal RBAC (V1).

Auth model: callers authenticate with an `X-API-Key` header. Keys are
configured in two ways (they union):

- `CONSTAT_API_KEYS` — comma-separated `name:role:key` entries, the
  RBAC-aware form. Each key resolves to a `Principal(name, role)`.
- `CONSTAT_API_KEY` — legacy single key. Maps to the implicit principal
  ("default", "operator") so existing deployments are unaffected.

Roles (V1, CISO review): `reader` (read endpoints only) and `operator`
(everything, including scans, rule runs, acks, retention actions).
Enforcement: `Depends(verify_api_key)` on read surfaces (any authenticated
principal), `Depends(require_operator)` on write surfaces (403 for readers).

When no key at all is configured (dev mode), auth is OPEN: every request
resolves to the anonymous principal ("anonymous", "operator") so the
local demo and the test-suite keep working, and a warning is logged at
startup. NEVER deploy with auth open. The /health endpoint stays open
regardless (LB health checks must not require auth).

Timing: key verification compares the presented key against EVERY
configured key with `hmac.compare_digest` and never short-circuits on a
match (see `_match_principal`). V2: replace with proper auth — JWT,
OAuth, or mTLS. The dependency interface stays the same so swap is
one-line.
"""

from __future__ import annotations

import hmac
import logging
import os
import warnings
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status

from constat_api.settings import ROLE_OPERATOR, Settings, settings

logger = logging.getLogger(__name__)

# One-time warning at module import. Cheap, prevents accidental prod
# deployment with auth open.
if not settings.api_key and not settings.api_keys:
    if os.getenv("CONSTAT_ENV", "dev") != "dev":
        warnings.warn(
            "CONSTAT_API_KEY / CONSTAT_API_KEYS are not set but CONSTAT_ENV != 'dev'. "
            "The API is unauthenticated. Set CONSTAT_API_KEYS before deploying.",
            stacklevel=1,
        )
    logger.warning("API auth is OPEN (no API keys configured). Do not deploy this way.")


@dataclass(frozen=True)
class Principal:
    """Who is calling. Resolved from X-API-Key by `verify_api_key`.

    `name` comes from the CONSTAT_API_KEYS entry ("alice") or is
    "default" for the legacy single key, "anonymous" when auth is open.
    Read attribution (CISO 3.3) records this name in audit_events.
    """

    name: str
    role: str


# Auth-open principal. Operator role: dev mode must not start 403-ing
# writes — there is nothing to escalate against when no keys exist.
ANONYMOUS_PRINCIPAL = Principal(name="anonymous", role=ROLE_OPERATOR)


def _get_settings() -> Settings:
    """Override-friendly settings accessor.

    Returns the module-level `settings` by default. Tests can override
    via `app.dependency_overrides[_get_settings] = ...`.
    """
    return settings


def _match_principal(x_api_key: str, cfg: Settings) -> Principal | None:
    """Resolve a presented key to its Principal, or None when unknown.

    Compares against EVERY configured key with `hmac.compare_digest`
    and deliberately does NOT break on the first match: the loop always
    runs to the end so response timing does not reveal which entry
    matched or how many keys are configured. (compare_digest still leaks
    key length in theory; configured keys are operator-chosen secrets,
    not user passwords, so this is acceptable for V1 — documented, not
    hidden.)
    """
    matched: Principal | None = None
    for entry in cfg.all_api_key_entries():
        if hmac.compare_digest(x_api_key, entry.key):
            matched = Principal(name=entry.name, role=entry.role)
    return matched


def verify_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    cfg: Settings = Depends(_get_settings),
) -> Principal:
    """Verify the X-API-Key header and resolve the calling Principal.

    Returns ANONYMOUS_PRINCIPAL when no key is configured (dev mode).
    Raises 401 when keys are configured and the presented key is missing
    or unknown. Readers and operators both pass — this is the dependency
    for READ endpoints. Use `require_operator` for write endpoints.
    """
    entries = cfg.all_api_key_entries()
    if not entries:
        return ANONYMOUS_PRINCIPAL  # dev mode
    if x_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-API-Key header is required",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    principal = _match_principal(x_api_key, cfg)
    if principal is None:
        # Don't reveal whether the key was wrong vs missing. Same body
        # for both, same status. Timing is constant-time-ish.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return principal


def require_operator(
    principal: Principal = Depends(verify_api_key),
) -> Principal:
    """Gate WRITE endpoints behind the operator role (V1 RBAC).

    Everything `verify_api_key` does (401 on missing/unknown key), plus
    403 when the authenticated principal is a reader. A reader must not
    be able to trigger a scan, a rule run, an ack, or a retention action.
    """
    if principal.role != ROLE_OPERATOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"principal {principal.name!r} has role 'reader'; "
            "this endpoint requires 'operator'",
        )
    return principal


def verify_metrics_key(
    x_metrics_key: str | None = Header(default=None, alias="X-Metrics-Key"),
    cfg: Settings = Depends(_get_settings),
) -> None:
    """Gate /metrics behind CONSTAT_METRICS_KEY (F-15).

    No-op when `cfg.metrics_key` is None: /metrics then shares the
    /health trust model (scraper on the trusted network) and a warning
    is logged at startup. When set, the scraper must send the key via
    the X-Metrics-Key header; the comparison is constant-time, same as
    the API key. Missing and wrong keys get the same 401 body.
    """
    if cfg.metrics_key is None:
        return
    if x_metrics_key is None or not hmac.compare_digest(x_metrics_key, cfg.metrics_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid metrics key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
