"""Generic storage cost rule evaluator.

The single home of the storage cost pipeline: fact gates, NO_MATCH
check, monthly cost arithmetic, severity assignment, payload
assembly. The three V1 storage rules (`ebs_gp2_to_gp3`,
`ebs_unattached`, `snapshot_orphan`) are ~30 lines of
`StorageRuleConfig` config + a thin wrapper that calls
`evaluate_storage`. Adding a new storage rule = one config; adding
a new price region to an existing rule = a catalog change, no
code change here.

Chantier III.2 of the roadmap consolidation: the
`size_gb x $/GB-month` arithmetic and the $500/$50 severity
thresholds live here, not in three rule packages. A regression
that drops the multiplication (or the region grid) breaks here,
in one place, and the test suite pins it.

The "required facts" pattern from EOL applies to storage with
per-rule `required_facts`: each rule declares which facts it
needs; the shared function gates on KNOWN state and emits the
inconclusive reason. NO_MATCH (e.g. volume_type != "gp2", or
volume_exists, or state != "available") is a per-rule
`should_emit` predicate, not a hard-coded switch.

Severity thresholds ($500/CRITICAL, $50/WARNING) are the
`StorageRuleConfig` defaults; all three V1 rules use the same
thresholds for dashboard consistency. The dashboard sorts by $
to surface the biggest wins regardless of severity.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from constat_core.models import Fact, Insight, Severity
from constat_core.namespaces import ValueState


@dataclass(frozen=True)
class StorageInsightResult:
    """Outcome of evaluating one resource for a storage rule.

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


