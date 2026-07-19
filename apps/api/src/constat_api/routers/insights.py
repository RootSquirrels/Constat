"""Insights HTTP endpoints."""

from __future__ import annotations

import csv
import io
from uuid import UUID

from constat_core.catalog.fx import usd_to_eur
from constat_core.models import Insight, Severity
from constat_core.monetary import monthly_cost_and_basis
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from constat_api.audit import get_audit_db, record_read
from constat_api.auth import Principal, _get_settings, require_operator, verify_api_key
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
    principal: Principal = Depends(verify_api_key),
    audit_session: Session = Depends(get_audit_db),
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
    insights = repo.list_insights(
        session,
        rule_name=rule_name,
        severity=severity,
        account_id=account_id,
        ack_status=ack_status,
        limit=limit,
        offset=offset,
    )
    # Read attribution (CISO 3.3): who saw the insights list, with which
    # filters present (never their values) and how many rows came back.
    record_read(
        audit_session,
        actor=principal.audit_actor,
        tenant_id=principal.tenant_id,
        target_type="insights",
        route="/insights",
        filters={
            "rule_name": rule_name is not None,
            "severity": severity is not None,
            "account_id": account_id is not None,
            "ack_status": ack_status is not None,
        },
        row_count=len(insights),
    )
    return insights


def _monthly_cost_and_basis(insight: Insight) -> tuple[float | None, str]:
    """Extract the monthly cost (USD) and its value basis from the payload.

    Delegates to the registry in constat_core.monetary — the single
    source of truth for which payload key carries a rule's amount
    (ADR-13). The previous hardcoded two-branch version silently
    dropped ebs_gp2_to_gp3 savings from the CSV export.
    """
    cost, basis = monthly_cost_and_basis(insight.rule_name, insight.payload)
    return cost, basis or ""


@router.get("/export.csv")
def export_insights_csv_endpoint(
    rule_name: str | None = Query(default=None),
    severity: Severity | None = Query(default=None),
    account_id: UUID | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
    principal: Principal = Depends(verify_api_key),
    audit_session: Session = Depends(get_audit_db),
) -> Response:
    """CSV export of the current insights — the artifact a prospect's
    champion circulates internally. Same filters as GET /insights,
    capped at 500 rows (V1 pilot volume).

    Amounts are USD (the catalog/billing currency). `monthly_cost_eur`
    is a convenience conversion at the dated ECB reference rate from
    constat_core.catalog.fx — the `fx_rate` / `fx_date` columns make
    the conversion auditable ("1 USD = fx_rate EUR on fx_date"). The
    three EUR-side columns are empty for rows without a USD amount."""
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
            "monthly_cost_eur",
            "fx_rate",
            "fx_date",
            "value_basis",
            "computed_at",
        ]
    )
    for insight in insights:
        monthly_cost, value_basis = _monthly_cost_and_basis(insight)
        if monthly_cost is not None:
            eur, fx_rate, fx_date = usd_to_eur(monthly_cost)
            eur_col, rate_col, date_col = f"{eur:.2f}", str(fx_rate), fx_date.isoformat()
        else:
            eur_col = rate_col = date_col = ""
        writer.writerow(
            [
                insight.rule_name,
                insight.severity.value,
                insight.title,
                str(insight.resource_id) if insight.resource_id else "",
                insight.account_id or "",
                f"{monthly_cost:.2f}" if monthly_cost is not None else "",
                eur_col,
                rate_col,
                date_col,
                value_basis,
                insight.computed_at.isoformat(),
            ]
        )
    # The export is the highest-leakage read we serve (a full CSV that
    # leaves the system) — attribution is non-negotiable here.
    record_read(
        audit_session,
        actor=principal.audit_actor,
        tenant_id=principal.tenant_id,
        target_type="insights",
        route="/insights/export.csv",
        filters={
            "rule_name": rule_name is not None,
            "severity": severity is not None,
            "account_id": account_id is not None,
        },
        row_count=len(insights),
    )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="insights.csv"'},
    )


@router.get("/{insight_id}", response_model=Insight)
def get_insight_endpoint(
    insight_id: UUID,
    session: Session = Depends(get_db),
    principal: Principal = Depends(verify_api_key),
    audit_session: Session = Depends(get_audit_db),
) -> Insight:
    insight = repo.get_insight(session, insight_id)
    if insight is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="insight not found")
    record_read(
        audit_session,
        actor=principal.audit_actor,
        tenant_id=principal.tenant_id,
        target_type="insight",
        route="/insights/{insight_id}",
        row_count=1,
    )
    return insight


@router.post(
    "",
    response_model=Insight,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_operator)],
)
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


@router.patch("/{insight_id}", response_model=Insight, dependencies=[Depends(require_operator)])
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="insight not found")
    return updated
