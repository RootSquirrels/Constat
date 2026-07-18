"""Insight runner: for each resource, evaluate rules and emit insights/inconclusive.

For each resource in `resources`:
1. Scope check: is there a successful source_run for (account, region, type)?
   If no -> emit INCONCLUSIVE with reason 'scope_not_proven'. Stop here.
2. Fetch facts. If empty -> emit INCONCLUSIVE with reason 'no_facts'. Stop here.
3. Call the rule's evaluate function. Get InsightResult.
4. Emit insights (MATCH) and/or inconclusive (INCONCLUSIVE).

The runner is the integration point for the inventory-first promise:
we never claim MATCH or NO_MATCH unless the scope was provably scanned.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

from constat_core.models import Fact, Inconclusive
from constat_rds_eol.resolver import evaluate as rds_eol_evaluate
from sqlalchemy.orm import Session

from constat_api.orm import InsightRunORM, ResourceORM
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
    resource: ResourceORM,
    missing_facts: list[str],
    reason: str,
) -> None:
    inconclusive_repo.insert_inconclusive(
        session,
        Inconclusive(
            rule_name=rule_name,
            resource_id=resource.id,
            account_id=str(resource.account_id) if resource.account_id else None,
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
            for inc in inconclusive:
                inconclusive_repo.insert_inconclusive(session, inc)
                inconclusive_emitted += 1
        except Exception as exc:
            errors.append(f"{resource.id}: {exc}")
            logger.exception("Resource %s failed", resource.id)

    run.finished_at = datetime.now(tz=UTC)
    run.status = "success" if not errors else "partial"
    run.resources_scanned = len(resources)
    run.insights_emitted = insights_emitted
    session.commit()

    return RunResult(
        rule_name="rds_eol",
        resources_scanned=len(resources),
        insights_emitted=insights_emitted,
        inconclusive_emitted=inconclusive_emitted,
        errors=errors,
    )
