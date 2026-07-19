"""Collect targets HTTP endpoints (batch onboarding, roadmap 1.3).

Persisted scan targets: onboard 35 AWS accounts with ONE CSV import
instead of 35 collect forms. Once imported, POST /collect/aws with an
empty body collects every persisted target — the ECS scheduler no longer
needs a `scan-targets` JSON secret.

Endpoints:
- POST /collect/targets/import  (operator) — CSV upsert, idempotent.
- GET  /collect/targets         (reader)   — list, external_id masked.
- DELETE /collect/targets/{aws_account_id} (operator) — offboard.

Secret discipline (F-06): external_id is a shared secret. It is
write-only over the API — accepted by the import, NEVER returned by any
response (the GET masks it as `external_id_set: true`; the repository
defers the column so it is not even SELECTed on the read path). Audit
metadata carries counts only (imported / updated / rejected), never
account ids, ARNs, or external ids.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from constat_api.audit import get_audit_db, record_event, record_read
from constat_api.auth import Principal, require_operator, verify_api_key
from constat_api.collectors.aws import DEFAULT_REGIONS
from constat_api.db import get_db
from constat_api.repositories import collect_targets as collect_targets_repo

router = APIRouter(
    prefix="/collect/targets",
    tags=["collect-targets"],
    dependencies=[Depends(verify_api_key)],
)

ACCOUNT_ID_RE = re.compile(r"^\d{12}$")

# Required CSV columns. `regions` is optional (absent column or empty
# cell = the collector's default region set). `resource_types` is not
# importable in V1 (persisted targets default to the RDS job, same as an
# explicit collect target without resource_types).
REQUIRED_CSV_COLUMNS = ("aws_account_id", "role_arn", "external_id")


class ImportRejectedRow(BaseModel):
    """One rejected CSV line. `reason` is a static string — it must never
    quote the row's values (the row may carry an external_id)."""

    line: int
    reason: str


class ImportTargetsResponse(BaseModel):
    """Counts only. external_id values are never echoed back."""

    imported: int
    updated: int
    rejected: list[ImportRejectedRow]


class CollectTargetOut(BaseModel):
    """Read view of a persisted target.

    role_arn is shown (not a secret — it is half of the confused-deputy
    pair and operators need it to verify the customer deployed the right
    role). external_id is NEVER shown: the column is NOT NULL, so the
    masked form is the constant `external_id_set: true`. Rotation happens
    by re-import (upsert), not by reading the old value back.
    """

    aws_account_id: str
    role_arn: str
    name: str | None
    regions: list[str] | None
    resource_types: list[str] | None
    external_id_set: bool
    created_at: str
    updated_at: str


def _validate_row(
    line: int,
    row: dict[str, Any],
) -> tuple[dict[str, Any] | None, ImportRejectedRow | None]:
    """Validate one CSV row. Returns (clean_row, None) or (None, rejected).

    Per-row rejection (not a whole-file 422) is deliberate: at 35
    accounts, one typo must not block the other 34 rows.
    """
    account_id = (row.get("aws_account_id") or "").strip()
    role_arn = (row.get("role_arn") or "").strip()
    external_id = (row.get("external_id") or "").strip()
    name = (row.get("name") or "").strip() or None

    if not ACCOUNT_ID_RE.match(account_id):
        return None, ImportRejectedRow(line=line, reason="aws_account_id must be 12 digits")
    if not role_arn:
        return None, ImportRejectedRow(line=line, reason="role_arn is required")
    if not external_id:
        # Confused-deputy guard (F-06), same invariant as TargetIn in
        # routers/aws.py: a cross-account role without an ExternalId
        # lets anyone who learns the ARN ride our trust policy.
        return None, ImportRejectedRow(
            line=line, reason="external_id is required when role_arn is set (F-06)"
        )

    regions: list[str] | None = None
    regions_cell = (row.get("regions") or "").strip()
    if regions_cell:
        regions = [r.strip() for r in regions_cell.split(";") if r.strip()]
        unknown = sorted(set(regions) - set(DEFAULT_REGIONS))
        if unknown:
            return None, ImportRejectedRow(
                line=line,
                reason=f"unknown region(s) {unknown} (known: {sorted(DEFAULT_REGIONS)})",
            )

    return (
        {
            "aws_account_id": account_id,
            "role_arn": role_arn,
            "external_id": external_id,
            "name": name,
            "regions": regions,
        },
        None,
    )


