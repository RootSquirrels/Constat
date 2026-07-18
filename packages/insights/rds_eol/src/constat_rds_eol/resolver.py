"""RDS PostgreSQL Extended Support insight.

Returns MATCH (gap found, emits Insight), NO_MATCH (no gap), or INCONCLUSIVE
(missing facts that block assessment). The INCONCLUSIVE branch is critical:
the GTM promise is "we tell you what you don't know about your fleet" —
disappearing silently when facts are missing is exactly the failure mode
the product must avoid (criterion n°15).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from constat_core.catalog.aws import (
    PostgresEOLInfo,
    extended_support_tier,
    postgres_eol_info,
    price_per_vcpu_hour,
)
from constat_core.models import Fact, Insight, Severity, ValueState

RULE_NAME = "rds_eol"

# Alert when EOL is within this window. Beyond this, the upgrade is a roadmap
# item, not an écart.
EOL_ALERT_WINDOW_DAYS = 90

# 730h = average month (365.25 * 24 / 12). Used for cost estimation.
HOURS_PER_MONTH = 730


@dataclass(frozen=True)
class InsightResult:
    """Outcome of evaluating one resource.

    - insights: gaps that should be surfaced (will be inserted into insights table)
    - inconclusive_reasons: facts that, if present, would let us conclude.
      A non-empty list means "we don't know yet" and produces an Inconclusive
      record — never a silent skip.
    """

    insights: list[Insight] = field(default_factory=list)
    inconclusive_reasons: list[str] = field(default_factory=list)

    @property
    def is_conclusive(self) -> bool:
        return not self.inconclusive_reasons

    @property
    def has_gap(self) -> bool:
        return bool(self.insights)


def _index_facts(facts: Iterable[Fact]) -> dict[str, Fact]:
    return {f"{f.namespace}.{f.key}": f for f in facts}


def _get(idx: dict[str, Fact], dotted_key: str) -> Fact | None:
    return idx.get(dotted_key)


def evaluate(
    resource_id: UUID,
    facts: Iterable[Fact],
    *,
    today: date | None = None,
) -> InsightResult:
    """Evaluate one RDS resource.

    Returns an InsightResult with:
    - insights: a single Insight if a gap is found, else []
    - inconclusive_reasons: what's missing, if anything blocks assessment
    """
    idx = _index_facts(facts)

    engine = _get(idx, "aws.rds.engine")
    version = _get(idx, "aws.rds.engine_version")
    vcpu = _get(idx, "aws.rds.vcpu")

    inconclusive: list[str] = []

    # Gate 1: engine must be KNOWN and postgres.
    if engine is None or engine.value_state != ValueState.KNOWN:
        inconclusive.append("aws.rds.engine")
    elif engine.value != "postgres":
        # Definitive NO_MATCH: we know it's not postgres, nothing to say.
        return InsightResult()

    # Gate 2: version must be KNOWN.
    if version is None or version.value_state != ValueState.KNOWN:
        inconclusive.append("aws.rds.engine_version")

    # Gate 3: vcpu must be KNOWN (we can't price without it).
    if vcpu is None or vcpu.value_state != ValueState.KNOWN:
        inconclusive.append("aws.rds.vcpu")

    if inconclusive:
        # We don't have enough to conclude. Don't emit a false negative — emit
        # an Inconclusive so the user sees the gap in their data.
        return InsightResult(insights=[], inconclusive_reasons=inconclusive)

    # Parse major version (e.g. "14.7" -> 14).
    try:
        major = int(str(version.value).split(".")[0])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        return InsightResult(insights=[], inconclusive_reasons=["aws.rds.engine_version.malformed"])

    eol_info = postgres_eol_info(major)
    if eol_info is None:
        # LTS (16+) or unknown version. No alert.
        return InsightResult()

    current = today or date.today()

    if current > eol_info.end_of_extended_support:
        # AWS will force-upgrade. Critical.
        days_to_force = (eol_info.end_of_extended_support - current).days
        return InsightResult(
            insights=[
                _make_insight(
                    resource_id=resource_id,
                    account_id=engine.account_id,  # type: ignore[union-attr]
                    major=major,
                    version_value=version.value,  # type: ignore[arg-type]
                    eol_info=eol_info,
                    current=current,
                    days_to_event=days_to_force,
                    severity=Severity.CRITICAL,
                    title=f"RDS PostgreSQL {major} will be force-upgraded in {days_to_force} days",
                    recommendation=(
                        f"AWS will force-upgrade to {major + 1} on "
                        f"{eol_info.end_of_extended_support.isoformat()}. "
                        f"Upgrade manually now to control timing."
                    ),
                )
            ]
        )

    days_to_eol = (eol_info.eol_date - current).days
    if days_to_eol > EOL_ALERT_WINDOW_DAYS:
        # Not yet urgent. Roadmap item, not an écart.
        return InsightResult()

    if days_to_eol <= 0:
        # Past EOL, still in Extended Support.
        severity = Severity.CRITICAL
        title = f"RDS PostgreSQL {major} is in Extended Support"
        recommendation = f"Upgrade to PostgreSQL {major + 1} LTS now to stop Extended Support fees"
    else:
        severity = Severity.WARNING
        title = f"RDS PostgreSQL {major} reaches EOL in {days_to_eol} days"
        recommendation = (
            f"Plan upgrade to PostgreSQL {major + 1} LTS before {eol_info.eol_date.isoformat()}"
        )

    return InsightResult(
        insights=[
            _make_insight(
                resource_id=resource_id,
                account_id=engine.account_id,  # type: ignore[union-attr]
                major=major,
                version_value=version.value,  # type: ignore[arg-type]
                eol_info=eol_info,
                current=current,
                days_to_event=days_to_eol,
                severity=severity,
                title=title,
                recommendation=recommendation,
            )
        ]
    )


def _make_insight(
    *,
    resource_id: UUID,
    account_id: str | None,
    major: int,
    version_value: str,
    eol_info: PostgresEOLInfo,
    current: date,
    days_to_event: int,
    severity: Severity,
    title: str,
    recommendation: str,
) -> Insight:
    tier = extended_support_tier(eol_info.eol_date, current)
    rate = price_per_vcpu_hour(eol_info, current)

    return Insight(
        rule_name=RULE_NAME,
        resource_id=resource_id,
        account_id=account_id,
        severity=severity,
        title=title,
        payload={
            "engine_version": version_value,
            "major_version": major,
            "eol_date": eol_info.eol_date.isoformat(),
            "end_of_extended_support": eol_info.end_of_extended_support.isoformat(),
            "days_to_event": days_to_event,
            "pricing_tier": tier,
            "pricing_usd_per_vcpu_hour": rate,
            "pricing_tier_label": "year_1_2" if tier == "year_1_2" else "year_3_plus",
            "recommendation": recommendation,
        },
    )
