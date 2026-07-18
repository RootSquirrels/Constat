"""Application settings.

V1: env-driven, no secrets manager. The CONSTAT_DATABASE_URL is the main knob.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID

import boto3

# V1 single-tenant. Every row gets this tenant_id. The column is in place
# from day 1 so V2 multi-tenant is a migration of policies, not schema.
DEFAULT_TENANT_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")

# V1 RBAC roles (CISO review): two roles, no DB tables. `reader` may call
# GET/HEAD/OPTIONS endpoints only; `operator` may call everything. Anything
# fancier (per-route grants, a roles table) is V2 — the pilot has ~3 humans.
ROLE_READER = "reader"
ROLE_OPERATOR = "operator"
ROLES: tuple[str, ...] = (ROLE_READER, ROLE_OPERATOR)


@dataclass(frozen=True)
class ApiKeyEntry:
    """One named API key with its role, parsed from CONSTAT_API_KEYS."""

    name: str
    role: str
    key: str


def parse_api_keys(raw: str) -> tuple[ApiKeyEntry, ...]:
    """Parse CONSTAT_API_KEYS: comma-separated `name:role:key` entries.

    Example: "alice:operator:K1,bob:reader:K2".

    Fails loudly (ValueError) on any malformed entry — a typo here means
    someone locked out or over-privileged, both of which must surface at
    startup, not at 3am. The error message names the entry's position and
    name but NEVER the key material.
    """
    entries: list[ApiKeyEntry] = []
    for i, item in enumerate(raw.split(",")):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 2)  # the key itself may contain ':'
        name = parts[0].strip() if parts else ""
        if len(parts) != 3 or not name:
            raise ValueError(
                f"invalid CONSTAT_API_KEYS entry #{i + 1}"
                + (f" (name={name!r})" if name else "")
                + ": expected 'name:role:key'"
            )
        role = parts[1].strip()
        key = parts[2]
        if role not in ROLES:
            raise ValueError(
                f"invalid CONSTAT_API_KEYS entry #{i + 1} (name={name!r}): "
                f"unknown role {role!r} (must be one of {ROLES})"
            )
        if not key:
            raise ValueError(f"invalid CONSTAT_API_KEYS entry #{i + 1} (name={name!r}): empty key")
        entries.append(ApiKeyEntry(name=name, role=role, key=key))
    return tuple(entries)


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "CONSTAT_DATABASE_URL",
        "postgresql://constat:constat@localhost:5432/constat",
    )
    api_title: str = "Constat API"
    # CORS origins for the web app (F-15). Env CONSTAT_CORS_ORIGINS is a
    # comma-separated list; unset falls back to the local Next.js dev
    # server.
    cors_origins: tuple[str, ...] = tuple(
        o.strip() for o in os.getenv("CONSTAT_CORS_ORIGINS", "").split(",") if o.strip()
    ) or ("http://localhost:3000",)
    default_tenant_id: UUID = DEFAULT_TENANT_ID
    # V1 auth: optional API key. When set, every request must carry a
    # matching X-API-Key header. When unset (dev), auth is open — a
    # warning is logged at startup. NEVER deploy without setting this
    # in any environment that an external caller can reach.
    #
    # Legacy single-key form. Maps to an implicit principal
    # ("default", "operator") — see all_api_key_entries().
    api_key: str | None = os.getenv("CONSTAT_API_KEY") or None
    # V1 RBAC (CISO review): named keys with roles, comma-separated
    # `name:role:key` entries (e.g. "alice:operator:K1,bob:reader:K2").
    # Invalid entries raise ValueError at startup (import of this module)
    # — misconfigured auth must never boot silently. Both this and the
    # legacy CONSTAT_API_KEY may be set; they union.
    api_keys: tuple[ApiKeyEntry, ...] = parse_api_keys(os.getenv("CONSTAT_API_KEYS", ""))
    # F-10: POST /insights lets any caller forge an insight without
    # provenance. It exists for tests and local demos, so it is gated
    # behind this explicit opt-in, default OFF. Real insights are
    # written by the rule runner, not this endpoint.
    enable_manual_insights: bool = os.getenv("CONSTAT_ENABLE_MANUAL_INSIGHTS", "").lower() in (
        "1",
        "true",
        "yes",
    )
    # F-15: optional key for /metrics. When set, the scraper must send
    # it via the X-Metrics-Key header. When unset, /metrics stays open
    # (same trust model as /health: the scraper is on the trusted
    # network) and a warning is logged at startup. Set this before
    # exposing /metrics beyond the private network.
    metrics_key: str | None = os.getenv("CONSTAT_METRICS_KEY") or None

    def all_api_key_entries(self) -> tuple[ApiKeyEntry, ...]:
        """Every (name, role, key) the auth layer must accept.

        Union of CONSTAT_API_KEYS entries and the legacy CONSTAT_API_KEY,
        which maps to an implicit ("default", "operator") principal so
        existing single-key deployments keep full access unchanged.
        """
        if self.api_key:
            return (
                *self.api_keys,
                ApiKeyEntry(name="default", role=ROLE_OPERATOR, key=self.api_key),
            )
        return self.api_keys


settings = Settings()


def get_base_aws_session() -> boto3.Session:
    """Build the base boto3 session used for AssumeRole.

    Local dev: CONSTAT_AWS_PROFILE=<name> reads ~/.aws/credentials.
    Prod (ECS/Fargate): use the task IAM role via default chain.
    """
    profile = os.getenv("CONSTAT_AWS_PROFILE")
    if profile:
        return boto3.Session(profile_name=profile)
    return boto3.Session()