@router.post("/import", response_model=ImportTargetsResponse)
async def import_targets(
    request: Request,
    session: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> ImportTargetsResponse:
    """Bulk-upsert collect targets from a CSV.

    Accepts the CSV either as the raw body (content-type text/csv) or as
    JSON `{"csv": "..."}`. Header: `aws_account_id,role_arn,external_id,
    name,regions` — `regions` is optional, `;`-separated, and must be a
    subset of the collector's DEFAULT_REGIONS when present.

    Upsert semantics: one row per (tenant, aws_account_id); re-importing
    the same file is idempotent (existing rows are updated, counted in
    `updated`). This is also the external_id rotation path.
    """
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        payload = await request.json()
        csv_text = payload.get("csv") if isinstance(payload, dict) else None
        if not isinstance(csv_text, str):
            raise HTTPException(status_code=422, detail='JSON body must be {"csv": "..."}')
    elif content_type.startswith(("text/csv", "text/plain")):
        csv_text = (await request.body()).decode("utf-8")
    else:
        raise HTTPException(
            status_code=415,
            detail="unsupported content-type: send text/csv or application/json",
        )

    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None or any(
        col not in reader.fieldnames for col in REQUIRED_CSV_COLUMNS
    ):
        raise HTTPException(
            status_code=422,
            detail=f"CSV header must include: {', '.join(REQUIRED_CSV_COLUMNS)}",
        )

    imported = 0
    updated = 0
    rejected: list[ImportRejectedRow] = []
    for row in reader:
        clean, rejection = _validate_row(reader.line_num, row)
        if rejection is not None:
            rejected.append(rejection)
            continue
        assert clean is not None
        _, created = collect_targets_repo.upsert(session, **clean)
        if created:
            imported += 1
        else:
            updated += 1

    # Audit: operator action, counts only — account ids, ARNs and
    # external ids stay out of audit metadata (module docstring).
    record_event(
        session,
        action="collect_targets_imported",
        actor=principal.name,
        target_type="collect_targets",
        metadata={"imported": imported, "updated": updated, "rejected": len(rejected)},
    )
    session.commit()
    return ImportTargetsResponse(imported=imported, updated=updated, rejected=rejected)


@router.get("", response_model=list[CollectTargetOut])
def list_targets_endpoint(
    session: Session = Depends(get_db),
    principal: Principal = Depends(verify_api_key),
    audit_session: Session = Depends(get_audit_db),
) -> list[CollectTargetOut]:
    """List persisted targets with external_id masked (reader role).

    The repository defers the external_id column (never SELECTed); the
    response carries `external_id_set: true` instead — the column is NOT
    NULL, so presence is guaranteed by the schema.
    """
    rows = collect_targets_repo.list_targets(session)
    # Read attribution (CISO 3.3): the target list is the customer-
    # identifying onboarding inventory — who enumerated it is on record.
    record_read(
        audit_session,
        actor=principal.name,
        target_type="collect_targets",
        route="/collect/targets",
        row_count=len(rows),
    )
    return [
        CollectTargetOut(
            aws_account_id=t.aws_account_id,
            role_arn=t.role_arn,
            name=t.name,
            regions=t.regions,
            resource_types=t.resource_types,
            external_id_set=True,
            created_at=t.created_at.isoformat() if t.created_at else "",
            updated_at=t.updated_at.isoformat() if t.updated_at else "",
        )
        for t in rows
    ]


@router.delete("/{aws_account_id}", dependencies=[Depends(require_operator)])
def delete_target(
    aws_account_id: str,
    session: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    """Offboard one AWS account: remove its persisted target.

    Scans already enqueued or running are not affected — this only
    removes the account from future empty-body collects.
    """
    if not collect_targets_repo.delete(session, aws_account_id):
        raise HTTPException(status_code=404, detail=f"unknown collect target {aws_account_id}")
    record_event(
        session,
        action="collect_target_deleted",
        actor=principal.name,
        target_type="collect_targets",
        # target_id precedent: the AWS collector already logs
        # aws_scan_completed with target_id = the AWS account id.
        target_id=aws_account_id,
        metadata={"deleted": 1},
    )
    session.commit()
    return {"deleted": aws_account_id}
