"""Audit logging (V1 security feature, migration 0010).

Append-only "who did what when" log. The first thing a security
questionnaire asks. Every privileged operation records here.

Contract: metadata is a strict dict of (counts, durations, rule
names, region names, etc.). NEVER raw account_id, ARN, tag values,
or any other customer-identifying field. The PII classifier in
pii.py is the canonical source for "what's the sensitivity of
this field" — use it to decide whether a value goes in metadata.

The actor format is "kind:value" — e.g. "api_key:abc123hash",
"system:cleanup_stuck_runs", "system:retention". The hash for
api_key is the SHA-256 of the configured key (so we can answer
"which key accessed X" without storing the secret).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator, Mapping
from typing import Any

from sqlalchemy.orm import Session

from constat_api.db import SessionLocal
from constat_api.orm import AuditEventORM
from constat_api.settings import DEFAULT_TENANT_ID, settings
from constat_api.tenant import bind_tenant

logger = logging.getLogger(__name__)

# System actor kinds. API key actors are formatted as "api_key:<hash>".
ACTOR_SYSTEM_CLEANUP = "system:cleanup_stuck_runs"
ACTOR_SYSTEM_RETENTION = "system:retention"
ACTOR_SYSTEM = "system"

# Read attribution (CISO requirement 3.3): every sensitive API read is
# recorded with this action, actor = the principal's name ("alice",
# "default", or "anonymous" when auth is open).
ACTION_API_READ = "api.read"


# Fields that MUST NOT appear in audit metadata. The list is enforced
# by record() at runtime; an attempt to log a dict containing these
# keys raises ValueError. Extend this list as the privacy review
# surfaces new sensitive fields.
PII_FORBIDDEN_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "account_id",
        "aws_account_id",
        "arn",
        "db_instance_arn",
        "billing_account_id",
        "sub_account_id",
        "tag_value",
        "tag_values",
        "external_id",
        "iam_role_arn",
        "secret_access_key",
        "access_key_id",
        "api_key",
        "value",  # the value of any field — caller must use a key without PII
    }
)


def actor_for_api_key(api_key: str | None) -> str:
    """Format an actor string for an API key. Hashes the key so we
    never store the secret in clear.

    If api_key is None (no auth or system call), returns ACTOR_SYSTEM.
    """
    if not api_key:
        return ACTOR_SYSTEM
    digest = hashlib.sha256(api_key.encode()).hexdigest()[:16]
    return f"api_key:{digest}"


def _validate_metadata(metadata: dict[str, Any]) -> None:
    """Raise ValueError if metadata contains PII keys.

    Defense in depth: the application is supposed to log only
    non-PII metadata. If a future refactor accidentally passes a
    raw value, this check catches it before it hits the DB.
    """
    bad_keys = set(metadata.keys()) & PII_FORBIDDEN_METADATA_KEYS
    if bad_keys:
        raise ValueError(
            f"audit metadata contains forbidden PII keys: {sorted(bad_keys)}. "
            "Use a non-PII key (e.g. account_count, region_name, rule_name) "
            "or hash the value before passing it in."
        )


class AuditLogger:
    """Append-only audit log writer.

    Use the session-scoped instance inside a request handler or a
    background job. The session owns the transaction; we don't commit
    here (caller decides the commit boundary).
    """

    def __init__(self, session: Session):
        self.session = session

    def record(
        self,
        *,
        action: str,
        actor: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEventORM:
        """Record an audit event. Returns the ORM object (caller
        decides whether to flush/commit).

        Args:
            action: short verb, e.g. "scan_completed", "insight_run",
                "cleanup_executed", "retention_applied".
            actor: "kind:value" string. Defaults to ACTOR_SYSTEM.
            target_type: e.g. "account", "resource", "rule", "table".
            target_id: id of the target (no PII).
            metadata: dict of non-PII fields. Counts, durations,
                region names, rule names. Will be validated against
                PII_FORBIDDEN_METADATA_KEYS.
        """
        if metadata is None:
            metadata = {}
        _validate_metadata(metadata)

        event = AuditEventORM(
            tenant_id=DEFAULT_TENANT_ID,
            actor=actor or ACTOR_SYSTEM,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata_json=dict(metadata),
        )
        self.session.add(event)
        return event


# Module-level logger functions for callers that don't need a class
# instance (the cleanup CLI, the Fargate task). They open their own
# session via SessionLocal.


def record_event(
    session: Session,
    *,
    action: str,
    actor: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEventORM:
    """One-shot audit record. Convenience for callers that don't
    want to instantiate AuditLogger. The caller owns the commit."""
    return AuditLogger(session).record(
        action=action,
        actor=actor,
        target_type=target_type,
        target_id=target_id,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Read attribution — "who saw my data" (CISO requirement 3.3)
# ---------------------------------------------------------------------------


def get_audit_db() -> Iterator[Session]:
    """FastAPI dependency: a short, INDEPENDENT session for audit writes.

    Read-attribution writes must NEVER share the request's read session:
    an audit write failure (lock, RLS violation, connection drop) must
    not be able to corrupt or roll back the read it describes. This
    session is opened around the single INSERT and closed immediately.
    """
    session = SessionLocal()
    bind_tenant(session, settings.default_tenant_id)
    try:
        yield session
    finally:
        session.close()


def record_read(
    session: Session,
    *,
    actor: str,
    target_type: str,
    route: str,
    filters: Mapping[str, bool] | None = None,
    row_count: int,
) -> None:
    """Attribute one sensitive READ to its principal.

    One audit_events row per request — fine at pilot volume (tens of
    reads/day). If volume grows, revisit with sampling or aggregation
    rather than dropping attribution.

    Metadata is strictly non-PII per this module's contract: the route
    path template, which filters were present (booleans, never their
    values), and the number of rows returned.

    AVAILABILITY BEATS AUDIT, deliberately: if the audit write fails we
    roll back the audit session, emit a loud warning (this is the signal
    to alert on — an "AUDIT GAP" line means unaudited reads were served),
    and serve the read anyway. A broken audit sink must not take the
    product's read paths down with it.
    """
    metadata: dict[str, Any] = {"route": route, "row_count": row_count}
    for key, present in (filters or {}).items():
        metadata[f"filter_{key}"] = bool(present)
    try:
        record_event(
            session,
            action=ACTION_API_READ,
            actor=actor,
            target_type=target_type,
            metadata=metadata,
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.warning(
            "AUDIT GAP: failed to record api.read on %s for actor %s — "
            "serving the read anyway (availability beats audit)",
            route,
            actor,
            exc_info=True,
        )
