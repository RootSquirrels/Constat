"""Insight runner: orchestrates rule evaluation across resources/facts.

Two rule types:
- Resource-based (rds_eol): for each resource, fetch facts, evaluate.
  Scope-completeness via source_runs (AWS scan must have succeeded).
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
from datetime import UTC, date, datetime

from constat_chargeback.resolver import (
    aggregate_by_period,
    aggregate_by_tag,
    build_insights,
)
from constat_core.models import Fact, Inconclusive
from constat_focus.loader import FocusCharge
from constat_rds_eol.resolver import evaluate as rds_eol_evaluate
from sqlalchemy.orm import Session

from constat_api.metrics import (
    record_inconclusive,
    record_insight_emitted,
    record_insight_run_duration,
)
from constat_api.orm import FocusChargeORM, InsightRunORM, ResourceORM
from constat_api.repositories import facts as facts_repo
from constat_api.repositories import inconclusive as inconclusive_repo
from constat_api.repositories import insights as insights_repo
from constat_api.repositories import source_runs as source_runs_repo
from constat_api.repositories.facts import _orm_to_pydantic
from constat_api.settings import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)

# V1: hardcoded source name. V2 will have multiple sources per resource type.
DEFAULT_SOURCE = "aws_rds"


@dataclass(frozen=True)
class RunResult:
    rule_name: str
    resources_scanned: int
    insights_emitted: int
    inconclusive_emitted: int
    errors: list[str]
    period_label: str = ""  # for account-based rules: which period was aggregated


def _is_scope_proven(session: Session, resource: ResourceORM) -> bool:
    """True if a successful source_run exists for this resource's scope.

    A 'successful' run is status='success' (not 'failed' or 'running').
    Without this proof, we cannot claim MATCH/NO_MATCH; we must emit
    INCONCLUSIVE (the GTM promise: never guess).
    """
    run = source_runs_repo.latest_successful_run(
        session,
        account_id=resource.account_id,
        region=resource.region,
        resource_type=resource.resource_type,
        source=DEFAULT_SOURCE,
    )
    return run is not None


def _emit_inconclusive(
    session: Session,
    *,
    rule_name: str,
    resource_id,
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
    today: date | None = None,
) -> tuple[list, list[Inconclusive]]:
    """Evaluate a single resource. Returns (insights, inconclusive) for the caller to insert.

    Returns the objects (not the IDs) so the caller controls the transaction
    boundary and the run metadata.
    """
    if not _is_scope_proven(session, resource):
        return [], [
            Inconclusive(
                rule_name="rds_eol",
                resource_id=resource.id,
                account_id=str(resource.account_id) if resource.account_id else None,
                missing_facts=["scope_not_proven"],
                reason=(
                    f"no successful source_run for ({resource.region}, {resource.resource_type})"
                ),
            )
        ]

    orm_facts = facts_repo.list_facts_for_resource(session, resource.id)
    if not orm_facts:
        return [], [
            Inconclusive(
                rule_name="rds_eol",
                resource_id=resource.id,
                account_id=str(resource.account_id) if resource.account_id else None,
                missing_facts=["<no facts>"],
                reason="no facts observed for this resource",
            )
        ]

    pydantic_facts: list[Fact] = [_orm_to_pydantic(f) for f in orm_facts]
    result = rds_eol_evaluate(resource.id, pydantic_facts, today=today)

    inconclusive: list[Inconclusive] = []
    if not result.is_conclusive:
        inconclusive.append(
            Inconclusive(
                rule_name="rds_eol",
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
) -> FocusCharge:
    """Build a FocusCharge dataclass from the ORM row + (optional) per-row tags.

    V2 (P3 item 11 fix): `per_row_tag_dicts` is the flat list of
    per-input-row tag dicts read from `focus_charge_tags`. Each element
    is one input FOCUS row's tag dict. Duplicates are preserved
    intentionally — the resolver uses the row count to attribute
    cost proportionally.

    If `per_row_tag_dicts` is None, fall back to the denormalized
    `focus_charges.tags` JSONB column (one element per unique tag
    dict, V1 semantics). This keeps the runner usable for callers
    that haven't read focus_charge_tags yet.
    """
    if per_row_tag_dicts is None:
        per_row_tag_dicts = list(orm.tags) if orm.tags else []
    return FocusCharge(
        account_id=str(orm.account_id) if orm.account_id else "",
        account_name="",  # not stored in focus_charges; account_id is the FOCUS BillingAccountId
        service=orm.service,
        region=orm.region,
        pricing_category=orm.pricing_category,
        period_start=orm.period_start,
        period_end=orm.period_end,
        billed_cost=orm.billed_cost,
        amortized_cost=orm.amortized_cost,
        resource_id=orm.resource_id,
        sub_account_id=orm.sub_account_id,
        tags=per_row_tag_dicts,
    )


def _load_per_row_tags_for(
    session: Session, focus_charge_ids: list[int]
) -> dict[int, list[dict[str, str]]]:
    """Read per-row tags for a list of focus_charge ids.

    Returns a dict {focus_charge_id: [tag_dict, ...]} where each
    tag_dict is one input FOCUS row's tag dict. The list preserves
    multiplicity (a focus_charge representing 5 input rows can have
    the same (key, value) appear 5 times).

    Used by the V2 chargeback runner to attribute cost proportionally
    rather than evenly.
    """
    if not focus_charge_ids:
        return {}
    from sqlalchemy import select

    from constat_api.orm import FocusChargeTagORM

    stmt = select(FocusChargeTagORM).where(FocusChargeTagORM.focus_charge_id.in_(focus_charge_ids))
    by_charge: dict[int, list[dict[str, str]]] = {cid: [] for cid in focus_charge_ids}
    for tag_row in session.execute(stmt).scalars():
        by_charge[tag_row.focus_charge_id].append({tag_row.key: tag_row.value})
    return by_charge


def run_rds_eol(session: Session, *, today: date | None = None) -> RunResult:
    """Run the rds_eol rule across all resources. Emits insights and inconclusive.

    Wraps everything in a single insight_run row for auditability.
    """
    run = InsightRunORM(
        tenant_id=DEFAULT_TENANT_ID,
        rule_name="rds_eol",
        status="running",
    )
    session.add(run)
    session.commit()

    started = time.monotonic()
    resources = session.query(ResourceORM).all()
    insights_emitted = 0
    inconclusive_emitted = 0
    errors: list[str] = []

    for resource in resources:
        try:
            insights, inconclusive = _evaluate_resource(session, resource, today=today)
            for insight in insights:
                insights_repo.insert_insight(session, insight)
                insights_emitted += 1
                record_insight_emitted(rule="rds_eol", severity=insight.severity.value)
            for inc in inconclusive:
                inconclusive_repo.insert_inconclusive(session, inc)
                inconclusive_emitted += 1
                record_inconclusive(rule="rds_eol", reason=inc.reason or "unspecified")
        except Exception as exc:
            errors.append(f"{resource.id}: {exc}")
            logger.exception("Resource %s failed", resource.id)

    run.finished_at = datetime.now(tz=UTC)
    run.status = "success" if not errors else "partial"
    run.resources_scanned = len(resources)
    run.insights_emitted = insights_emitted
    session.commit()

    record_insight_run_duration(rule="rds_eol", duration_seconds=time.monotonic() - started)

    return RunResult(
        rule_name="rds_eol",
        resources_scanned=len(resources),
        insights_emitted=insights_emitted,
        inconclusive_emitted=inconclusive_emitted,
        errors=errors,
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

    Args:
        period_label: human-readable label for the aggregation scope.
        tag_key: when set, re-aggregate by (account, service, period,
            tag_value) where tag_value is taken from each row's tag dict.
            Charges with no tag for the key are bucketed as `UNTAGGED`.
    """
    run = InsightRunORM(
        tenant_id=DEFAULT_TENANT_ID,
        rule_name="chargeback",
        status="running",
    )
    session.add(run)
    session.commit()

    started = time.monotonic()
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
            focus_charge_ids = [c.id for c in orm_charges]
            per_row_tags_by_id = _load_per_row_tags_for(session, focus_charge_ids)

            charges = [
                _focus_charge_to_pydantic(c, per_row_tags_by_id.get(c.id)) for c in orm_charges
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
    if rule_name == "rds_eol":
        return run_rds_eol(session, today=today)
    if rule_name == "chargeback":
        return run_chargeback(session, period_label=period_label, tag_key=tag_key)
    raise ValueError(f"runner dispatch failed for {rule_name}")
