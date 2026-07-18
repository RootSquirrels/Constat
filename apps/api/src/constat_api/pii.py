"""PII classification (V1 security feature, migration 0010).

Per-field sensitivity labels. The first thing a privacy
questionnaire asks: "where does customer PII live and how is it
classified?" The answer this module enables:

- For each (resource_type, resource_id, field_name), record the
  sensitivity level (public / internal / confidential / restricted).
- Store the SHA-256 hash of the value, not the value itself. The
  value lives in the source row (focus_charges.account_id, etc.)
  where the business logic needs it; here we just need a
  stable identifier for "we've seen this value before" and
  "what's the sensitivity of this field?".

V1 rules are explicit and conservative. V2 will:
- Move to a YAML config (the `fact-registry` concept already
  captures related metadata).
- Add per-tenant overrides.
- Maybe auto-detect PII in tag values (regex for emails, etc.).
"""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy.orm import Session

from constat_api.orm import PIIClassificationORM
from constat_api.settings import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)


# Sensitivity levels (ordered: public < internal < confidential < restricted).
PUBLIC = "public"
INTERNAL = "internal"
CONFIDENTIAL = "confidential"
RESTRICTED = "restricted"

ALL_SENSITIVITIES: tuple[str, ...] = (PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED)


# Classification rules. V1: explicit field -> level mapping.
# Anything not in the table defaults to INTERNAL.
#
# "confidential": customer-identifying info, may need explicit
#                consent (DORA, GDPR Art. 9 special categories)
# "internal": business info, not customer-identifying but
#             not public (e.g. instance class, region)
# "public": published service info
#
# V2: load from YAML, allow per-tenant overrides.
SENSITIVITY_RULES: dict[str, str] = {
    # Accounts and ARNs are customer identifiers.
    "account_id": CONFIDENTIAL,
    "aws_account_id": CONFIDENTIAL,
    "arn": CONFIDENTIAL,
    "db_instance_arn": CONFIDENTIAL,
    "sub_account_id": CONFIDENTIAL,
    "billing_account_id": CONFIDENTIAL,
    "billing_account_name": INTERNAL,
    # Resource identifiers.
    "resource_id": CONFIDENTIAL,
    "instance_id": CONFIDENTIAL,
    "db_instance_identifier": CONFIDENTIAL,
    # Tags: the KEYS are internal (Application, CostCenter are
    # well-known names). The VALUES are confidential by default
    # (could be anything — project names, internal codes).
    "tag_key": INTERNAL,
    "tag_value": CONFIDENTIAL,
    "tag": CONFIDENTIAL,
    # RDS instance attributes — public.
    "engine": PUBLIC,
    "engine_version": PUBLIC,
    "instance_class": PUBLIC,
    "region": INTERNAL,
    "pricing_category": INTERNAL,
    "availability_zone": INTERNAL,
    # Cost data — internal (competitively sensitive but not PII).
    "billed_cost": INTERNAL,
    "amortized_cost": INTERNAL,
    "effective_cost": INTERNAL,
}


def _default_sensitivity(field_name: str) -> str:
    """Map a field name to its sensitivity. Conservative default:
    INTERNAL when the field isn't in the table.

    Sub-keys like 'tag:Application' are matched against the prefix
    'tag' (V1 simplification: all tag-related fields default to
    CONFIDENTIAL because the value can be anything).
    """
    if field_name in SENSITIVITY_RULES:
        return SENSITIVITY_RULES[field_name]
    # Sub-key match: "tag:Application" -> "tag"
    if ":" in field_name:
        prefix = field_name.split(":", 1)[0]
        if prefix in SENSITIVITY_RULES:
            return SENSITIVITY_RULES[prefix]
    return INTERNAL


def hash_value(value: str) -> str:
    """SHA-256 hex of a value. Used as a stable identifier without
    storing the value itself."""
    return hashlib.sha256(value.encode()).hexdigest()


def classify(
    field_name: str,
    value: str,
) -> tuple[str, str]:
    """Classify a (field, value) pair.

    Returns:
        (sensitivity, value_hash). Sensitivity is one of
        ALL_SENSITIVITIES; value_hash is the SHA-256 hex of the value.
    """
    sensitivity = _default_sensitivity(field_name)
    return sensitivity, hash_value(value)


class PIIClassifier:
    """Writes classifications to pii_classifications.

    Used by the AWS collector at ingest time: for each (resource,
    field, value) tuple, write a row. The runner does NOT need to
    call this — classifications are written at scan time.
    """

    def __init__(self, session: Session):
        self.session = session

    def record(
        self,
        *,
        resource_type: str,
        resource_id: str,
        field_name: str,
        value: str,
        sensitivity: str | None = None,
    ) -> PIIClassificationORM | None:
        """Record a classification. Returns None if the value is
        empty (nothing to classify)."""
        if not value:
            return None
        if sensitivity is None:
            sensitivity, value_hash = classify(field_name, value)
        else:
            value_hash = hash_value(value)
            if sensitivity not in ALL_SENSITIVITIES:
                raise ValueError(
                    f"invalid sensitivity {sensitivity!r} for {field_name} "
                    f"(must be one of {ALL_SENSITIVITIES})"
                )
        row = PIIClassificationORM(
            tenant_id=DEFAULT_TENANT_ID,
            resource_type=resource_type,
            resource_id=resource_id,
            field_name=field_name,
            sensitivity=sensitivity,
            value_hash=value_hash,
        )
        self.session.add(row)
        return row
