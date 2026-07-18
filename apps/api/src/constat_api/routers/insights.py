"""Insights HTTP endpoints."""

from __future__ import annotations

import csv
import io
from uuid import UUID

from constat_core.models import Insight, Severity
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from constat_api.auth import _get_settings, verify_api_key
from constat_api.db import get_db
from constat_api.repositories import insights as repo
from constat_api.settings import Settings

router = APIRouter(
    prefix="/insights",
    tags=["insights"],
    dependencies=[Depends(verify_api_key)],
)


@router.get("", response_model=list[Insight])
def list_insights_endpoint(
    rule_name: str | None = Query(default=None),
    severity: Severity | None = Query(default=None),
    account_id: UUID | None = Query(default=None),
    ack_status: str | None = Query(
        default=None,
        description=(
            "Filter by operator-triage state. 'open' is a virtual value "
            "meaning ack_status IS NULL; the other values match the "
            "column directly (acknowledged | in_progress | resolved | dismissed)."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
) -> list[Insight]:
    # Validate the filter value at the boundary so a typo returns 400
    # rather than a ValueError leaking as 500 from the repo.
    if ack_status is not None and ack_status != "open" and ack_status not in repo.ACK_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"invalid ack_status {ack_status!r}; "
                f"must be 'open' or one of {sorted(repo.ACK_STATUSES)}"
            ),
        )
    return repo.list_insights(
        session,
        rule_name=rule_name,
        severity=severity,
        account_id=account_id,
        ack_status=ack_status,
        limit=limit,
        offset=offset,
    )


def _monthly_cost_and_basis(insight: Insight) -> tuple[float | None, str]:
    """Extract the monthly cost (USD) and its value basis from the payload.

    Costs live in rule-specific payload keys (the Insight contract has no
    cost field). Chargeback drift comes from FOCUS billing rows → ACTUAL.
    Rule estimates from catalog pricing (e.g. rds_eol) → ESTIMATED until a
    FOCUS line confirms them (see docs/roadmap-scoreboard-features.md).
    """
    if insight.rule_name == "chargeback":
        drift = insight.payload.get("drift_amortized_minus_billed_usd")
        return (float(drift) if isinstance(drift, int | float) else None), "ACTUAL"
    estimate = insight.payload.get("extended_support_monthly_usd")
    return (float(estimate) if isinstance(estimate, int | float) else None), "ESTIMATED"


@router.get("/export.csv")
def export_insights_csv_endpoint(
    rule_name: str | None = Query(default=None),
    severity: Severity | None = Query(default=None),
    account_id: UUID | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
) -> Response:
    """CSV export of the current insights — the artifact a prospect's
    champion circulates internally. Same filters as GET /insights,
    capped at 500 rows (V1 pilot volume)."""
    insights = repo.list_insights(
        session,
        rule_name=rule_name,
        severity=severity,
        account_id=account_id,
        limit=limit,
        offset=offset,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "rule_name",
            "severity",
            "title",
            "resource_id",
            "account_id",
            "monthly_cost_usd",
            "value_basis",
            "computed_at",
        ]
    )
    for insight in insights:
        monthly_cost, value_basis = _monthly_cost_and_basis(insight)
        writer.writerow(
            [
                insight.rule_name,
                insight.severity.value,
                insight.title,
                str(insight.resource_id) if insight.resource_id else "",
                insight.account_id or "",
                f"{monthly_cost:.2f}" if monthly_cost is not None else "",
                value_basis,
                insight.computed_at.isoformat(),
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="insights.csv"'},
    )


@router.get("/{insight_id}", response_model=Insight)
def get_insight_endpoint(insight_id: UUID, session: Session = Depends(get_db)) -> Insight:
    insight = repo.get_insight(session, insight_id)
    if insight is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="insight not found")
    return insight


@router.post("", response_model=Insight, status_code=status.HTTP_201_CREATED)
def create_insight_endpoint(
    insight: Insight,
    session: Session = Depends(get_db),
    cfg: Settings = Depends(_get_settings),
) -> Insight:
    """Manual insight insertion — tests and local demos only (F-10).

    Any API-key holder could otherwise forge an insight without
    provenance, so this is gated behind CONSTAT_ENABLE_MANUAL_INSIGHTS
    (default off). Real insights are written by the rule runner. When
    enabled, the payload is stamped source="manual" so these rows stay
    distinguishable from rule-produced ones.
    """
    if not cfg.enable_manual_insights:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="manual insight creation is disabled "
            "(set CONSTAT_ENABLE_MANUAL_INSIGHTS=1 to enable)",
        )
    insight.payload = {**insight.payload, "source": "manual"}
    return repo.insert_insight(session, insight)


# ----------------------------------------------------------------------------
# Operator acknowledgment (P1 item 1)
# ----------------------------------------------------------------------------


class AckIn(BaseModel):
    """Body for PATCH /insights/{id}.

    `ack_status` is required (the point of the PATCH). `ack_by` is
    optional but recommended; without it the audit trail says
    "someone acked this, we don't know who". `ack_at` is server-set
    and the client cannot override it.
    """

    ack_status: str
    ack_by: str | None = None


@router.patch("/{insight_id}", response_model=Insight)
def patch_insight_endpoint(
    insight_id: UUID,
    body: AckIn,
    session: Session = Depends(get_db),
) -> Insight:
    """Set the operator-triage state of an insight.

    The V1 schema accepts four values plus a virtual "open" via
    `ack_status: null` (see list endpoint). Last write wins; we do
    not keep an audit log (V2 adds `insight_acks` for history).
    """
    if body.ack_status not in repo.ACK_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"invalid ack_status {body.ack_status!r}; "
                f"must be one of {sorted(repo.ACK_STATUSES)}"
            ),
        )
    updated = repo.update_ack(
        session,
        insight_id,
        ack_status=body.ack_status,
        ack_by=body.ack_by,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="insight not found"
        )
    return updated
