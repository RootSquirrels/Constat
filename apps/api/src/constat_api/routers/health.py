"""Health check — surface real signal, not just "DB is reachable".

UX/ops P3 item 12: the V1 /health only ran `SELECT 1` on Postgres. That
detects a dead connection, not a stuck pipeline. We expand it to also
report:
- DB is reachable (the original check, kept)
- FOCUS data freshness: how stale is the most recent focus_charges
  row? An empty table or a 30-day-old newest row is a problem the
  LB / Kubernetes liveness probe should see.
- Stuck source_runs: are any scans 'running' for longer than the
  threshold? (P1#1 fix from earlier this session — a scan that died
  silently leaves the row in 'running' until cleanup_stuck_runs runs.)

The endpoint returns:
- HTTP 200 with a structured body when everything is fine
- HTTP 503 with the failing checks when something is unhealthy, so
  the LB / k8s takes the pod out of rotation

We deliberately do NOT 503 on "no data yet" (day 1 of a fresh
deploy). The defaults are: any FOCUS data, regardless of age, counts
as fresh. The threshold is configurable via the `?stale_after_hours=24`
query parameter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from constat_api.db import get_db
from constat_api.orm import FocusChargeORM, SourceRunORM
from constat_api.settings import settings

router = APIRouter(tags=["health"])

# Default freshness window. "FOCUS data is stale if the most recent
# row is older than this." 24h is generous for monthly billing: a
# healthy pipeline ingests once per FOCUS export (typically monthly).
DEFAULT_STALE_AFTER_HOURS = 24.0

# Default stuck-run threshold. A scan 'running' for longer than this
# is a sign that the worker died. Mirrors cleanup_stuck_runs's
# DEFAULT_STUCK_RUN_THRESHOLD (2h). Tuned together: if you lower
# one, lower the other.
DEFAULT_STUCK_RUN_HOURS = 2.0


@router.get("/health")
def health(
    response: Response,
    stale_after_hours: float = Query(
        default=DEFAULT_STALE_AFTER_HOURS,
        gt=0,
        description="FOCUS data is considered stale if the most recent row is older than this.",
    ),
    stuck_run_hours: float = Query(
        default=DEFAULT_STUCK_RUN_HOURS,
        gt=0,
        description="A source_run is 'stuck' if it has been 'running' for longer than this.",
    ),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """Return 200 (healthy) or 503 (one or more checks failed) with details.

    The body always includes the per-check status and a top-level
    `status` (the worst of the children). The LB / k8s probes only
    care about the HTTP status code; ops humans care about the
    body for triage.
    """
    checks: dict[str, dict[str, object]] = {}
    overall_ok = True

    # 1) DB reachable (kept from V1; the cheapest, most useful check).
    try:
        session.execute(text("SELECT 1"))
        checks["db"] = {"status": "ok"}
    except Exception as exc:
        checks["db"] = {"status": "error", "detail": str(exc)}
        overall_ok = False

    # 2) FOCUS data freshness.
    now = datetime.now(tz=UTC)
    newest_focus = session.execute(select(func.max(FocusChargeORM.ingested_at))).scalar_one()
    focus_count = session.execute(select(func.count(FocusChargeORM.id))).scalar_one()

    if newest_focus is None:
        # No data yet (day 1). Not an error — just report it.
        checks["focus_freshness"] = {
            "status": "ok",
            "detail": "no FOCUS data ingested yet",
            "ingested_count": 0,
        }
    else:
        # Sqlite returns naive datetimes; Postgres returns tz-aware.
        # Normalize to UTC before subtracting.
        if newest_focus.tzinfo is None:
            newest_focus_aware = newest_focus.replace(tzinfo=UTC)
        else:
            newest_focus_aware = newest_focus
        age = now - newest_focus_aware
        is_stale = age > timedelta(hours=stale_after_hours)
        checks["focus_freshness"] = {
            "status": "stale" if is_stale else "ok",
            "ingested_count": focus_count,
            "newest_ingested_at": newest_focus_aware.isoformat(),
            "age_seconds": int(age.total_seconds()),
            "stale_threshold_hours": stale_after_hours,
        }
        if is_stale:
            overall_ok = False

    # 3) Stuck source_runs. The 'running' partial unique index prevents
    # concurrent scans per scope, so a 'running' row older than
    # threshold means the worker died.
    cutoff = now - timedelta(hours=stuck_run_hours)
    stuck_count = session.execute(
        select(func.count(SourceRunORM.id)).where(
            SourceRunORM.status == "running",
            SourceRunORM.started_at < cutoff,
        )
    ).scalar_one()
    if stuck_count > 0:
        checks["stuck_runs"] = {
            "status": "error",
            "stuck_count": stuck_count,
            "stuck_threshold_hours": stuck_run_hours,
            "detail": (
                f"{stuck_count} source_run(s) in 'running' for > {stuck_run_hours}h. "
                "Call POST /collect/aws/cleanup-stuck-runs to free them."
            ),
        }
        overall_ok = False
    else:
        checks["stuck_runs"] = {
            "status": "ok",
            "stuck_count": 0,
            "stuck_threshold_hours": stuck_run_hours,
        }

    # Set the HTTP status code based on the overall result. The body
    # always carries the per-check breakdown for ops triage.
    response.status_code = status.HTTP_200_OK if overall_ok else status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if overall_ok else "degraded",
        "checked_at": now.isoformat(),
        "tenant": str(settings.default_tenant_id),
        "checks": checks,
    }
