"""Generic Extended Support (EOL) rule evaluator.

The single home of the EOL pipeline: fact gates, force-upgrade branch,
alert-window branch, in-extended-support branch, payload assembly.
The three V1 EOL rules (`rds_eol`, `mysql_eol`, `aurora_eol`) are
~30 lines of `EngineEolMatcher` config + a thin wrapper that calls
`evaluate_eol`. Adding a new engine = one matcher; adding a new EOL
rule for the same engine family = one config.

Chantier III.1 of the roadmap consolidation: the arithmetic
`vcpu x tier rate x 730h` lives here, not in the rule packages.
A regression that drops the multiplication (the rds_eol tiering
refactor that prompted this consolidation) breaks here, in one
place, and the test suite pins it.

The 4 fact gates (engine, engine_version, vcpu, region) are
identical across the three V1 rules: KNOWN state is required for
each; UNKNOWN emits an inconclusive reason. The three branches
(force-upgrade, in-extended-support, pre-EOL window) are identical
except for the engine-specific display name and the upgrade
target. The payload structure is identical except for the engine
field and the EOL info type.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from constat_core.models import Fact, Insight, Severity
from constat_core.namespaces import ValueState

# 730h = average month (365.25 * 24 / 12). The whole EOL cost
# estimate is built from this constant; the 3 V1 rules all imported
# their own copy, which is exactly the duplication the refactor
# removes. See commit-message §III.1 for the regression story.
HOURS_PER_MONTH = 730


@dataclass(frozen=True)
class EolInsightResult:
    """Outcome of evaluating one resource for an EOL rule.

    - insights: gaps that should be surfaced (will be inserted into
      the `insights` table by the runner).
    - inconclusive_reasons: fact keys that, if present, would let
      us conclude. A non-empty list means "we don't know yet" and
      produces an Inconclusive record — never a silent skip.
    """

    insights: list[Insight] = field(default_factory=list)
    inconclusive_reasons: list[str] = field(default_factory=list)

    @property
    def is_conclusive(self) -> bool:
        return not self.inconclusive_reasons

    @property
    def has_gap(self) -> bool:
        return bool(self.insights)


# The EOL info type is engine-specific (`PostgresEOLInfo` for
# standalone RDS PG, `EngineEOLInfo` for MySQL / Aurora). Both share
# the four fields the shared function reads
# (`eol_date`, `year_1_2_usd_per_vcpu_hour`, `year_3_plus_usd_per_vcpu_hour`,
# `end_of_extended_support`); the static type is `Any` to keep the
# function portable across both. Runtime duck-typing.
EolInfoT = Any

LookupFn = Callable[[Any], EolInfoT | None]
ParseMajorFn = Callable[[str], Any | None]
FormatMajorFn = Callable[[Any], str]
UpgradeTargetFn = Callable[[Any], str]
ComputeTierFn = Callable[[EolInfoT, date], str]
RegionPriceFn = Callable[[str, str | None], tuple[float, str, bool]]


@dataclass(frozen=True)
class EngineEolMatcher:
    """All the engine-specific knowledge for one engine value.

    The shared EOL pipeline (gates, force-upgrade branch, alert
    window, in-ES branch, payload assembly) lives in `evaluate_eol`;
    this dataclass is the engine-specific data + the small set of
    functions that vary by engine. Adding a new engine = one matcher;
    no change to the shared function.
    """

    # The engine value that selects this matcher (e.g. "postgres",
    # "mysql", "aurora-mysql", "aurora-postgresql"). The shared
    # function dispatches on this.
    engine_value: str
    # Human-readable name for titles and payloads
    # ("RDS PostgreSQL", "RDS MySQL", "Aurora MySQL", "Aurora PostgreSQL").
    display_name: str
    # FOCUS service catalog canonical (roadmap-consolidation §II.1).
    # Surfaced in the payload as `service_canonical` for cross-provider
    # grouping.
    service_canonical: str
    # Look up the EOL info for the parsed major, or None if the
    # version is in standard support or not in the catalog.
    lookup_eol_info: LookupFn
    # Parse the major from a raw engine_version fact value. Returns
    # None on malformed input; the shared function emits an
    # inconclusive reason in that case.
    parse_major: ParseMajorFn
    # Human-readable major for titles (e.g. 14 -> "14",
    # "8.0" -> "8.0", 2 -> "2"). The shared function passes the
    # result through f-string interpolation either way; the formatter
    # is for engines that want a custom display ("Aurora MySQL 3"
    # for major=2 etc.).
    format_major: FormatMajorFn
    # Upgrade-target string for the recommendation (e.g.
    # "PostgreSQL 15", "MySQL 8.4", "Aurora MySQL 3 (MySQL 8.0)").
    upgrade_target: UpgradeTargetFn
    # Tier function: maps (EolInfo, today) -> 'year_1_2' | 'year_3_plus'.
    compute_tier: ComputeTierFn
    # Price function: maps (tier, region) -> (rate, region_used, exact).
    # Defaults to the AWS Extended Support grid; engines with their
    # own pricing (none today) override.
    price_per_vcpu_hour: RegionPriceFn
    # Payload key for the vCPU count. The V1 rules diverge:
    # rds_eol emits `vcpu`; mysql_eol / aurora_eol emit `vcpu_count`.
    # The monetary test suite pins the key per-rule, so the matcher
    # controls the key explicitly. New engines: pick the convention
    # (`vcpu_count` is the newer, more explicit one).
    vcpu_payload_key: str = "vcpu_count"
    # Value basis the payload writes for the monetary key. "ESTIMATED"
    # (catalog-derived) is the V1 default; None omits the field
    # entirely (rds_eol's original behavior, pinned by
    # `test_reconcile_with_azure_focus_is_noop_not_error`).
    value_basis: str | None = "ESTIMATED"


@dataclass(frozen=True)
class EolRuleConfig:
    """The rule-level config: name, engines it evaluates, alert window."""

    rule_name: str
    # One or more engines; the shared function picks the matcher
    # whose engine_value matches the resource's engine fact.
    # (A single rule can cover several engines, e.g. aurora_eol
    # evaluates both aurora-mysql and aurora-postgresql.)
    engines: tuple[EngineEolMatcher, ...]
    # Days before EOL to start alerting. 90 by default.
    alert_window_days: int = 90
    # Value basis for the monetary key. "ESTIMATED" (catalog-derived)
    # by default; "ACTUAL" would come from FOCUS reconciliation
    # (out of V1 scope for the EOL rules — they always emit
    # ESTIMATED because they evaluate before the cost is incurred).
    value_basis: str = "ESTIMATED"


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _index_facts(facts: Iterable[Fact]) -> dict[str, Fact]:
    return {f"{f.namespace}.{f.key}": f for f in facts}


def _get(idx: dict[str, Fact], dotted_key: str) -> Fact | None:
    return idx.get(dotted_key)


def _select_matcher(config: EolRuleConfig, engine_value: str) -> EngineEolMatcher | None:
    """Pick the matcher whose `engine_value` equals the resource's
    engine fact. None when the engine is not in the rule's set
    (a definitive NO_MATCH — the rule has nothing to say)."""
    for m in config.engines:
        if m.engine_value == engine_value:
            return m
    return None


def _make_insight(
    *,
    config: EolRuleConfig,
    matcher: EngineEolMatcher,
    resource_id: UUID,
    account_id: str | None,
    major: Any,
    version_value: str,
    vcpu_count: int,
    region: str | None,
    eol_info: EolInfoT,
    current: date,
    days_to_event: int,
    severity: Severity,
    title: str,
    recommendation: str,
    catalog_version: str | None = None,
) -> Insight:
    """Build the EOL insight. The single home of the monthly-cost
    arithmetic and the payload structure."""
    tier = matcher.compute_tier(eol_info, current)
    rate, pricing_region, region_exact = matcher.price_per_vcpu_hour(tier, region)
    # The number the whole product exists to produce. Chantier III.1
    # of the roadmap consolidation moved this single multiplication
    # from three rule packages to this one location. The previous
    # duplication hid a regression where the rds_eol tiering refactor
    # dropped the vcpu x rate line and no test caught it (the audit
    # committee did, in `tests/test_monetary_extraction.py`).
    monthly_usd = round(vcpu_count * rate * HOURS_PER_MONTH, 2)

    payload: dict[str, Any] = {
        "engine": matcher.engine_value,
        "engine_display": matcher.display_name,
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
        # Per-matcher payload key: rds_eol uses `vcpu`, the newer rules
        # use `vcpu_count`. The test suite pins the key per-rule; the
        # matcher controls it explicitly.
        matcher.vcpu_payload_key: vcpu_count,
        "extended_support_monthly_usd": monthly_usd,
        "recommendation": recommendation,
        "catalog_version": catalog_version,
        # FOCUS service catalog canonical (roadmap-consolidation §II.1).
        # Surfaced for cross-provider grouping; the consumer UI may
        # group insights by canonical to fold AWS + Azure into a
        # single "managed_postgres" line item.
        "service_canonical": matcher.service_canonical,
    }
    if matcher.value_basis is not None:
        # `config.value_basis` is the rule-level default ("ESTIMATED"
        # for all V1 rules); the matcher-level field overrides per
        # engine (rds_eol historically omitted the field).
        payload["value_basis"] = matcher.value_basis or config.value_basis
    return Insight(
        rule_name=config.rule_name,
        resource_id=resource_id,
        account_id=account_id,
        severity=severity,
        title=title,
        payload=payload,
    )


def evaluate_eol(
    resource_id: UUID,
    facts: Iterable[Fact],
    config: EolRuleConfig,
    *,
    today: date | None = None,
    catalog_version: str | None = None,
) -> EolInsightResult:
    """The single home of the EOL pipeline.

    Behavior is identical to the per-rule evaluators this function
    replaced; the three V1 test suites (rds_eol, mysql_eol,
    aurora_eol) pass unchanged.

    Pipeline (identical across the V1 rules):
    1. Gate the 4 facts: engine (must match a matcher's engine_value),
       engine_version, vcpu, region. UNKNOWN state on any of the
       non-engine gates emits an inconclusive reason.
    2. Parse the major from the raw engine_version. Returns
       inconclusive on malformed input.
    3. Look up the EOL info via the matcher's catalog. None means
       the version is in standard support — no alert.
    4. Three branches in priority order:
       - past `end_of_extended_support`: force-upgrade (CRITICAL)
       - past `eol_date` (in extended support): CRITICAL
       - within `alert_window_days` of `eol_date`: WARNING
       - else: beyond the alert window, no alert.
    5. Build the insight via the single `_make_insight` helper. The
       arithmetic `vcpu x tier rate x 730h` lives there.
    """
    idx = _index_facts(facts)

    engine = _get(idx, "aws.rds.engine")
    version = _get(idx, "aws.rds.engine_version")
    vcpu = _get(idx, "aws.rds.vcpu")
    region = _get(idx, "aws.rds.region")

    inconclusive: list[str] = []

    # Gate 1: engine must be KNOWN and match a matcher's engine_value.
    if engine is None or engine.value_state != ValueState.KNOWN:
        inconclusive.append("aws.rds.engine")
    else:
        matcher = _select_matcher(config, str(engine.value))
        if matcher is None:
            # Definitive NO_MATCH: we know the engine, it's just not
            # one this rule evaluates. No alert, no inconclusive.
            return EolInsightResult()

    # Gate 2: version must be KNOWN.
    if version is None or version.value_state != ValueState.KNOWN:
        inconclusive.append("aws.rds.engine_version")

    # Gate 3: vcpu must be KNOWN (we can't price without it).
    if vcpu is None or vcpu.value_state != ValueState.KNOWN:
        inconclusive.append("aws.rds.vcpu")

    # Gate 4: region must be KNOWN — Extended Support pricing is
    # not region-uniform, so we can't price honestly without knowing
    # the region. Facts written before the collector emitted this
    # fact are healed by the next daily scan.
    if region is None or region.value_state != ValueState.KNOWN:
        inconclusive.append("aws.rds.region")

    if inconclusive:
        # We don't have enough to conclude. Don't emit a false
        # negative — emit Inconclusive so the user sees the gap in
        # their data.
        return EolInsightResult(insights=[], inconclusive_reasons=inconclusive)

    # Type narrow: after the inconclusive check, all 4 facts are KNOWN.
    assert engine is not None
    assert version is not None
    assert vcpu is not None
    assert region is not None
    matcher = _select_matcher(config, str(engine.value))
    assert matcher is not None  # guarded by Gate 1's NO_MATCH return

    # Parse the major. The matcher knows the engine-specific shape
    # (rds: "14.7" -> 14, mysql: "8.0.42" -> "8.0", aurora-mysql:
    # "5.7.mysql_aurora.2.11.4" -> 2 with the prefix stripped).
    major = matcher.parse_major(str(version.value))
    if major is None:
        return EolInsightResult(
            insights=[], inconclusive_reasons=["aws.rds.engine_version.malformed"]
        )

    # The vCPU count is a fact (a JSON value, not a schema). Gate 3
    # guarantees the fact is KNOWN, but the value can still be
    # malformed ("not-a-number"). Without it we cannot price the
    # finding — INCONCLUSIVE, never a silent skip.
    try:
        vcpu_count = int(vcpu.value)
    except (TypeError, ValueError):
        return EolInsightResult(insights=[], inconclusive_reasons=["aws.rds.vcpu.malformed"])

    eol_info = matcher.lookup_eol_info(major)
    if eol_info is None:
        # In standard support or not-yet-catalogued version. No alert.
        return EolInsightResult()

    current = today or date.today()
    target = matcher.upgrade_target(major)
    display = matcher.display_name
    formatted_major = matcher.format_major(major)
    version_value = str(version.value)
    region_value = str(region.value) if region.value is not None else None
    account_id = engine.account_id

    if current > eol_info.end_of_extended_support:
        # AWS will force-upgrade. Critical.
        days_to_force = (eol_info.end_of_extended_support - current).days
        return EolInsightResult(
            insights=[
                _make_insight(
                    config=config,
                    matcher=matcher,
                    resource_id=resource_id,
                    account_id=account_id,
                    major=major,
                    version_value=version_value,
                    vcpu_count=vcpu_count,
                    region=region_value,
                    eol_info=eol_info,
                    current=current,
                    days_to_event=days_to_force,
                    severity=Severity.CRITICAL,
                    title=f"{display} {formatted_major} will be force-upgraded in {days_to_force} days",
                    recommendation=(
                        f"AWS will force-upgrade to {target} on "
                        f"{eol_info.end_of_extended_support.isoformat()}. "
                        f"Upgrade manually now to control timing."
                    ),
                    catalog_version=catalog_version,
                )
            ]
        )

    days_to_eol = (eol_info.eol_date - current).days
    if days_to_eol > config.alert_window_days:
        # Not yet urgent. Roadmap item, not an écart.
        return EolInsightResult()

    if days_to_eol <= 0:
        # Past EOL, still in Extended Support.
        return EolInsightResult(
            insights=[
                _make_insight(
                    config=config,
                    matcher=matcher,
                    resource_id=resource_id,
                    account_id=account_id,
                    major=major,
                    version_value=version_value,
                    vcpu_count=vcpu_count,
                    region=region_value,
                    eol_info=eol_info,
                    current=current,
                    days_to_event=days_to_eol,
                    severity=Severity.CRITICAL,
                    title=f"{display} {formatted_major} is in Extended Support",
                    recommendation=(f"Upgrade to {target} now to stop Extended Support fees"),
                    catalog_version=catalog_version,
                )
            ]
        )

    return EolInsightResult(
        insights=[
            _make_insight(
                config=config,
                matcher=matcher,
                resource_id=resource_id,
                account_id=account_id,
                major=major,
                version_value=version_value,
                vcpu_count=vcpu_count,
                region=region_value,
                eol_info=eol_info,
                current=current,
                days_to_event=days_to_eol,
                severity=Severity.WARNING,
                title=f"{display} {formatted_major} reaches EOL in {days_to_eol} days",
                recommendation=(f"Plan upgrade to {target} before {eol_info.eol_date.isoformat()}"),
                catalog_version=catalog_version,
            )
        ]
    )
