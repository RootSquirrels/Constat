"""RDS MySQL Extended Support insight.

Same evaluation contract as rds_eol: returns MATCH (gap found, emits
Insight), NO_MATCH (no gap), or INCONCLUSIVE (missing facts that block
assessment). The INCONCLUSIVE branch is critical: the GTM promise is "we
tell you what you don't know about your fleet" — disappearing silently
when facts are missing is exactly the failure mode the product must
avoid (criterion n°15).

Unlike rds_eol, the payload carries an explicit monthly Extended Support
cost (`extended_support_monthly_usd` = vCPU x tier rate x 730h) stamped
`value_basis=ESTIMATED`: the figure is catalog-derived until a FOCUS
line confirms the actual charge (per roadmap vague 1).

Region honesty (same as rds_eol): Extended Support pricing is not
region-uniform, so the aws.rds.region fact is mandatory — missing/UNKNOWN
region = INCONCLUSIVE. Facts written before the collector emitted the
region fact lack it; the next daily scan heals them. An uncatalogued
region still matches on the us-east-1 fallback grid with
`price_region_exact: false` (`pricing_region` says which grid priced it).
Amounts are USD (`source_currency`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from constat_core.catalog.aws import (
    CATALOG_VERSION,
    EngineEOLInfo,
    engine_extended_support_tier,
    es_price_per_vcpu_hour,
    mysql_eol_info,
)
from constat_core.models import Fact, Insight, Severity, ValueState

RULE_NAME = "mysql_eol"

# Alert when EOL is within this window. Beyond this, the upgrade is a roadmap
# item, not an écart.
EOL_ALERT_WINDOW_DAYS = 90

# 730h = average month (365.25 * 24 / 12). Used for cost estimation.
HOURS_PER_MONTH = 730

# Upgrade target for each catalogued MySQL major (RDS supports
# 5.7 -> 8.0 -> 8.4 major upgrades). Keys mirror MYSQL_EOL exactly.
NEXT_MAJOR = {"5.7": "8.0", "8.0": "8.4"}


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
    region = _get(idx, "aws.rds.region")

    inconclusive: list[str] = []

    # Gate 1: engine must be KNOWN and mysql.
    if engine is None or engine.value_state != ValueState.KNOWN:
        inconclusive.append("aws.rds.engine")
    elif engine.value != "mysql":
        # Definitive NO_MATCH: we know it's not mysql, nothing to say.
        return InsightResult()

    # Gate 2: version must be KNOWN.
    if version is None or version.value_state != ValueState.KNOWN:
        inconclusive.append("aws.rds.engine_version")

    # Gate 3: vcpu must be KNOWN (we can't price without it).
    if vcpu is None or vcpu.value_state != ValueState.KNOWN:
        inconclusive.append("aws.rds.vcpu")

    # Gate 4: region must be KNOWN — Extended Support pricing is not
    # region-uniform, so we can't price honestly without knowing the
    # region. Facts written before the collector emitted this fact are
    # healed by the next daily scan.
    if region is None or region.value_state != ValueState.KNOWN:
        inconclusive.append("aws.rds.region")

    if inconclusive:
        # We don't have enough to conclude. Don't emit a false negative — emit
        # an Inconclusive so the user sees the gap in their data.
        return InsightResult(insights=[], inconclusive_reasons=inconclusive)

    # Parse major version: for MySQL the major is X.Y (e.g. "8.0.42" -> "8.0").
    parts = str(version.value).split(".")  # type: ignore[union-attr]
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return InsightResult(insights=[], inconclusive_reasons=["aws.rds.engine_version.malformed"])
    major = f"{parts[0]}.{parts[1]}"

    eol_info = mysql_eol_info(major)
    if eol_info is None:
        # In-standard-support or not-yet-catalogued version. No alert.
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
                    vcpu_count=int(vcpu.value),  # type: ignore[arg-type]
                    region=str(region.value),  # type: ignore[union-attr]
                    eol_info=eol_info,
                    current=current,
                    days_to_event=days_to_force,
                    severity=Severity.CRITICAL,
                    title=f"RDS MySQL {major} will be force-upgraded in {days_to_force} days",
                    recommendation=(
                        f"AWS will force-upgrade to MySQL {NEXT_MAJOR[major]} on "
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
        title = f"RDS MySQL {major} is in Extended Support"
        recommendation = f"Upgrade to MySQL {NEXT_MAJOR[major]} now to stop Extended Support fees"
    else:
        severity = Severity.WARNING
        title = f"RDS MySQL {major} reaches EOL in {days_to_eol} days"
        recommendation = (
            f"Plan upgrade to MySQL {NEXT_MAJOR[major]} before {eol_info.eol_date.isoformat()}"
        )

    return InsightResult(
        insights=[
            _make_insight(
                resource_id=resource_id,
                account_id=engine.account_id,  # type: ignore[union-attr]
                major=major,
                version_value=version.value,  # type: ignore[arg-type]
                vcpu_count=int(vcpu.value),  # type: ignore[arg-type]
                region=str(region.value),  # type: ignore[union-attr]
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
    major: str,
    version_value: str,
    vcpu_count: int,
    region: str,
    eol_info: EngineEOLInfo,
    current: date,
    days_to_event: int,
    severity: Severity,
    title: str,
    recommendation: str,
) -> Insight:
    tier = engine_extended_support_tier(eol_info, current)
    rate, pricing_region, region_exact = es_price_per_vcpu_hour(tier, region)
    monthly_usd = round(vcpu_count * rate * HOURS_PER_MONTH, 2)

    return Insight(
        rule_name=RULE_NAME,
        resource_id=resource_id,
        account_id=account_id,
        severity=severity,
        title=title,
        payload={
            "engine": "mysql",
            "engine_version": version_value,
            "major_version": major,
            "eol_date": eol_info.eol_date.isoformat(),
            "end_of_extended_support": eol_info.end_of_extended_support.isoformat(),
            "days_to_event": days_to_event,
            "pricing_tier": tier,
            "pricing_usd_per_vcpu_hour": rate,
            "pricing_tier_label": "year_1_2" if tier == "year_1_2" else "year_3_plus",
            "pricing_region": pricing_region,
            "price_region_exact": region_exact,
            "source_currency": "USD",
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
