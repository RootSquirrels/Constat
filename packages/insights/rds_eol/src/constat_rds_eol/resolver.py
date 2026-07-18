"""RDS PostgreSQL Extended Support insight.

Returns 0 or 1 Insight per resource. Emits WARNING if EOL is within
EOL_ALERT_WINDOW_DAYS, CRITICAL if already past EOL.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from uuid import UUID

from constat_core.catalog.aws import (
    EXT_SUPPORT_USD_PER_VCPU_HOUR,
    POSTGRES_EOL_DATE,
)
from constat_core.models import Fact, Insight, Severity, ValueState

RULE_NAME = "rds_eol"

# Alert when EOL is within this window. Beyond this, the upgrade is a roadmap
# item, not an écart.
EOL_ALERT_WINDOW_DAYS = 90

# 730h = average month (365.25 * 24 / 12). Used for cost estimation.
HOURS_PER_MONTH = 730


def _index_facts(facts: Iterable[Fact]) -> dict[str, Fact]:
    return {f"{f.namespace}.{f.key}": f for f in facts}


def _get(idx: dict[str, Fact], dotted_key: str) -> Fact | None:
    return idx.get(dotted_key)


def evaluate(
    resource_id: UUID, facts: Iterable[Fact], *, today: date | None = None
) -> list[Insight]:
    """Evaluate one RDS resource and return 0 or 1 insight.

    `today` is injectable for tests; defaults to `date.today()`.
    """
    idx = _index_facts(facts)

    engine = _get(idx, "aws.rds.engine")
    version = _get(idx, "aws.rds.engine_version")
    vcpu = _get(idx, "aws.rds.vcpu")

    # Gate 1: engine must be KNOWN and postgres.
    if engine is None or engine.value_state != ValueState.KNOWN or engine.value != "postgres":
        return []

    # Gate 2: version must be KNOWN.
    if version is None or version.value_state != ValueState.KNOWN:
        return []

    # Gate 3: vcpu must be KNOWN (we can't price without it).
    if vcpu is None or vcpu.value_state != ValueState.KNOWN:
        return []

    # Parse major version (e.g. "14.7" -> 14).
    try:
        major = int(str(version.value).split(".")[0])
    except (ValueError, IndexError):
        return []

    eol_date = POSTGRES_EOL_DATE.get(major)
    if eol_date is None:
        # LTS (16+) or unknown version. No alert.
        return []

    current = today or date.today()
    days_to_eol = (eol_date - current).days

    if days_to_eol > EOL_ALERT_WINDOW_DAYS:
        # Not yet urgent. Roadmap item, not an écart.
        return []

    try:
        vcpu_count = int(vcpu.value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return []

    monthly_extra_usd = vcpu_count * EXT_SUPPORT_USD_PER_VCPU_HOUR * HOURS_PER_MONTH

    if days_to_eol <= 0:
        severity = Severity.CRITICAL
        title = f"RDS PostgreSQL {major} is in Extended Support"
        recommendation = "Upgrade to PostgreSQL 16 LTS now to stop Extended Support fees"
    else:
        severity = Severity.WARNING
        title = f"RDS PostgreSQL {major} reaches EOL in {days_to_eol} days"
        recommendation = f"Plan upgrade to PostgreSQL 16 LTS before {eol_date.isoformat()}"

    return [
        Insight(
            rule_name=RULE_NAME,
            resource_id=resource_id,
            account_id=engine.account_id,
            severity=severity,
            title=title,
            payload={
                "engine_version": version.value,
                "major_version": major,
                "eol_date": eol_date.isoformat(),
                "days_to_eol": days_to_eol,
                "vcpu": vcpu_count,
                "ext_support_usd_per_vcpu_hour": EXT_SUPPORT_USD_PER_VCPU_HOUR,
                "ext_support_monthly_usd_estimate": round(monthly_extra_usd, 2),
                "recommendation": recommendation,
            },
        )
    ]
