"""Insight runner: orchestrates rule evaluation across resources/facts.

Two rule types:
- Resource-based (rds_eol, mysql_eol, aurora_eol): for each resource,
  fetch facts, evaluate. Scope-completeness via source_runs (AWS scan
  must have succeeded). All resource rules share a single generic
  runner, `run_resource_rule`, dispatched through the RESOURCE_RULES
  registry ({rule_name: evaluate_fn}).
- Account-based (chargeback): for each (account, service) tuple in
  focus_charges, aggregate costs, emit drift insights. No source_run
  check (FOCUS is ingested manually; "completeness" = "user gave us data").

The runner is the integration point for the inventory-first promise:
we never claim MATCH/NO_MATCH for a resource unless the scope was
provably scanned. For account-based rules, the assumption is that
FOCUS data IS complete (we can't prove otherwise; the user is the source).

UX/ops P2 item 11 (metrics): the runner records
`constat_insights_emitted_total{rule, severity}`,
`constat_inconclusive_total{rule, reason}`, and
`constat_insights_run_duration_seconds{rule}` for every execution.
The SLO dashboard reads these counters; the alerting rules fire on
the histograms.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from constat_aurora_eol.resolver import evaluate as aurora_eol_evaluate
from constat_chargeback.resolver import (
    aggregate_by_period,
    aggregate_by_tag,
    build_insights,
)
from constat_core.models import Fact, Inconclusive, Insight
from constat_ebs_gp2_to_gp3.resolver import evaluate as ebs_gp2_to_gp3_evaluate
from constat_ebs_unattached.resolver import evaluate as ebs_unattached_evaluate
from constat_ec2_stopped_with_storage.resolver import (
    evaluate as ec2_stopped_with_storage_evaluate,
)
from constat_focus.loader import FocusCharge
from constat_mysql_eol.resolver import evaluate as mysql_eol_evaluate
from constat_rds_eol.resolver import evaluate as rds_eol_evaluate
from constat_snapshot_orphan.resolver import evaluate as snapshot_orphan_evaluate
from sqlalchemy.orm import Session

from constat_api.insights.reconcile import reconcile_with_focus
from constat_api.metrics import (
    record_inconclusive,
    record_insight_emitted,
    record_insight_run_duration,
)
from constat_api.orm import AccountORM, FocusChargeORM, InsightRunORM, ResourceORM
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import inconclusive as inconclusive_repo
from constat_api.repositories import insight_events as insight_events_repo
from constat_api.repositories import insights as insights_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.repositories.source_runs import _age_since
from constat_api.tenant import tenant_or_default

logger = logging.getLogger(__name__)

# Default source for the legacy RDS-only path. Each rule declares its own
# source via RULE_SOURCES; this constant is kept for callers that
# historically used `latest_successful_run(..., source=DEFAULT_SOURCE)`
# without a rule name (audit/inconclusive-cleanup paths).
DEFAULT_SOURCE = "aws_rds"

# Freshness window for scope proof (audit F-02). A successful source_run
# older than this no longer proves the scope: the resource goes
# INCONCLUSIVE with reason scope_stale instead of MATCH/NO_MATCH.
DEFAULT_SCOPE_MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class RunResult:
    rule_name: str
    resources_scanned: int
    insights_emitted: int
    inconclusive_emitted: int
    errors: list[str]
    period_label: str = ""  # for account-based rules: which period was aggregated


# Signature shared by all resource-based rule resolvers: each package
# defines its own InsightResult, but they all expose the same contract
# (.insights, .inconclusive_reasons, .is_conclusive), which is all the
# runner relies on.
ResourceEvaluateFn = Callable[..., Any]

# Resource-based rule registry: rule_name -> resolver evaluate function.
# Adding a new resource rule = one resolver package + one line here.
RESOURCE_RULES: dict[str, ResourceEvaluateFn] = {
    "rds_eol": rds_eol_evaluate,
    "mysql_eol": mysql_eol_evaluate,
    "aurora_eol": aurora_eol_evaluate,
    "ebs_gp2_to_gp3": ebs_gp2_to_gp3_evaluate,
    "ebs_unattached": ebs_unattached_evaluate,
    "snapshot_orphan": snapshot_orphan_evaluate,
    "ec2_stopped_with_storage": ec2_stopped_with_storage_evaluate,
}


# Source name per rule. Scope-completeness is per (account, region,
# resource_type, source): a successful RDS scan does NOT prove EC2 scope
# and vice-versa. Each rule must declare its source here so the runner's
# `_is_scope_proven` looks up the right source_run.
#
# Adding a new resource rule = one entry here. The rule package is free
# to expose a SOURCE constant (mysql_eol, rds_eol, ebs_gp2_to_gp3 all
# do); the dict below is the single source of truth for the runner.
RULE_SOURCES: dict[str, str] = {
    "rds_eol": "aws_rds",
    "mysql_eol": "aws_rds",
    "aurora_eol": "aws_rds",
    "ebs_gp2_to_gp3": "aws_ec2",
    "ebs_unattached": "aws_ec2",
    "snapshot_orphan": "aws_ec2",
    "ec2_stopped_with_storage": "aws_ec2",
}


def _is_scope_proven(
    session: Session,
    resource: ResourceORM,
    *,
    source: str = DEFAULT_SOURCE,
    max_age: timedelta | None = None,
) -> bool:
    """True if a successful source_run exists for this resource's scope.

    A 'successful' run is status='success' (not 'failed' or 'running').
    Without this proof, we cannot claim MATCH/NO_MATCH; we must emit
    INCONCLUSIVE (the GTM promise: never guess).

    `source` defaults to the legacy RDS source for backward-compat with
    callers that don't know about multi-source scopes; resource-based
    rules pass the source declared in RULE_SOURCES so an RDS scan
    doesn't accidentally prove an EC2 scope (or vice-versa).

    When max_age is set, the run must also be fresher than max_age
    (audit F-02): an old scan no longer proves the scope.
    """
    run = source_runs_repo.latest_successful_run(
        session,
        account_id=resource.account_id,
        region=resource.region,
        resource_type=resource.resource_type,
        source=source,
        max_age=max_age,
    )
    return run is not None


def _scope_inconclusive(
    session: Session,
    resource: ResourceORM,
    *,
    rule_name: str,
    scope_max_age: timedelta | None,
    source: str = DEFAULT_SOURCE,
) -> Inconclusive | None:
    """Return the scope-level Inconclusive for this resource, or None if proven.

    Two distinct machine-readable reasons (audit F-02):
    - scope_not_proven: no successful source_run exists at all.
    - scope_stale: a successful run exists but is older than scope_max_age;
      the human-readable reason carries the run age.

    `source` selects which source_run to check (RDS, EC2, ...). Resource
    rules pass the source declared in RULE_SOURCES.
    """
    if _is_scope_proven(session, resource, source=source, max_age=scope_max_age):
        return None
    account_id = str(resource.account_id) if resource.account_id else None
    latest = source_runs_repo.latest_successful_run(
        session,
        account_id=resource.account_id,
        region=resource.region,
        resource_type=resource.resource_type,
        source=source,
    )
    if latest is None:
        return Inconclusive(
            rule_name=rule_name,
            resource_id=resource.id,
            account_id=account_id,
            missing_facts=["scope_not_proven"],
            reason=(f"no successful source_run for ({resource.region}, {resource.resource_type})"),
        )
    age = _age_since(latest.finished_at) if latest.finished_at else None
    return Inconclusive(
        rule_name=rule_name,
        resource_id=resource.id,
        account_id=account_id,
        missing_facts=["scope_stale"],
        reason=(
            f"scope_stale: latest successful source_run for "
            f"({resource.region}, {resource.resource_type}) is {age} old, "
            f"older than the freshness window {scope_max_age}"
        ),
    )


def _emit_inconclusive(
    session: Session,
    *,
    rule_name: str,
    resource_id: UUID,
    account_id: str | None,
    missing_facts: list[str],
    reason: str,
) -> None:
    inconclusive_repo.insert_inconclusive(
        session,
        Inconclusive(
            rule_name=rule_name,
            resource_id=resource_id,
            account_id=account_id,
            missing_facts=missing_facts,
            reason=reason,
        ),
    )


def _evaluate_resource(
    session: Session,
    resource: ResourceORM,
    *,
    rule_name: str,
    evaluate_fn: ResourceEvaluateFn,
    pydantic_facts: list[Fact],
    source: str = DEFAULT_SOURCE,
    today: date | None = None,
    scope_max_age: timedelta | None = None,
) -> tuple[list[Insight], list[Inconclusive]]:
    """Evaluate a single resource. Returns (insights, inconclusive) for the caller to insert.

    `pydantic_facts` is this resource's slice of the bulk fact fetch done
    by the caller (audit F-16: one query for all resources, grouped in
    memory — no per-resource N+1).

    `source` is the connector name (RDS, EC2, ...) used to look up the
    resource's scope-completeness proof. Defaults to DEFAULT_SOURCE for
    callers that haven't been updated for multi-source scopes.

    Returns the objects (not the IDs) so the caller controls the transaction
    boundary and the run metadata.
    """
    scope_inc = _scope_inconclusive(
        session,
        resource,
        rule_name=rule_name,
        scope_max_age=scope_max_age,
        source=source,
    )
    if scope_inc is not None:
        return [], [scope_inc]

    if not pydantic_facts:
        return [], [
            Inconclusive(
                rule_name=rule_name,
                resource_id=resource.id,
                account_id=str(resource.account_id) if resource.account_id else None,
                missing_facts=["<no facts>"],
                reason="no facts observed for this resource",
            )
        ]

    result = evaluate_fn(resource.id, pydantic_facts, today=today)

    inconclusive: list[Inconclusive] = []
    if not result.is_conclusive:
        inconclusive.append(
            Inconclusive(
                rule_name=rule_name,
                resource_id=resource.id,
                account_id=str(resource.account_id) if resource.account_id else None,
                missing_facts=result.inconclusive_reasons,
                reason="missing_facts",
            )
        )

    return list(result.insights), inconclusive


def _focus_charge_to_pydantic(
    orm: FocusChargeORM,
    per_row_tag_dicts: list[dict[str, str]] | None = None,
    per_row_costs: list[tuple[Decimal, Decimal]] | None = None,
    *,
    account_name: str = "",
) -> FocusCharge:
    """Build a FocusCharge dataclass from the ORM row + (optional) per-row data.

    V2 (P3 item 11 fix): `per_row_tag_dicts` is the flat list of
    per-input-row tag dicts read from `focus_charge_tags`. Each element
    is one input FOCUS row's tag dict. Duplicates are preserved
    intentionally — the resolver uses the row count to attribute
    cost proportionally.

    Migration 0020: `per_row_costs` is parallel to `per_row_tag_dicts`.
    Each tuple is (billed, amortized) for the same input row. The
    resolver uses the two lists together for cost-weighted tag
    attribution: 3 EUR web + 97 EUR api -> 3% / 97% (not 50/50).

    If `per_row_tag_dicts` is None, fall back to the denormalized
    `focus_charges.tags` JSONB column (one element per unique tag
    dict, V1 semantics). This keeps the runner usable for callers
    that haven't read focus_charge_tags yet.

    If `per_row_costs` is None, the FocusCharge has no per-row cost
    data — the resolver falls back to V2 row-count weighting for it.
    Pre-0020 data and data with no tags (and therefore no per-row
    tag rows) end up in this branch.

    `account_name` is the display name from the accounts table (audit
    F-13): focus_charges doesn't store it, so the caller resolves it.
    """
    if per_row_tag_dicts is None:
        per_row_tag_dicts = list(orm.tags) if orm.tags else []
    return FocusCharge(
        account_id=str(orm.account_id) if orm.account_id else "",
        account_name=account_name,
        service=orm.service,
        # Roadmap-consolidation §II.1: the cross-provider canonical
        # (e.g. "Amazon RDS" + "Azure Database for PostgreSQL" both
        # canonical to "managed_postgres") is persisted on the row
        # and read back here. The resolver groups by canonical
        # through `service_canonical or service` so a single
        # canonical bucket absorbs both providers.
        service_canonical=orm.service_canonical,
        region=orm.region,
        pricing_category=orm.pricing_category,
        period_start=orm.period_start,
        period_end=orm.period_end,
        billed_cost=orm.billed_cost,
        amortized_cost=orm.amortized_cost,
        resource_id=orm.resource_id,
        sub_account_id=orm.sub_account_id,
        tags=per_row_tag_dicts,
        per_row_costs=per_row_costs or [],
        billing_currency=orm.billing_currency,
    )


def _load_per_row_tags_for(
    session: Session, focus_charge_ids: list[int]
) -> dict[int, list[dict[str, str]]]:
    """Read per-row tags for a list of focus_charge ids.

    Migration 0020: returns {focus_charge_id: [tag_dict, ...]} where
    each tag_dict is one INPUT FOCUS ROW's full tag dict (1 element
    per input row, NOT 1 element per (key, value) row). The list is
    parallel to the per_row_costs list returned by
    `_load_per_row_costs_for`. The resolver zips the two together
    for cost-weighted tag attribution.

    Pre-0020 data: input_row_index=0, so all focus_charge_tags rows
    of the same focus_charge collapse into a single input row with
    all the keys merged. This loses per-row granularity for old data
    (a 5-row bucket with 1 web + 4 api rows would have all 5 keys
    merged into 1 dict), but the resolver's V2 row-count fallback
    handles the missing per-row cost anyway.
    """
    if not focus_charge_ids:
        return {}
    from sqlalchemy import select

    from constat_api.orm import FocusChargeTagORM

    stmt = select(FocusChargeTagORM).where(FocusChargeTagORM.focus_charge_id.in_(focus_charge_ids))
    # Group by (focus_charge_id, input_row_index) — collect all (key,
    # value) pairs of the same input row into one tag dict.
    grouped: dict[tuple[int, int], dict[str, str]] = {}
    max_idx_per_charge: dict[int, int] = {cid: -1 for cid in focus_charge_ids}
    for tag_row in session.execute(stmt).scalars():
        key = (tag_row.focus_charge_id, tag_row.input_row_index)
        if key not in grouped:
            grouped[key] = {}
        grouped[key][tag_row.key] = tag_row.value
        max_idx_per_charge[tag_row.focus_charge_id] = max(
            max_idx_per_charge[tag_row.focus_charge_id], tag_row.input_row_index
        )
    by_charge: dict[int, list[dict[str, str]]] = {cid: [] for cid in focus_charge_ids}
    for cid in focus_charge_ids:
        max_idx = max_idx_per_charge[cid]
        for i in range(max_idx + 1):
            by_charge[cid].append(grouped.get((cid, i), {}))
    return by_charge


def _load_per_row_costs_for(
    session: Session, focus_charge_ids: list[int]
) -> dict[int, list[tuple[Decimal, Decimal]]]:
    """Read per-row costs for a list of focus_charge ids (migration 0020).

    Returns {focus_charge_id: [(billed, amortized), ...]} where each
    tuple is one input FOCUS row's cost, indexed by input_row_index
    in the focus_charge_tags table.

    The cost is denormalized across all (key, value) rows of the
    same input row in focus_charge_tags. We group by input_row_index
    and take the first row's cost (they should all be equal for the
    same input row — the upsert writes the same cost on every row
    of the same input).

    Pre-0020 rows have input_row_index=0 and billed_cost=0; the
    resolver falls back to V2 row-count weighting for those.

    The returned list is parallel to the one returned by
    `_load_per_row_tags_for`: index N is the cost of the input row
    whose tag dict is at index N in the tag list.
    """
    if not focus_charge_ids:
        return {}
    from sqlalchemy import select

    from constat_api.orm import FocusChargeTagORM

    stmt = select(FocusChargeTagORM).where(FocusChargeTagORM.focus_charge_id.in_(focus_charge_ids))
    # Group by (focus_charge_id, input_row_index) — keep the first
    # billed_cost / amortized_cost seen (they should be equal across
    # all tag rows of the same input row, by the upsert invariant).
    by_charge: dict[int, dict[int, tuple[Decimal, Decimal]]] = {cid: {} for cid in focus_charge_ids}
    for tag_row in session.execute(stmt).scalars():
        per_input = by_charge[tag_row.focus_charge_id]
        if tag_row.input_row_index not in per_input:
            per_input[tag_row.input_row_index] = (tag_row.billed_cost, tag_row.amortized_cost)
    # Convert the inner dict to an ordered list, indexed by input_row_index.
    out: dict[int, list[tuple[Decimal, Decimal]]] = {}
    for cid, per_input in by_charge.items():
        if per_input:
            max_idx = max(per_input.keys())
            out[cid] = [per_input.get(i, (Decimal("0"), Decimal("0"))) for i in range(max_idx + 1)]
        else:
            out[cid] = []
    return out


def run_resource_rule(
    session: Session,
    rule_name: str,
    *,
    today: date | None = None,
    scope_max_age: timedelta | None = DEFAULT_SCOPE_MAX_AGE,
) -> RunResult:
    """Run a resource-based rule across all resources. Emits insights and inconclusive.

    Wraps everything in a single insight_run row for auditability.

    Delete-and-replace (audit F-03): the rule's previous insights and
    inconclusive rows are deleted at the start of the run, so re-runs
    never accumulate duplicates. The pre-delete state is snapshotted
    first (roadmap 2.4): the post-run fingerprint diff writes
    appeared/resolved rows to insight_events, so history survives the
    replace. The operator's ack (ack_status / ack_at / ack_by) is
    also snapshotted, keyed by **stable_id** (the gap identity, not
    the title — see insights_repo.stable_id_of), and re-applied to
    the fresh rows after the insert. The title can change every day
    (EOL countdown, phase transition, drift amount) and the operator's
    decision still survives, because the gap is the same; the
    fingerprint churn is a separate problem. Fresh ESTIMATED amounts
    are then contextualized against FOCUS: the resource's FOCUS cost
    for the same period is attached to the payload as informational
    context (focus_confirmed, focus_resource_monthly_usd, focus_period,
    focus_billing_currency). The basis never flips to ACTUAL — that
    label is reserved for V2 when a per-charge-type matcher can link
    a rule's amount to a specific FOCUS component.

    Args:
        rule_name: key in RESOURCE_RULES (rds_eol, mysql_eol, aurora_eol).
        today: injected "current date" for the EOL computation (tests).
        scope_max_age: freshness window for the scope proof (audit F-02).
            A successful source_run older than this sends the resource to
            INCONCLUSIVE scope_stale. None disables the freshness check.
    """
    if rule_name not in RESOURCE_RULES:
        raise ValueError(f"unknown resource rule: {rule_name} (supports: {sorted(RESOURCE_RULES)})")
    evaluate_fn = RESOURCE_RULES[rule_name]
    # Each rule declares its source. The scope check looks up source_runs
    # by this name, so a successful RDS scan does NOT prove EC2 scope.
    source = RULE_SOURCES.get(rule_name, DEFAULT_SOURCE)

    run = InsightRunORM(
        tenant_id=tenant_or_default(session),
        rule_name=rule_name,
        status="running",
    )
    session.add(run)
    session.commit()

    started = time.monotonic()
    resources = session.query(ResourceORM).all()

    # Roadmap 2.4: snapshot the rule's current insights BEFORE the delete —
    # the delete-and-replace below would otherwise destroy the history we
    # need for the appeared/resolved diff after the fresh inserts.
    previous_state = insight_events_repo.snapshot_rule(session, rule_name)

    # Ack carry-over: snapshot the rule's acked rows keyed by stable_id
    # (gap identity, not title) so the operator's decision survives the
    # delete-and-replace even when the title changes (EOL countdown,
    # pricing-tier transition, amount refresh). The lifecycle fingerprint
    # is the wrong key here — it hashes the title, which embeds the
    # dynamic `days_to_eol` and changes every re-run.
    ack_snapshot = insights_repo.snapshot_acks(session, rule_name)

    # F-03: clear the rule's previous output before writing fresh results.
    insights_repo.delete_insights_for_rule(session, rule_name)
    inconclusive_repo.delete_inconclusive_for_rule(session, rule_name)

    # F-16: one bulk query for all resources' facts, grouped in memory.
    facts_by_resource: dict[UUID | None, list[Fact]] = {}
    for fact in facts_repo.list_facts_for_resources(session, [r.id for r in resources]):
        facts_by_resource.setdefault(fact.resource_id, []).append(fact)

    insights_emitted = 0
    inconclusive_emitted = 0
    errors: list[str] = []

    for resource in resources:
        try:
            insights, inconclusive = _evaluate_resource(
                session,
                resource,
                rule_name=rule_name,
                evaluate_fn=evaluate_fn,
                pydantic_facts=facts_by_resource.get(resource.id, []),
                source=source,
                today=today,
                scope_max_age=scope_max_age,
            )
            for insight in insights:
                insights_repo.insert_insight(session, insight)
                insights_emitted += 1
                record_insight_emitted(rule=rule_name, severity=insight.severity.value)
            for inc in inconclusive:
                inconclusive_repo.insert_inconclusive(session, inc)
                inconclusive_emitted += 1
                record_inconclusive(rule=rule_name, reason=inc.reason or "unspecified")
        except Exception as exc:
            errors.append(f"{resource.id}: {exc}")
            logger.exception("Resource %s failed", resource.id)

    # Roadmap 2.3 (audit fix): attach FOCUS context to the fresh
    # ESTIMATED amounts (in-place payload merge; the basis never
    # flips to ACTUAL — see apps/api/insights/reconcile.py).
    reconcile_with_focus(session, rule_name)

    # Ack carry-over (after the fresh inserts, before the diff): apply
    # the snapshotted acks to the rule's new insights by stable_id. The
    # fresh rows have ack_status=NULL at this point, so the apply is
    # unconditional — every match gets the carried ack. A row that
    # doesn't match (genuinely new gap, or genuinely closed) is left
    # alone.
    insights_repo.apply_acks_to_rule(session, rule_name, ack_snapshot)

    # Roadmap 2.4: diff old vs fresh fingerprints -> appeared/resolved
    # events, in the same transaction as the fresh insights.
    insight_events_repo.diff_and_record_events(
        session, rule_name=rule_name, previous=previous_state, insight_run_id=run.id
    )

    run.finished_at = datetime.now(tz=UTC)
    run.status = "success" if not errors else "partial"
    run.resources_scanned = len(resources)
    run.insights_emitted = insights_emitted
    session.commit()

    record_insight_run_duration(rule=rule_name, duration_seconds=time.monotonic() - started)

    return RunResult(
        rule_name=rule_name,
        resources_scanned=len(resources),
        insights_emitted=insights_emitted,
        inconclusive_emitted=inconclusive_emitted,
        errors=errors,
    )


def run_rds_eol(
    session: Session,
    *,
    today: date | None = None,
    scope_max_age: timedelta | None = DEFAULT_SCOPE_MAX_AGE,
) -> RunResult:
    """Thin back-compat wrapper: run the rds_eol rule via the generic runner."""
    return run_resource_rule(session, "rds_eol", today=today, scope_max_age=scope_max_age)


def run_mysql_eol(
    session: Session,
    *,
    today: date | None = None,
    scope_max_age: timedelta | None = DEFAULT_SCOPE_MAX_AGE,
) -> RunResult:
    """Thin wrapper: run the mysql_eol rule via the generic runner."""
    return run_resource_rule(session, "mysql_eol", today=today, scope_max_age=scope_max_age)


def run_aurora_eol(
    session: Session,
    *,
    today: date | None = None,
    scope_max_age: timedelta | None = DEFAULT_SCOPE_MAX_AGE,
) -> RunResult:
    """Thin wrapper: run the aurora_eol rule via the generic runner."""
    return run_resource_rule(session, "aurora_eol", today=today, scope_max_age=scope_max_age)


def run_ebs_gp2_to_gp3(
    session: Session,
    *,
    today: date | None = None,
    scope_max_age: timedelta | None = DEFAULT_SCOPE_MAX_AGE,
) -> RunResult:
    """Thin wrapper: run the ebs_gp2_to_gp3 rule via the generic runner."""
    return run_resource_rule(session, "ebs_gp2_to_gp3", today=today, scope_max_age=scope_max_age)


def run_ebs_unattached(
    session: Session,
    *,
    today: date | None = None,
    scope_max_age: timedelta | None = DEFAULT_SCOPE_MAX_AGE,
) -> RunResult:
    """Thin wrapper: run the ebs_unattached rule via the generic runner."""
    return run_resource_rule(session, "ebs_unattached", today=today, scope_max_age=scope_max_age)


def run_snapshot_orphan(
    session: Session,
    *,
    today: date | None = None,
    scope_max_age: timedelta | None = DEFAULT_SCOPE_MAX_AGE,
) -> RunResult:
    """Thin wrapper: run the snapshot_orphan rule via the generic runner."""
    return run_resource_rule(session, "snapshot_orphan", today=today, scope_max_age=scope_max_age)


def run_ec2_stopped_with_storage(
    session: Session,
    *,
    today: date | None = None,
    scope_max_age: timedelta | None = DEFAULT_SCOPE_MAX_AGE,
) -> RunResult:
    """Thin wrapper: run the ec2_stopped_with_storage rule via the generic runner."""
    return run_resource_rule(
        session, "ec2_stopped_with_storage", today=today, scope_max_age=scope_max_age
    )


def run_chargeback(
    session: Session,
    *,
    period_label: str = "all-time",
    tag_key: str | None = None,
) -> RunResult:
    """Run the chargeback rule across all FOCUS charges.

    For each (account, service) tuple, aggregate costs and emit an
    insight with the amortized-vs-billed drift. No source_run check:
    FOCUS is "complete by ingestion" (the user is the source).

    Delete-and-replace (audit F-03): all previous chargeback insights are
    deleted at the start of the run. The tag_key variant shares the rule
    name "chargeback", so a tagged run also clears untagged insights (and
    vice versa) — the V1 semantic is "the insights table holds the output
    of the latest chargeback run, whichever grouping was used".

    Args:
        period_label: human-readable label for the aggregation scope.
        tag_key: when set, re-aggregate by (account, service, period,
            tag_value) where tag_value is taken from each row's tag dict.
            Charges with no tag for the key are bucketed as `UNTAGGED`.
    """
    run = InsightRunORM(
        tenant_id=tenant_or_default(session),
        rule_name="chargeback",
        status="running",
    )
    session.add(run)
    session.commit()

    started = time.monotonic()
    # Roadmap 2.4: snapshot before the delete — chargeback also
    # deletes/replaces, so its appeared/resolved history needs the same
    # fingerprint diff as the resource rules.
    previous_state = insight_events_repo.snapshot_rule(session, "chargeback")

    # Ack carry-over: same as run_resource_rule — snapshot the rule's
    # acked rows keyed by stable_id (account_id + service + period +
    # tag) so the operator's decision survives the daily FOCUS re-ingest
    # even when the drift amount in the title changes.
    ack_snapshot = insights_repo.snapshot_acks(session, "chargeback")

    # F-03: clear the rule's previous output before writing fresh results.
    insights_repo.delete_insights_for_rule(session, "chargeback")

    # F-13: resolve account display names once for readable insight titles.
    account_names = {
        str(acc_id): name
        for acc_id, name in session.query(AccountORM.id, AccountORM.name).all()
        if name
    }

    # Distinct accounts that have FOCUS data
    account_ids = {row[0] for row in session.query(FocusChargeORM.account_id).distinct().all()}
    insights_emitted = 0
    errors: list[str] = []

    for account_id in account_ids:
        try:
            orm_charges = (
                session.query(FocusChargeORM).filter(FocusChargeORM.account_id == account_id).all()
            )
            if not orm_charges:
                continue

            # V2: read per-row tags for proportional cost attribution.
            # Migration 0020: also read per-row costs for cost-weighted
            # tag attribution (3 EUR web + 97 EUR api -> 3% / 97%, not
            # 50/50). The two lists are zipped in the resolver.
            focus_charge_ids = [c.id for c in orm_charges]
            per_row_tags_by_id = _load_per_row_tags_for(session, focus_charge_ids)
            per_row_costs_by_id = _load_per_row_costs_for(session, focus_charge_ids)

            charges = [
                _focus_charge_to_pydantic(
                    c,
                    per_row_tags_by_id.get(c.id),
                    per_row_costs_by_id.get(c.id),
                    account_name=account_names.get(str(c.account_id), ""),
                )
                for c in orm_charges
            ]
            if tag_key:
                aggregated = aggregate_by_tag(charges, tag_key=tag_key)
            else:
                aggregated = aggregate_by_period(charges)
            insights = build_insights(aggregated, period_label=period_label)
            for insight in insights:
                insights_repo.insert_insight(session, insight)
                insights_emitted += 1
                record_insight_emitted(rule="chargeback", severity=insight.severity.value)
        except Exception as exc:
            errors.append(f"account {account_id}: {exc}")
            logger.exception("Account %s chargeback failed", account_id)

    run.finished_at = datetime.now(tz=UTC)
    run.status = "success" if not errors else "partial"
    run.resources_scanned = len(account_ids)
    run.insights_emitted = insights_emitted

    # Ack carry-over: apply the snapshotted acks to the fresh
    # chargeback insights by stable_id. Done in the same transaction
    # as the rest of the run — a failed run leaves no half-acked rows.
    insights_repo.apply_acks_to_rule(session, "chargeback", ack_snapshot)

    # Roadmap 2.4: diff old vs fresh fingerprints -> appeared/resolved
    # events, same transaction as the fresh insights + the audit row.
    insight_events_repo.diff_and_record_events(
        session, rule_name="chargeback", previous=previous_state, insight_run_id=run.id
    )

    # Audit: log the insight run. The metadata is the rule name +
    # the counts. The rule name is the action, the counts are the
    # scope. We don't log the period_label or tag_key in metadata
    # because they could be PII ("my-tenant-2024-internal") — the
    # caller can correlate via the insight_run row.
    from constat_api.audit import record_event

    record_event(
        session,
        action="chargeback_run",
        actor="system:insights_runner",
        target_type="rule",
        target_id="chargeback",
        metadata={
            "accounts_scanned": len(account_ids),
            "insights_emitted": insights_emitted,
            "errors_count": len(errors),
            "has_tag_key": tag_key is not None,
        },
    )

    session.commit()

    record_insight_run_duration(rule="chargeback", duration_seconds=time.monotonic() - started)

    effective_label = f"{period_label} tag_key={tag_key}" if tag_key else period_label

    return RunResult(
        rule_name="chargeback",
        resources_scanned=len(account_ids),
        insights_emitted=insights_emitted,
        inconclusive_emitted=0,  # chargeback doesn't emit INCONCLUSIVE in V1
        errors=errors,
        period_label=effective_label,
    )


# Dispatcher for CLI and HTTP endpoint.
RunnerFn = Callable[..., RunResult]

RUNNERS: dict[str, RunnerFn] = {
    "rds_eol": run_rds_eol,
    "mysql_eol": run_mysql_eol,
    "aurora_eol": run_aurora_eol,
    "ebs_gp2_to_gp3": run_ebs_gp2_to_gp3,
    "ebs_unattached": run_ebs_unattached,
    "snapshot_orphan": run_snapshot_orphan,
    "ec2_stopped_with_storage": run_ec2_stopped_with_storage,
    "chargeback": run_chargeback,
}


def run_rule(
    session: Session,
    rule_name: str,
    *,
    today: date | None = None,
    period_label: str = "all-time",
    tag_key: str | None = None,
) -> RunResult:
    """Dispatch to the rule's runner. Raises ValueError on unknown rule."""
    if rule_name not in RUNNERS:
        raise ValueError(f"unknown rule: {rule_name} (V1 supports: {sorted(RUNNERS)})")
    if rule_name == "chargeback":
        return run_chargeback(session, period_label=period_label, tag_key=tag_key)
    # All other RUNNERS entries are resource-based rules sharing the
    # generic runner semantics.
    return run_resource_rule(session, rule_name, today=today)
