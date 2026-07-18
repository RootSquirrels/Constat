"""Aurora MySQL/PostgreSQL Extended Support insight.

Same evaluation contract as rds_eol: returns MATCH (gap found, emits
Insight), NO_MATCH (no gap), or INCONCLUSIVE (missing facts that block
assessment). Handles both Aurora engines (aurora-mysql, aurora-
postgresql); any other engine is a definitive NO_MATCH.

Aurora-specific pricing nuance: Aurora MySQL has NO year-3 Extended
Support tier (per the Aurora MySQL release calendar, year-3 start is
"Not applicable"), so it bills the year 1-2 rate for the whole window;
Aurora PostgreSQL gets a year-3 tier from its published start date.
Both are priced per provisioned vCPU-hour (Aurora Serverless per-ACU
pricing is out of scope for V1).

The payload carries an explicit monthly Extended Support cost
(`extended_support_monthly_usd` = vCPU x tier rate x 730h) stamped
`value_basis=ESTIMATED`: the figure is catalog-derived until a FOCUS
line confirms the actual charge (per roadmap vague 1).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from constat_core.catalog.aws import (
    CATALOG_VERSION,
    EngineEOLInfo,
    aurora_mysql_eol_info,
    aurora_postgres_eol_info,
    engine_extended_support_tier,
    engine_price_per_vcpu_hour,
)
from constat_core.models import Fact, Insight, Severity, ValueState

RULE_NAME = "aurora_eol"

# Alert when EOL is within this window. Beyond this, the upgrade is a roadmap
# item, not an écart.
EOL_ALERT_WINDOW_DAYS = 90

# 730h = average month (365.25 * 24 / 12). Used for cost estimation.
HOURS_PER_MONTH = 730

# Engines this rule evaluates. Anything else is a definitive NO_MATCH.
AURORA_ENGINES = ("aurora-mysql", "aurora-postgresql")

# Human-readable upgrade target for Aurora MySQL majors (keys mirror
# AURORA_MYSQL_EOL exactly). Aurora PostgreSQL uses major + 1 instead.
AURORA_MYSQL_NEXT_MAJOR = {2: "Aurora MySQL 3 (MySQL 8.0)", 3: "Aurora MySQL 8.4"}

_ENGINE_DISPLAY_NAMES = {
    "aurora-mysql": "Aurora MySQL",
    "aurora-postgresql": "Aurora PostgreSQL",
}


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


def _parse_aurora_major(engine: str, raw: str) -> int | None:
    """Extract the Aurora major version from an engine_version fact.

    Aurora PostgreSQL versions carry the community major directly
    ("14.9" -> 14). Aurora MySQL versions are Aurora-numbered
    ("2.12.4" -> 2, "3.08.1" -> 3); older fleets may report them
    community-prefixed ("5.7.mysql_aurora.2.11.4" -> 2).
    Returns None when the string cannot be parsed.
    """
    if engine == "aurora-mysql" and "mysql_aurora." in raw:
        raw = raw.split("mysql_aurora.", 1)[1]
    try:
        return int(raw.split(".")[0])
    except (ValueError, IndexError):
        return None


def _upgrade_target(engine: str, major: int) -> str:
    if engine == "aurora-mysql":
        return AURORA_MYSQL_NEXT_MAJOR[major]
    return f"Aurora PostgreSQL {major + 1}"


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

    # Gate 1: engine must be KNOWN and an Aurora engine.
    if engine is None or engine.value_state != ValueState.KNOWN:
        inconclusive.append("aws.rds.engine")
    elif engine.value not in AURORA_ENGINES:
        # Definitive NO_MATCH: we know it's not Aurora, nothing to say.
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

    engine_value = str(engine.value)  # type: ignore[union-attr]
    major = _parse_aurora_major(engine_value, str(version.value))  # type: ignore[union-attr]
    if major is None:
        return InsightResult(insights=[], inconclusive_reasons=["aws.rds.engine_version.malformed"])

    eol_info = (
        aurora_mysql_eol_info(major)
        if engine_value == "aurora-mysql"
        else aurora_postgres_eol_info(major)
    )
    if eol_info is None:
        # In-standard-support or not-yet-catalogued version. No alert.
        return InsightResult()

    display = _ENGINE_DISPLAY_NAMES[engine_value]
    target = _upgrade_target(engine_value, major)
    current = today or date.today()

    if current > eol_info.end_of_extended_support:
        # AWS will force-upgrade. Critical.
        days_to_force = (eol_info.end_of_extended_support - current).days
        return InsightResult(
            insights=[
                _make_insight(
                    resource_id=resource_id,
                    account_id=engine.account_id,  # type: ignore[union-attr]
                    engine_value=engine_value,
                    display=display,
                    major=major,
                    version_value=version.value,  # type: ignore[arg-type]
                    vcpu_count=int(vcpu.value),  # type: ignore[arg-type]
                    eol_info=eol_info,
                    current=current,
                    days_to_event=days_to_force,
                    severity=Severity.CRITICAL,
                    title=f"{display} {major} will be force-upgraded in {days_to_force} days",
                    recommendation=(
                        f"AWS will force-upgrade to {target} on "
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
        title = f"{display} {major} is in Extended Support"
        recommendation = f"Upgrade to {target} now to stop Extended Support fees"
    else:
        severity = Severity.WARNING
        title = f"{display} {major} reaches EOL in {days_to_eol} days"
        recommendation = f"Plan upgrade to {target} before {eol_info.eol_date.isoformat()}"

    return InsightResult(
        insights=[
            _make_insight(
                resource_id=resource_id,
                account_id=engine.account_id,  # type: ignore[union-attr]
                engine_value=engine_value,
                display=display,
                major=major,
                version_value=version.value,  # type: ignore[arg-type]
                vcpu_count=int(vcpu.value),  # type: ignore[arg-type]
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
    engine_value: str,
    display: str,
    major: int,
    version_value: str,
    vcpu_count: int,
    eol_info: EngineEOLInfo,
    current: date,
    days_to_event: int,
    severity: Severity,
    title: str,
    recommendation: str,
) -> Insight:
    tier = engine_extended_support_tier(eol_info, current)
    rate = engine_price_per_vcpu_hour(eol_info, current)
    monthly_usd = round(vcpu_count * rate * HOURS_PER_MONTH, 2)

    return Insight(
        rule_name=RULE_NAME,
        resource_id=resource_id,
        account_id=account_id,
        severity=severity,
        title=title,
        payload={
            "engine": engine_value,
            "engine_display": display,
            "engine_version": version_value,
            "major_version": major,
            "eol_date": eol_info.eol_date.isoformat(),
            "end_of_extended_support": eol_info.end_of_extended_support.isoformat(),
            "days_to_event": days_to_event,
            "pricing_tier": tier,
            "pricing_usd_per_vcpu_hour": rate,
            "pricing_tier_label": "year_1_2" if tier == "year_1_2" else "year_3_plus",
            "vcpu_count": vcpu_count,
            "extended_support_monthly_usd": monthly_usd,
            # Catalog-derived estimate until a FOCUS line confirms the actual
            # charge; the roadmap flips this to ACTUAL on reconciliation.
            "value_basis": "ESTIMATED",
            "recommendation": recommendation,
            # Source-of-truth stamp: which catalog version produced this insight.
            "catalog_version": CATALOG_VERSION,
        },
    )