class StorageInconclusiveError(Exception):
    """Raised by `should_emit` or `compute_cost` to surface a fact
    value that's present but malformed (or any other reason a rule
    can't compute its cost without losing the evidence). The
    shared function catches it and emits the reason as
    INCONCLUSIVE — never a silent skip, never a guessed $0."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class StorageCost:
    """The dollar figure the rule is built to produce, plus the
    catalog honesty metadata that's always surfaced in the payload.

    `monthly_usd` is the number the dashboard shows.
    `monetary_payload_key` is the payload key it's written under
    (per-rule: `savings_monthly_usd`, `monthly_waste_usd`, or
    `orphan_snapshot_monthly_usd`). The `MONETARY` registry
    (`constat_core.monetary`) lists the V1 keys.
    `pricing_region` and `price_region_exact` are the catalog
    honesty metadata.
    `extras` carries the rule-specific payload fields
    (savings breakdown, volume_type, age_days, etc.) that the
    shared function doesn't know about.
    """

    monthly_usd: float
    monetary_payload_key: str
    pricing_region: str
    price_region_exact: bool
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StorageRuleConfig:
    """The rule-level config: name, required facts, NO_MATCH check,
    cost computation, severity thresholds.

    The shared pipeline (gates, NO_MATCH, severity, payload
    assembly) lives in `evaluate_storage`; this dataclass is the
    rule-specific data + the small set of callables that vary by
    rule. Adding a new storage rule = one config; no change to the
    shared function.
    """

    rule_name: str
    # Fact keys the rule needs to conclude. The shared function
    # gates on each (KNOWN state required; UNKNOWN = inconclusive).
    # Facts present but not in this list are still available to
    # `should_emit` / `compute_cost` via the indexed fact dict —
    # the snapshot_orphan rule reads the optional `storage_tier`
    # and `start_time` facts this way.
    required_facts: tuple[str, ...]
    # NO_MATCH check. Returns True to proceed, False for a
    # definitive NO_MATCH, or raises `StorageInconclusiveError(reason)`
    # to surface an inconclusive condition (e.g. a fact value
    # that's present but malformed).
    should_emit: Callable[[dict[str, Fact]], bool]
    # Compute the monthly cost. Returns `None` for a NO_MATCH
    # that's only known AFTER the cost is computed (e.g. the
    # gp2_to_gp3 noise threshold — a real saving of $0.01 is
    # below the noise floor). Raises `StorageInconclusiveError(reason)`
    # on a malformed input (e.g. size_gb not parseable).
    compute_cost: Callable[[dict[str, Fact], date], StorageCost | None]
    # Title and recommendation for the insight. The shared
    # function passes the fact index and the cost.
    build_title: Callable[[dict[str, Fact], StorageCost], str]
    build_recommendation: Callable[[dict[str, Fact], StorageCost], str]
    # Severity thresholds. Same across all V1 storage rules for
    # dashboard consistency. $500/CRITICAL is a fleet-level
    # problem; $50/WARNING is a real number the operator notices.
    critical_threshold: float = 500.0
    warning_threshold: float = 50.0
    # Value basis for the monetary key. "ESTIMATED" (catalog-derived)
    # is the V1 default; reconciliation with FOCUS attaches
    # informational context (focus_confirmed, focus_resource_monthly_usd)
    # without flipping the basis.
    value_basis: str = "ESTIMATED"


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _index_facts(facts: Iterable[Fact]) -> dict[str, Fact]:
    return {f"{f.namespace}.{f.key}": f for f in facts}


def _get(idx: dict[str, Fact], dotted_key: str) -> Fact | None:
    return idx.get(dotted_key)


def _account_id_of(idx: dict[str, Fact], key: str) -> str | None:
    """Account id stamped on the first required fact. All facts of
    one resource share the same account_id (the collector sets it
    on every fact write), so picking the first required fact is
    safe."""
    f = _get(idx, key)
    return f.account_id if f is not None else None


def evaluate_storage(
    resource_id: UUID,
    facts: Iterable[Fact],
    config: StorageRuleConfig,
    *,
    today: date | None = None,
    catalog_version: str | None = None,
) -> StorageInsightResult:
    """The single home of the storage cost pipeline.

    Behavior is identical to the per-rule evaluators this function
    replaced; the three V1 test suites (test_ebs_gp2_to_gp3,
    test_ebs_unattached, test_snapshot_orphan) pass unchanged.

    Pipeline (identical across the V1 rules):
    1. Gate the `required_facts`: each must be KNOWN. UNKNOWN state
       on any emits an inconclusive reason.
    2. `should_emit`: rule-specific NO_MATCH check. False =
       NO_MATCH (no alert); raises `StorageInconclusiveError(reason)`
       for a malformed-but-present fact value.
    3. `compute_cost`: the rule-specific monthly arithmetic.
       Returns `None` for a NO_MATCH known only after the cost
       (e.g. below the noise threshold). Raises
       `StorageInconclusiveError(reason)` on a malformed input.
    4. Severity from the cost vs the configured thresholds.
    5. Build the insight via `build_title` + `build_recommendation`.
       The payload structure is shared: monetary_payload_key,
       value_basis, pricing_region, price_region_exact,
       source_currency, recommendation, catalog_version — plus
       the cost's `extras` dict.
    """
    idx = _index_facts(facts)

    inconclusive: list[str] = []
    for key in config.required_facts:
        f = _get(idx, key)
        if f is None or f.value_state != ValueState.KNOWN:
            inconclusive.append(key)
    if inconclusive:
        # Missing facts — never silent, always INCONCLUSIVE so the
        # user sees the gap in their data.
        return StorageInsightResult(insights=[], inconclusive_reasons=inconclusive)

    try:
        if not config.should_emit(idx):
            return StorageInsightResult()
        cost = config.compute_cost(idx, today or date.today())
    except StorageInconclusiveError as exc:
        return StorageInsightResult(insights=[], inconclusive_reasons=[exc.reason])

    if cost is None:
        # compute_cost returned None: a NO_MATCH known only after
        # the cost (e.g. below the noise threshold).
        return StorageInsightResult()

    monthly = cost.monthly_usd
    if monthly >= config.critical_threshold:
        severity = Severity.CRITICAL
    elif monthly >= config.warning_threshold:
        severity = Severity.WARNING
    else:
        severity = Severity.INFO

    title = config.build_title(idx, cost)
    recommendation = config.build_recommendation(idx, cost)
    account_id = _account_id_of(idx, config.required_facts[0])

    payload: dict[str, Any] = {
        cost.monetary_payload_key: cost.monthly_usd,
        "value_basis": config.value_basis,
        "pricing_region": cost.pricing_region,
        "price_region_exact": cost.price_region_exact,
        "source_currency": "USD",
        "recommendation": recommendation,
        "catalog_version": catalog_version,
    }
    payload.update(cost.extras)

    return StorageInsightResult(
        insights=[
            Insight(
                rule_name=config.rule_name,
                resource_id=resource_id,
                account_id=account_id,
                severity=severity,
                title=title,
                payload=payload,
            )
        ]
    )
